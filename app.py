import streamlit as st
import time
import pandas as pd
import base64
from supabase import create_client, Client
from pydantic import BaseModel, Field
from typing import List
from google import genai
from google.genai import types

class InvoiceRecord(BaseModel):
    shipment_id: str
    lane_id: str
    mode: str
    weight_billed: float
    base_freight_billed: float
    fsc_billed: float
    total_billed: float

class InvoiceList(BaseModel):
    invoices: List[InvoiceRecord]

def init_loot_state():
    default_states = {
        'rate_card_df': pd.DataFrame(),
        'pod_df': pd.DataFrame(),
        'invoice_df': pd.DataFrame(),
        'exceptions_df': pd.DataFrame(),
        'pdf_vault': {} # Added to store uploaded PDFs in memory
    }
    for key, val in default_states.items():
        if key not in st.session_state:
            st.session_state[key] = val

def fetch_live_contracts():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(url, key)
    
    # Fetch rate cards from Supabase
    rc_response = supabase.table("rate_cards").select("*").execute()
    rate_card_df = pd.DataFrame(rc_response.data)
    
    # Fetch actual shipments (POD) from Supabase
    pod_response = supabase.table("shipments").select("*").execute()
    pod_df = pd.DataFrame(pod_response.data)
    
    return rate_card_df, pod_df

def extract_invoice_data(pdf_bytes: bytes, api_key: str, filename: str) -> pd.DataFrame:
    client = genai.Client(api_key=api_key)
    
    # 1. Stricter Prompt Engineering
    prompt = """
    You are a strict freight auditing AI. Extract billing details from this document.
    - If the document is clearly not a freight invoice, return an empty list.
    - Accurately hunt for the Shipment ID, Lane ID, and Service Mode.
    - Extract Weight Billed, Total Billed, Base Freight, and Fuel Surcharge (FSC).
    - If FSC is missing or bundled into the base rate, strictly output 0.0.
    - Ensure all monetary values are purely numeric decimals.
    """
    
    # 2. Error Catching (Try/Except)
    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf'), prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=InvoiceList,
                temperature=0.0
            )
        )
        extracted_records = [record.model_dump() for record in response.parsed.invoices] if response.parsed else []
        return pd.DataFrame(extracted_records)
    except Exception as e:
        # Silently log the error to the UI without stopping the engine
        st.toast(f"Skipped {filename}: Unreadable or API error.")
        return pd.DataFrame()

def run_3_way_match(invoice_df, pod_df, rate_card_df, tolerance_usd=1.00):
    match_1 = pd.merge(invoice_df, pod_df, on='shipment_id', how='left')
    master_df = pd.merge(match_1, rate_card_df, on=['lane_id', 'mode'], how='left')

    master_df['weight_variance_lbs'] = master_df['weight_billed'] - master_df['weight_actual']
    master_df['base_variance_usd'] = master_df['base_freight_billed'] - master_df['contracted_base_rate']
    master_df['expected_fsc'] = master_df['contracted_base_rate'] * (master_df['fsc_percent'] / 100)
    master_df['fsc_variance_usd'] = master_df['fsc_billed'] - master_df['expected_fsc']
    master_df['total_variance_usd'] = master_df['base_variance_usd'] + master_df['fsc_variance_usd']

    return master_df[
        (master_df['total_variance_usd'] > tolerance_usd) | 
        (master_df['weight_variance_lbs'] > 0)
    ].copy()

@st.cache_data
def convert_df_to_csv(df):
    return df.to_csv(index=False).encode('utf-8')

st.set_page_config(page_title="LOOT | Audit Engine", layout="wide")
init_loot_state()

with st.sidebar:
    st.title("LOOT")
    st.subheader("Batch Ingestion")
    api_key = st.secrets.get("GEMINI_API_KEY", "")
    
    uploaded_pdfs = st.file_uploader("Upload Carrier Invoices (PDF)", type=['pdf'], accept_multiple_files=True)
    
    if st.button("Run Autonomous Audit", type="primary"):
        if uploaded_pdfs and api_key:
            with st.spinner(f"Extracting {len(uploaded_pdfs)} invoices..."):
                rc, pod = fetch_live_contracts()
                st.session_state['rate_card_df'], st.session_state['pod_df'] = rc, pod
                
                all_invoices = []
                st.session_state['pdf_vault'].clear() # Clear old PDFs
                
                for pdf in uploaded_pdfs:
                    pdf_bytes = pdf.getvalue()
                    
                    # Pass pdf.name into the function so the try/except block works
                    df = extract_invoice_data(pdf_bytes, api_key, pdf.name)
                    
                    if not df.empty:
                        df['source_filename'] = pdf.name 
                        all_invoices.append(df)
                        st.session_state['pdf_vault'][pdf.name] = pdf_bytes 
                        
                    # Pause for 3 seconds so Google doesn't block the API
                    time.sleep(3)
                if all_invoices:
                    st.session_state['invoice_df'] = pd.concat(all_invoices, ignore_index=True)
                    st.session_state['exceptions_df'] = run_3_way_match(st.session_state['invoice_df'], pod, rc)
        else:
            st.error("Please provide an API key and upload PDFs.")

st.title("Exception Verification Queue")

exceptions = st.session_state['exceptions_df']
invoices = st.session_state['invoice_df']

if not invoices.empty:
    if exceptions.empty:
        st.success("Inbox Zero: All invoices perfectly matched contracts. No manual review required.")
    else:
        m1, m2 = st.columns(2)
        m1.metric("Total Leakage Flagged", f"${exceptions['total_variance_usd'].sum():,.2f}", delta="Requires Review", delta_color="inverse")
        m2.metric("Auto-Clear Rate", f"{((len(invoices) - len(exceptions)) / len(invoices)) * 100:.1f}%", "Passed Audit")
        st.divider()

        st.subheader("Flagged Discrepancies")
        display_cols = ['shipment_id', 'total_variance_usd', 'base_variance_usd', 'fsc_variance_usd', 'weight_variance_lbs', 'source_filename']
        st.dataframe(exceptions[display_cols].style.format({
            'total_variance_usd': "${:.2f}", 'base_variance_usd': "${:.2f}", 
            'fsc_variance_usd': "${:.2f}", 'weight_variance_lbs': "{:.0f} lbs"
        }), use_container_width=True, hide_index=True)

        st.download_button(
            label="Download Exceptions for Dispute (CSV)",
            data=convert_df_to_csv(exceptions[display_cols]),
            file_name="loot_exceptions_report.csv",
            mime="text/csv",
            type="primary"
        )
        
     # --- NEW: DOCUMENT REVIEWER ---
        st.divider()
        st.subheader("Document Reviewer")
        
        # Create a dropdown to select which flagged file to view
        flagged_files = exceptions['source_filename'].unique()
        selected_file = st.selectbox("Select a flagged invoice to verify the original document:", flagged_files)
        
        if selected_file and selected_file in st.session_state['pdf_vault']:
            pdf_bytes = st.session_state['pdf_vault'][selected_file]
            
            st.info("Browser security prevents embedding PDFs directly on this cloud server.")
            st.download_button(
                label="Download & View Original Document",
                data=pdf_bytes,
                file_name=selected_file,
                mime="application/pdf",
                type="primary",
                use_container_width=True
            )

else:
    st.info("Upload PDFs from the sidebar to begin batch processing.")

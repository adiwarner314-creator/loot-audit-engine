import streamlit as st
import time
import pandas as pd
from supabase import create_client, Client
from pydantic import BaseModel
from typing import List
from google import genai
from google.genai import types

# --- Box 4: Dual-Region Schema ---
class InvoiceRecord(BaseModel):
    shipment_id: str
    lane_id: str
    mode: str
    currency: str # AI outputs "USD" or "INR"
    weight_billed: float
    base_freight_billed: float
    surcharge_billed: float # Holds either FSC or GST
    total_billed: float
class InvoiceList(BaseModel):
    invoices: List[InvoiceRecord]

# ==========================================
# 5. The Inbox-Zero UI & Login Gate
# ==========================================
# st.set_page_config MUST be the very first Streamlit command!
st.set_page_config(page_title="LOOT | Audit Engine", layout="wide")
init_loot_state()

# --- Box 1: The Login Gate ---
if 'authenticated' not in st.session_state:
    st.session_state['authenticated'] = False

if not st.session_state['authenticated']:
    st.title(" LOOT | Secure Login")
    with st.form("login_form"):
        email = st.text_input("Admin Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In")
        
        if submitted:
            try:
                url = st.secrets["SUPABASE_URL"]
                key = st.secrets["SUPABASE_KEY"]
                supabase: Client = create_client(url, key)
                
                # Verify credentials against Supabase Auth
                supabase.auth.sign_in_with_password({
                    "email": email, 
                    "password": password
                })
                
                st.session_state['authenticated'] = True
                st.rerun()
            except Exception:
                st.error("Invalid email or password. Please try again.")
                
    # This stops the rest of the page from loading if they aren't logged in
    st.stop() 

# ==========================================================
# (Your existing sidebar code starts exactly here)
# ==========================================================
with st.sidebar:
    st.title("LOOT")


# ==========================================================
# (Your existing sidebar code starts exactly here)
# ==========================================================
with st.sidebar:
    st.title("LOOT")

def fetch_live_contracts():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(url, key)
    
    rc_response = supabase.table("rate_cards").select("*").execute()
    rate_card_df = pd.DataFrame(rc_response.data)
    
    pod_response = supabase.table("shipments").select("*").execute()
    pod_df = pd.DataFrame(pod_response.data)
    
    return rate_card_df, pod_df

def vault_pdf_to_supabase(pdf_bytes: bytes, filename: str):
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    supabase: Client = create_client(url, key)
    
    try:
        # Silently archive the file to your new private bucket
        supabase.storage.from_("invoice-vault").upload(
            file=pdf_bytes,
            path=filename,
            file_options={"content-type": "application/pdf"}
        )
    except Exception:
        # If a file with this exact name already exists in the bucket, skip the upload
        pass
def extract_invoice_data(pdf_bytes: bytes, api_key: str, filename: str) -> pd.DataFrame:
    client = genai.Client(api_key=api_key)
    
    # --- Box 4: Dual-Region AI Prompt ---
    prompt = """
    You are a strict freight auditing AI. Extract billing details from this document.
    - If clearly not a freight invoice, return an empty list.
    - Identify the currency as either 'USD' or 'INR'.
    - Extract Weight Billed, Total Billed, and Base Freight.
    - If USD, extract Fuel Surcharge (FSC) into 'surcharge_billed'.
    - If INR, extract total GST (CGST + SGST + IGST) into 'surcharge_billed'.
    - Ensure all monetary values are purely numeric decimals.
    """
    
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
        st.toast(f"Skipped {filename}: Unreadable or API error.")
        return pd.DataFrame()

def run_3_way_match(invoice_df, pod_df, rate_card_df, tolerance=1.00):
    match_1 = pd.merge(invoice_df, pod_df, on='shipment_id', how='left')
    master_df = pd.merge(match_1, rate_card_df, on=['lane_id', 'mode'], how='left')

    master_df['weight_variance'] = master_df['weight_billed'] - master_df['weight_actual']
    master_df['base_variance'] = master_df['base_freight_billed'] - master_df['contracted_base_rate']
    master_df['expected_surcharge'] = master_df['contracted_base_rate'] * (master_df['fsc_percent'] / 100)
    master_df['surcharge_variance'] = master_df['surcharge_billed'] - master_df['expected_surcharge']
    master_df['total_variance'] = master_df['base_variance'] + master_df['surcharge_variance']

    return master_df[
        (master_df['total_variance'] > tolerance) | 
        (master_df['weight_variance'] > 0)
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
            with st.spinner(f"Processing {len(uploaded_pdfs)} files..."):
                rc, pod = fetch_live_contracts()
                st.session_state['rate_card_df'], st.session_state['pod_df'] = rc, pod
                
                all_invoices = []
                st.session_state['pdf_vault'].clear() 
                
           for pdf in uploaded_pdfs:
                    # --- Box 3: The Crash-Proof 5MB Size Limit ---
                    if pdf.size > 5_000_000:
                        st.error(f"Skipped {pdf.name}: File exceeds 5MB limit.")
                        continue
                        
                    pdf_bytes = pdf.getvalue()
                    
                    # --- Box 2: Permanent Cloud Storage ---
                    vault_pdf_to_supabase(pdf_bytes, pdf.name)
                    
                    df = extract_invoice_data(pdf_bytes, api_key, pdf.name)
                    
                    if not df.empty:
                        df['source_filename'] = pdf.name 
                        all_invoices.append(df)
                        st.session_state['pdf_vault'][pdf.name] = pdf_bytes 
                        
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
        m1.metric("Total Leakage Flagged", f"{exceptions['total_variance'].sum():,.2f}", delta="Requires Review", delta_color="inverse")
        m2.metric("Auto-Clear Rate", f"{((len(invoices) - len(exceptions)) / len(invoices)) * 100:.1f}%", "Passed Audit")
        st.divider()

        st.subheader("Flagged Discrepancies")
        display_cols = ['shipment_id', 'currency', 'total_variance', 'base_variance', 'surcharge_variance', 'weight_variance', 'source_filename']
        st.dataframe(exceptions[display_cols].style.format({
            'total_variance': "{:.2f}", 'base_variance': "{:.2f}", 
            'surcharge_variance': "{:.2f}", 'weight_variance': "{:.0f}"
        }), use_container_width=True, hide_index=True)

        st.download_button("Download Exceptions (CSV)", convert_df_to_csv(exceptions[display_cols]), "loot_exceptions.csv", "text/csv", type="primary")
        
        st.divider()
        st.subheader("Document Reviewer")
        flagged_files = exceptions['source_filename'].unique()
        selected_file = st.selectbox("Select a flagged invoice to verify the original document:", flagged_files)
        
        if selected_file and selected_file in st.session_state['pdf_vault']:
            st.info("Browser security prevents embedding PDFs directly on this cloud server.")
            st.download_button("📥 Download & View Original Document", st.session_state['pdf_vault'][selected_file], selected_file, "application/pdf", type="primary", use_container_width=True)
else:
    st.info("Upload PDFs from the sidebar to begin batch processing.")

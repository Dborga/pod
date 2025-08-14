# app.py
import os
import logging
import fitz  # PyMuPDF
import regex as re
from flask import Flask, request, redirect, url_for, flash, send_from_directory, render_template, send_file, session
from datetime import datetime, timedelta
from rapidfuzz import fuzz
import pytesseract
from PIL import Image
import io
import zipfile
import subprocess
import base64
from werkzeug.utils import secure_filename

# Async + progress
from threading import Thread, Lock
import uuid

# --- Smartsheet + dotenv ---
from dotenv import load_dotenv
import smartsheet
# ---------------------------

# --- Gmail API imports ---
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
# --------------------------

# Load environment variables
load_dotenv()

# Configure Tesseract path (override with TESSERACT_CMD if set)
pytesseract.pytesseract.tesseract_cmd = os.getenv('TESSERACT_CMD', '/usr/local/bin/tesseract')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = 'secret-key'

# --- Debug Endpoint ---
@app.route('/debug-tesseract')
def debug_tesseract():
    env_path = os.getenv("PATH", "Not set")
    try:
        tesseract_path = subprocess.check_output(["which", "tesseract"]).decode().strip()
    except Exception as e:
        tesseract_path = f"Error: {e}"
    return f"PATH: {env_path}\nTesseract path: {tesseract_path}"
# ----------------------

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- Secrets from .env ---
UPLOAD_PASSWORD = os.getenv('UPLOAD_PASSWORD')  # set in .env
SMARTSHEET_TOKEN = os.getenv("SMARTSHEET_API")  # set in .env

# Gmail config
INBOUND_ATTACH_DIR = os.getenv("INBOUND_ATTACH_DIR", "email_attachments")
os.makedirs(INBOUND_ATTACH_DIR, exist_ok=True)
# Process all emails with attachments from the last 7 days (configurable via env)
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "has:attachment newer_than:7d")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
# Use readonly scope since we're not marking messages as read anymore
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# ======== Customer mapping / PDF processing ========

customer_mapping = {
    "crevier lubricants inc": lambda po, d: f"Crevier {po}.{d}",
    "parkland fuel corporation": lambda _, d: f"Parkland {d}",
    "catalina": lambda _, d: f"Catalina {d}",
    "econo gas": lambda _, d: f"Econogas {d}",
    "fuel it": lambda _, d: f"Fuel It {d}",
    "les petroles belisle": lambda _, d: f"Belisle {d}",
    "petro montestrie": lambda _, d: f"Petro Mont {d}",
    "petrole leger": lambda _, d: f"Leger {d}",
    "rav petroleum": lambda _, d: f"Rav {d}",
    "st-pierre fuels inc": lambda _, d: f"Stpierre {d}",
}

def extract_po_delivery(text, customer):
    po_number = None
    if customer == "crevier lubricants inc":
        po_match = re.search(r'PO\s*[:#]?\s*(5\d{5})', text, re.IGNORECASE)
        if po_match:
            po_number = po_match.group(1)
        else:
            po_match = re.search(r'\b(5\d{5})\b', text)
            if po_match:
                po_number = po_match.group(1)

    delivery_number = None
    # Space-tolerant delivery number detection (1 followed by 7 digits, spaces allowed)
    delivery_match = re.search(r'1\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d\s*\d', text)
    if delivery_match:
        raw = re.sub(r'\s+', '', delivery_match.group())
        delivery_number = raw
    else:
        # Manual format fallback (e.g., 07282025-3DB)
        manual_delivery_match = re.search(
            r'(\d{8})[-]?(\d+)([A-Za-z]{1,2})\b',
            text
        )
        if manual_delivery_match:
            date_part = manual_delivery_match.group(1)
            sequence = manual_delivery_match.group(2)
            initials = manual_delivery_match.group(3).upper()
            delivery_number = f"{date_part}-{sequence}{initials}"
            logging.info(f"Found manual delivery number: {delivery_number}")

    # Trim extra digits if more than 8 and starts with 1
    if delivery_number and delivery_number.isdigit() and delivery_number.startswith("1") and len(delivery_number) > 8:
        logging.info(f"Trimming delivery number {delivery_number} to first 8 digits.")
        delivery_number = delivery_number[:8]

    return po_number, delivery_number

def detect_customer(text):
    text_lower = text.lower()
    for customer in customer_mapping.keys():
        score = fuzz.partial_ratio(customer, text_lower)
        if score >= 80:
            logging.info(f"Detected customer '{customer}' with score {score}")
            return customer
    return None

def save_page_as_pdf(input_pdf_path, page_number, output_filename):
    try:
        doc = fitz.open(input_pdf_path)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_number, to_page=page_number)
        output_path = os.path.join(OUTPUT_FOLDER, output_filename + ".pdf")
        new_doc.save(output_path)
        new_doc.close()
        logging.info(f"Saved page {page_number + 1} as {output_path}")
        return output_filename + ".pdf"
    except Exception as e:
        logging.error(f"Error saving page {page_number + 1}: {e}")
        return None

def perform_ocr(page):
    try:
        pix = page.get_pixmap()
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))
        return pytesseract.image_to_string(img)
    except Exception as e:
        logging.error(f"Error during OCR: {e}")
        return ""

def process_pdf(pdf_path):
    saved_files = []
    try:
        doc = fitz.open(pdf_path)
        logging.info(f"Processing PDF: {pdf_path} with {doc.page_count} pages.")
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            text = page.get_text()
            if not text.strip() or len(text.strip()) < 20:
                text = perform_ocr(page)

            logging.info(f"--- PAGE {page_number + 1} RAW TEXT START ---")
            logging.info(text)
            logging.info(f"--- PAGE {page_number + 1} RAW TEXT END ---")

            customer = detect_customer(text)
            if customer:
                po_number, delivery_number = extract_po_delivery(text, customer)

                logging.info(f"PAGE {page_number + 1} - Customer: {customer}")
                logging.info(f"PAGE {page_number + 1} - Detected PO: {po_number}")
                logging.info(f"PAGE {page_number + 1} - Detected Delivery: {delivery_number}")

                if customer == "crevier lubricants inc":
                    if po_number and delivery_number:
                        output_filename = customer_mapping[customer](po_number, delivery_number)
                    else:
                        output_filename = f"Crevier_{page_number + 1}"
                else:
                    if delivery_number:
                        output_filename = customer_mapping[customer]("", delivery_number)
                    else:
                        fallback_name = customer_mapping[customer]("", "").strip()
                        output_filename = f"{fallback_name}_{page_number + 1}"
                saved = save_page_as_pdf(pdf_path, page_number, output_filename)
                if saved:
                    saved_files.append(saved)
        doc.close()
    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
    return saved_files

# ------------------ Authentication ------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == UPLOAD_PASSWORD:
            session['authenticated'] = True
            session['login_time'] = datetime.utcnow().isoformat()
            return redirect(url_for('upload_file'))
        else:
            flash('Incorrect password. Please try again.')
    return render_template('login.html')

def is_session_valid():
    login_time_str = session.get('login_time')
    if not session.get('authenticated') or not login_time_str:
        return False
    login_time = datetime.fromisoformat(login_time_str)
    if datetime.utcnow() - login_time > timedelta(minutes=15):
        return False
    return True

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if not is_session_valid():
        session.clear()
        return redirect(url_for('login'))

    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['pdf_file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        if file:
            file_path = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(file_path)
            saved_files = process_pdf(file_path)
            session['saved_files'] = saved_files
            session['login_time'] = datetime.utcnow().isoformat()
            return render_template('download.html', saved_files=saved_files) if saved_files else redirect(url_for('upload_file'))
    return render_template('upload.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

@app.route('/download_all')
def download_all():
    saved_files = session.get('saved_files', [])
    if not saved_files:
        flash("No recent files available for download.")
        return redirect(url_for('upload_file'))

    zip_filename = "processed_files.zip"
    zip_path = os.path.join(OUTPUT_FOLDER, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file_name in saved_files:
            file_path = os.path.join(OUTPUT_FOLDER, file_name)
            if os.path.exists(file_path):
                zipf.write(file_path, file_name)

    return send_file(zip_path, as_attachment=True)

# ===================== Smartsheet Integration: Test PODS =====================

if not SMARTSHEET_TOKEN:
    logging.error("SMARTSHEET_API env var is missing. Add it to your .env")
ss_client = smartsheet.Smartsheet(SMARTSHEET_TOKEN) if SMARTSHEET_TOKEN else None

WORKSPACE_NAME = "Test PODS"  # Target workspace name
_RESOLVED_WORKSPACE_ID = None  # cache once resolved

def get_workspace_id_by_name(workspace_name: str):
    """Return the workspace ID matching the provided name, else None."""
    if not ss_client:
        return None
    resp = ss_client.Workspaces.list_workspaces()
    for ws in resp.data or []:
        if ws.name.strip().lower() == workspace_name.strip().lower():
            return ws.id
    while getattr(resp, "next_page", None):
        resp = ss_client.Workspaces.list_workspaces(page=resp.next_page)
        for ws in resp.data or []:
            if ws.name.strip().lower() == workspace_name.strip().lower():
                return ws.id
    return None

def find_sheet_id_by_name_in_workspace(workspace_id: int, sheet_name: str):
    """Find a sheet by name inside a given workspace."""
    if not ss_client:
        return None
    ws = ss_client.Workspaces.get_workspace(workspace_id)  # NOTE: no .data
    for s in (ws.sheets or []):
        if s.name.strip().lower() == sheet_name.strip().lower():
            return s.id
    return None

def find_row_by_delivery_number(sheet_id: int, delivery_number: str):
    """Find the first row where the 'Delivery #' cell equals the delivery number."""
    if not ss_client:
        return None
    sheet = ss_client.Sheets.get_sheet(sheet_id)
    # Try to locate the "Delivery #" column id
    delivery_col_id = None
    for col in sheet.columns:
        if col.title.strip().lower() == "delivery #":
            delivery_col_id = col.id
            break

    if delivery_col_id:
        for row in sheet.rows:
            for cell in row.cells:
                if cell.column_id == delivery_col_id and str(cell.display_value or "").strip() == delivery_number:
                    return row.id
    else:
        # Fallback: scan all cells if the column title doesn't match
        for row in sheet.rows:
            for cell in row.cells:
                if str(cell.display_value or "").strip() == delivery_number:
                    return row.id
    return None

def extract_delivery_from_filename(filename: str):
    """
    Try to capture an 8-digit delivery number starting with 1 from the saved filename.
    Example filenames: 'Catalina 12345678.pdf', 'Crevier 512345.12345678.pdf'
    """
    m = re.search(r'\b1\d{7}\b', filename)
    if m:
        return m.group(0)
    return None

# --- Looser filename parse + month inference for inbound emails ---
def extract_delivery_from_filename_loose(filename: str) -> str | None:
    """
    Extract the 8-digit delivery number from filenames like:
      5.Oleo_POD__10049935_20250806.pdf
    Strategy:
      - Find all 8-digit sequences using digit-boundary lookarounds (works with underscores).
      - Prefer the token that:
          1) starts with '1' (your pattern), and
          2) does NOT look like a date (not starting with '20').
      - If still ambiguous, prefer the 8-digit token that is immediately BEFORE a date token (_20YYYYMMDD).
      - Fall back to the first 8-digit token.
    """
    base = os.path.basename(filename)
    name, _ = os.path.splitext(base)
    name = re.sub(r'^\d+\.', '', name)  # strip leading index like "5."

    eighters = re.findall(r'(?<!\d)(\d{8})(?!\d)', name)
    if not eighters:
        return None

    date_match = re.search(r'(?<!\d)(20\d{6})(?!\d)', name)  # YYYYMMDD
    date_token = date_match.group(1) if date_match else None

    if date_token:
        parts = name.split('_')
        ordered_eighters = []
        for p in parts:
            ordered_eighters.extend(re.findall(r'(?<!\d)(\d{8})(?!\d)', p))
        if date_token in ordered_eighters:
            idx = ordered_eighters.index(date_token)
            if idx > 0:
                candidate = ordered_eighters[idx - 1]
                if not candidate.startswith('20'):
                    return candidate

    pref = [n for n in eighters if n.startswith('1') and not n.startswith('20')]
    if pref:
        return pref[0]

    non_dates = [n for n in eighters if not n.startswith('20')]
    if non_dates:
        return non_dates[0]

    return eighters[0]

def pick_month_candidates_from_filename(filename: str) -> list[str]:
    """
    Derive month sheet names from a YYYYMMDD token if present (e.g., 20250806),
    and always include current & previous months (both long and short).
    """
    cands = set()
    name = os.path.splitext(os.path.basename(filename))[0]

    m = re.search(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)', name)  # YYYYMMDD
    if m:
        y, mo, _ = m.groups()
        try:
            dt = datetime(int(y), int(mo), 1)
            cands.add(dt.strftime('%B %Y'))
            cands.add(dt.strftime('%b %Y'))
        except ValueError:
            pass

    now = datetime.now()
    prev = (now.replace(day=1) - timedelta(days=1))
    for dt in (now, prev):
        cands.add(dt.strftime('%B %Y'))
        cands.add(dt.strftime('%b %Y'))

    return list(cands)

def upload_file_by_delivery(file_path: str):
    """
    Given a local PDF path, extract delivery # from filename, find matching row in Test PODS,
    and attach the file. Returns (success, message).
    """
    global _RESOLVED_WORKSPACE_ID
    if not ss_client:
        return (False, "Smartsheet not configured")

    if _RESOLVED_WORKSPACE_ID is None:
        _RESOLVED_WORKSPACE_ID = get_workspace_id_by_name(WORKSPACE_NAME)
        if _RESOLVED_WORKSPACE_ID is None:
            return (False, f"Workspace '{WORKSPACE_NAME}' not found")

    filename = os.path.basename(file_path)
    delivery = extract_delivery_from_filename_loose(filename)
    if not delivery:
        return (False, f"No 8-digit delivery number found in '{filename}'")

    for month_name in pick_month_candidates_from_filename(filename):
        sheet_id = find_sheet_id_by_name_in_workspace(_RESOLVED_WORKSPACE_ID, month_name)
        if not sheet_id:
            continue
        row_id = find_row_by_delivery_number(sheet_id, delivery)
        if row_id:
            # Optional idempotency: skip if same filename already attached
            try:
                atts = ss_client.Attachments.list_row_attachments(sheet_id, row_id)
                if any(getattr(a, "name", "") == filename for a in (atts.data or [])):
                    return (True, f"Already attached on row {row_id} in {month_name}")
            except Exception:
                pass
            try:
                with open(file_path, 'rb') as fh:
                    ss_client.Attachments.attach_file_to_row(
                        int(sheet_id), int(row_id), (filename, fh, 'application/pdf')
                    )
                return (True, f"Uploaded to {month_name} (row {row_id}) for delivery {delivery}")
            except Exception as e:
                logging.exception("Attach failed")
                return (False, f"Attach failed for {filename}: {e}")

    return (False, f"No matching row found for delivery {delivery}")

# ===================== Gmail: service, message processing, PROGRESS =====================

def gmail_service():
    """Get Gmail service using OAuth2."""
    creds = None
    
    # First try to load from environment variables (for production)
    if os.getenv('GMAIL_TOKEN_JSON'):
        try:
            import json
            token_data = json.loads(os.getenv('GMAIL_TOKEN_JSON'))
            creds = Credentials.from_authorized_user_info(token_data, GMAIL_SCOPES)
            logging.info("Loaded Gmail credentials from environment variable")
        except Exception as e:
            logging.error(f"Error loading credentials from environment: {e}")
    
    # Fallback to token file (for local development)
    elif os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        logging.info(f"Loaded Gmail credentials from {GMAIL_TOKEN_FILE}")
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logging.info("Refreshed Gmail credentials")
                
                # For local development, save refreshed token back to file
                if not os.getenv('GMAIL_TOKEN_JSON') and os.path.exists(os.path.dirname(GMAIL_TOKEN_FILE) if os.path.dirname(GMAIL_TOKEN_FILE) else '.'):
                    try:
                        with open(GMAIL_TOKEN_FILE, 'w') as token:
                            token.write(creds.to_json())
                        logging.info("Saved refreshed token to file")
                    except Exception as e:
                        logging.error(f"Could not save refreshed token: {e}")
                        
            except Exception as e:
                logging.error(f"Error refreshing credentials: {e}")
                creds = None
        
        if not creds or not creds.valid:
            # Try to load credentials.json from environment variable for initial auth
            if os.getenv('GMAIL_CREDENTIALS_JSON'):
                try:
                    import json
                    import tempfile
                    credentials_data = json.loads(os.getenv('GMAIL_CREDENTIALS_JSON'))
                    
                    # Create a temporary file for the flow
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_creds:
                        json.dump(credentials_data, temp_creds)
                        temp_creds_path = temp_creds.name
                    
                    flow = InstalledAppFlow.from_client_secrets_file(temp_creds_path, GMAIL_SCOPES)
                    # In production, we can't run local server, so this will fail
                    # The token should already be provided via GMAIL_TOKEN_JSON
                    logging.error("Cannot perform interactive auth in production environment")
                    
                    # Clean up temp file
                    os.unlink(temp_creds_path)
                    raise RuntimeError("Gmail token expired and cannot refresh in production")
                    
                except Exception as e:
                    logging.error(f"Error with credentials from environment: {e}")
                    raise RuntimeError("Gmail credentials setup failed in production")
                    
            elif os.path.exists(GMAIL_CREDENTIALS_FILE):
                # Local development - can do interactive auth
                flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
                
                # Save the credentials for the next run
                with open(GMAIL_TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
            else:
                raise RuntimeError("Gmail credentials not found. Set GMAIL_TOKEN_JSON and GMAIL_CREDENTIALS_JSON environment variables for production.")

    return build('gmail', 'v1', credentials=creds)

def _iter_parts(payload):
    """Yield all parts recursively."""
    if 'parts' in payload:
        for part in payload['parts']:
            yield from _iter_parts(part)
    else:
        yield payload

# ----- progress store -----
progress_store = {}
progress_lock = Lock()

def _set_progress(job_id, **kwargs):
    with progress_lock:
        progress_store.setdefault(job_id, {"total": 0, "processed": 0, "skipped": 0, "done": False, "logs": []})
        progress_store[job_id].update(**kwargs)

def count_pdf_attachments(query: str) -> int:
    svc = gmail_service()
    total = 0
    next_page_token = None
    while True:
        kwargs = {'userId': 'me', 'q': query}
        if next_page_token:
            kwargs['pageToken'] = next_page_token
        resp = svc.users().messages().list(**kwargs).execute()
        messages = resp.get('messages', [])
        for msg in messages:
            msg_data = svc.users().messages().get(userId='me', id=msg['id']).execute()
            for part in _iter_parts(msg_data.get('payload', {})):
                fn = (part.get('filename') or '').lower()
                if fn.endswith('.pdf'):
                    total += 1
        next_page_token = resp.get('nextPageToken')
        if not next_page_token:
            break
    return total

def _gmail_worker(job_id: str, query: str):
    try:
        total = count_pdf_attachments(query)
        _set_progress(job_id, total=total)

        svc = gmail_service()
        processed = 0
        skipped = 0
        next_page_token = None

        while True:
            kwargs = {'userId': 'me', 'q': query}
            if next_page_token:
                kwargs['pageToken'] = next_page_token
            resp = svc.users().messages().list(**kwargs).execute()
            messages = resp.get('messages', [])

            for msg in messages:
                msg_data = svc.users().messages().get(userId='me', id=msg['id']).execute()
                found_any = False

                for part in _iter_parts(msg_data.get('payload', {})):
                    filename = part.get('filename') or ''
                    if not filename.lower().endswith('.pdf'):
                        continue

                    data = None
                    body = part.get('body', {})
                    if 'data' in body:
                        data = body['data']
                    elif 'attachmentId' in body:
                        att = svc.users().messages().attachments().get(
                            userId='me', messageId=msg['id'], id=body['attachmentId']
                        ).execute()
                        data = att.get('data')

                    if not data:
                        skipped += 1
                        _set_progress(job_id, skipped=skipped)
                        continue

                    file_bytes = base64.urlsafe_b64decode(data)
                    safe_name = secure_filename(filename)
                    local_path = os.path.join(INBOUND_ATTACH_DIR, safe_name)
                    with open(local_path, 'wb') as f:
                        f.write(file_bytes)

                    ok, _msg = upload_file_by_delivery(local_path)
                    if ok:
                        processed += 1
                    else:
                        skipped += 1
                    found_any = True

                    _set_progress(job_id, processed=processed, skipped=skipped)

                # Note: We no longer mark messages as read since we're processing all emails with attachments

            next_page_token = resp.get('nextPageToken')
            if not next_page_token:
                break

        _set_progress(job_id, done=True)
    except Exception as e:
        logging.exception("Gmail worker failed")
        _set_progress(job_id, done=True)

# ----- async start + poll routes -----
@app.route('/start_check_pod_emails', methods=['POST'])
def start_check_pod_emails():
    if not is_session_valid():
        session.clear()
        return {"error": "unauthorized"}, 401

    job_id = uuid.uuid4().hex
    _set_progress(job_id, total=0, processed=0, skipped=0, done=False, logs=[])
    Thread(target=_gmail_worker, args=(job_id, GMAIL_QUERY), daemon=True).start()
    return {"job_id": job_id}, 202

@app.route('/progress/<job_id>')
def get_progress(job_id):
    with progress_lock:
        data = progress_store.get(job_id)
    if not data:
        return {"error": "unknown job"}, 404

    total = data.get("total", 0)
    processed = data.get("processed", 0)
    skipped = data.get("skipped", 0)
    done = data.get("done", False)

    # If we couldn't pre-count, use (processed+skipped) as the denominator
    denom = total if total else max(1, processed + skipped)
    percent = int(min(100, round(((processed + skipped) / denom) * 100)))
    if done:
        percent = 100

    return {
        "total": total,
        "processed": processed,
        "skipped": skipped,
        "done": done,
        "percent": percent,
    }, 200

# ===================== Smartsheet matching UI flow =====================

@app.route('/smartsheet_match', methods=['GET', 'POST'])
def smartsheet_match():
    """
    POST: compute matches from session['saved_files'], store in session, then redirect (PRG) to GET.
    GET: render the matches currently stored in session.
    """
    global _RESOLVED_WORKSPACE_ID

    if request.method == 'POST':
        if not ss_client:
            flash("Smartsheet is not configured. Set SMARTSHEET_API in your .env.")
            return redirect(url_for('upload_file'))

        saved_files = session.get('saved_files', [])
        delivery_matches = []

        # Resolve the workspace ID once
        if _RESOLVED_WORKSPACE_ID is None:
            _RESOLVED_WORKSPACE_ID = get_workspace_id_by_name(WORKSPACE_NAME)
            logging.info(f"Resolved workspace '{WORKSPACE_NAME}' to ID: {_RESOLVED_WORKSPACE_ID}")
            if _RESOLVED_WORKSPACE_ID is None:
                flash(f"Workspace '{WORKSPACE_NAME}' not found or not shared with this API user.")
                return redirect(url_for('upload_file'))

        now = datetime.now()
        # Support both long and short month names
        month_candidates = [
            now.strftime('%B %Y'), now.strftime('%b %Y'),
            (now.replace(day=1) - timedelta(days=1)).strftime('%B %Y'),
            (now.replace(day=1) - timedelta(days=1)).strftime('%b %Y'),
        ]

        for filename in saved_files:
            delivery_number = extract_delivery_from_filename(filename)
            if not delivery_number:
                continue

            # Try each month sheet until we find a row
            seen_sheets = set()
            for month_name in month_candidates:
                if month_name in seen_sheets:
                    continue
                seen_sheets.add(month_name)

                sheet_id = find_sheet_id_by_name_in_workspace(_RESOLVED_WORKSPACE_ID, month_name)
                if not sheet_id:
                    continue
                row_id = find_row_by_delivery_number(sheet_id, delivery_number)
                if row_id:
                    delivery_matches.append({
                        "delivery_number": delivery_number,
                        "file": filename,
                        "sheet_name": month_name,
                        "sheet_id": sheet_id,
                        "row_id": row_id
                    })
                    break  # stop after first hit for this file

        session['matches'] = delivery_matches
        flash(f"Found {len(delivery_matches)} matching delivery number(s).")
        # Redirect to GET (PRG)
        return redirect(url_for('smartsheet_match'))

    # GET: just render whatever is in session
    delivery_matches = session.get('matches', [])
    return render_template("matches.html", matches=delivery_matches)

@app.route('/upload_match/<sheet_id>/<row_id>/<filename>', methods=['POST'])
def upload_match(sheet_id, row_id, filename):
    if not ss_client:
        flash("Smartsheet is not configured. Set SMARTSHEET_API in your .env.")
        return redirect(url_for('smartsheet_match'))

    file_path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(file_path):
        flash(f"File {filename} not found.")
        return redirect(url_for('smartsheet_match'))

    try:
        with open(file_path, 'rb') as fh:
            ss_client.Attachments.attach_file_to_row(
                int(sheet_id),
                int(row_id),
                (filename, fh, 'application/pdf')
            )
        flash(f"Uploaded {filename} to Smartsheet.")
    except Exception as e:
        logging.exception("Smartsheet upload failed")
        flash(f"Smartsheet upload failed for {filename}: {e}")
    return redirect(url_for('smartsheet_match'))

@app.route('/upload_all_matches', methods=['POST'])
def upload_all_matches():
    """
    Upload every matched file in session['matches'] to its corresponding row.
    """
    if not ss_client:
        flash("Smartsheet is not configured. Set SMARTSHEET_API in your .env.")
        return redirect(url_for('smartsheet_match'))

    matches = session.get('matches', []) or []
    if not matches:
        flash("No matches to upload.")
        return redirect(url_for('smartsheet_match'))

    successes = 0
    failures = 0
    for m in matches:
        filename = m.get('file')
        sheet_id = m.get('sheet_id')
        row_id = m.get('row_id')
        file_path = os.path.join(OUTPUT_FOLDER, filename)

        if not (filename and sheet_id and row_id and os.path.exists(file_path)):
            failures += 1
            continue

        try:
            with open(file_path, 'rb') as fh:
                ss_client.Attachments.attach_file_to_row(
                    int(sheet_id),
                    int(row_id),
                    (filename, fh, 'application/pdf')
                )
            successes += 1
        except Exception as e:
            logging.exception(f"Smartsheet upload failed for {filename}")
            failures += 1

    flash(f"Upload complete: {successes} succeeded, {failures} failed.")
    return redirect(url_for('smartsheet_match'))

# ============================================================================

if __name__ == '__main__':
    app.run(debug=True)




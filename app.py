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

# --- Configuration ---
pytesseract.pytesseract.tesseract_cmd = os.getenv('TESSERACT_CMD', '/usr/local/bin/tesseract')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = 'secret-key'

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

UPLOAD_PASSWORD = os.getenv('UPLOAD_PASSWORD', 'Augerpods#1')

customer_mapping = {
    "crevier lubricants inc":    lambda po, d: f"Crevier {po}.{d}",
    "parkland fuel corporation": lambda _, d: f"Parkland {d}",
    "catalina":                  lambda _, d: f"Catalina {d}",
    "econo gas":                 lambda _, d: f"Econogas {d}",
    "fuel it":                   lambda _, d: f"Fuel It {d}",
    "les petroles belisle":      lambda _, d: f"Belisle {d}",
    "petro montestrie":          lambda _, d: f"Petro Mont {d}",
    "petrole leger":             lambda _, d: f"Leger {d}",
    "rav petroleum":             lambda _, d: f"Rav {d}",
    "st-pierre fuels inc":       lambda _, d: f"Stpierre {d}"
}

# --- OCR & PDF helpers ---
def perform_ocr(page):
    try:
        pix = page.get_pixmap()
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img)
    except Exception as e:
        logging.error(f"OCR error: {e}")
        return ""

def save_page_as_pdf(input_pdf, page_no, name):
    try:
        src = fitz.open(input_pdf)
        dst = fitz.open()
        dst.insert_pdf(src, from_page=page_no, to_page=page_no)
        out_path = os.path.join(OUTPUT_FOLDER, name + ".pdf")
        dst.save(out_path)
        dst.close()
        logging.info(f"Saved page {page_no+1} as {out_path}")
        return True
    except Exception as e:
        logging.error(f"Error saving page {page_no+1}: {e}")
        return False

# --- Extraction logic ---
def extract_po_delivery(text, customer):
    # PO extraction (Crevier)
    po_number = None
    if customer == "crevier lubricants inc":
        m = re.search(r'PO\s*[:#]?\s*([5](?:\s*\d){5,})', text, re.IGNORECASE)
        if m:
            po_number = re.sub(r'\D', '', m.group(1))

    # DELIVERY extraction: improved overlapping regex (no newlines)
    delivery_number = None
    overlaps = [mo.group(1) for mo in re.finditer(r'(?=(9(?:[ \t]*\d){6}))', text)]
    for raw in overlaps:
        norm = re.sub(r'\D', '', raw)
        if len(norm) == 7:
            delivery_number = norm
            logging.info(f"Found delivery number via improved regex: {delivery_number}")
            break

    # fallback manual pattern
    if not delivery_number:
        m2 = re.search(r'(\d{8})-?(\d+)([A-Za-z]{1,2})\b', text)
        if m2:
            delivery_number = f"{m2.group(1)}-{m2.group(2)}{m2.group(3).upper()}"
            logging.info(f"Found manual delivery number: {delivery_number}")

    return po_number, delivery_number


def detect_customer(text):
    tl = text.lower()
    for cust in customer_mapping:
        if fuzz.partial_ratio(cust, tl) >= 80:
            logging.info(f"Detected customer '{cust}'")
            return cust
    return None


def process_pdf(path):
    results = []
    doc = fitz.open(path)
    logging.info(f"Processing '{path}' with {doc.page_count} pages")
    for i in range(doc.page_count):
        page = doc.load_page(i)
        txt = page.get_text().strip()
        if not txt or len(txt) < 20:
            name = f"Unread_{i+1}"
            if save_page_as_pdf(path, i, name):
                results.append(name + ".pdf")
            continue

        cust = detect_customer(txt)
        if not cust:
            logging.info(f"Skipping page {i+1}: readable but no customer match")
            continue

        po, dlv = extract_po_delivery(txt, cust)
        if cust == "crevier lubricants inc":
            base = customer_mapping[cust](po or "", dlv or "")
        else:
            base = customer_mapping[cust]("", dlv or "")

        if not dlv:
            fallback = customer_mapping[cust]("", "").strip().replace(" ", "")
            base = f"{fallback}_{i+1}"

        if save_page_as_pdf(path, i, base):
            results.append(base + ".pdf")

    doc.close()
    return results

# --- Flask routes ---
@app.route('/debug-tesseract')
def debug_tesseract():
    p = os.getenv("PATH", "")
    try:
        which = subprocess.check_output(["which", "tesseract"]).decode().strip()
    except:
        which = "not found"
    return f"PATH={p}\nTesseract={which}"

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == UPLOAD_PASSWORD:
            session['authenticated'] = True
            session['login_time'] = datetime.utcnow().isoformat()
            return redirect(url_for('upload_file'))
        flash('Incorrect password')
    return render_template('login.html')

def is_session_valid():
    lt = session.get('login_time')
    if not session.get('authenticated') or not lt:
        return False
    return (datetime.utcnow() - datetime.fromisoformat(lt)) < timedelta(minutes=15)

@app.route('/', methods=['GET','POST'])
def upload_file():
    if not is_session_valid():
        session.clear()
        return redirect(url_for('login'))
    if request.method == 'POST':
        f = request.files.get('pdf_file')
        if not f or f.filename == '':
            flash('No file selected')
            return redirect(request.url)
        path = os.path.join(UPLOAD_FOLDER, f.filename)
        f.save(path)
        files = process_pdf(path)
        session['saved_files'] = files
        session['login_time'] = datetime.utcnow().isoformat()
        if files:
            return render_template('download.html', saved_files=files)
    return render_template('upload.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

@app.route('/download_all')
def download_all():
    files = session.get('saved_files', [])
    if not files:
        flash("No files to download")
        return redirect(url_for('upload_file'))
    zip_name = "processed.zip"
    zip_path = os.path.join(OUTPUT_FOLDER, zip_name)
    with zipfile.ZipFile(zip_path, 'w') as z:
        for fn in files:
            p = os.path.join(OUTPUT_FOLDER, fn)
            if os.path.exists(p):
                z.write(p, fn)
    return send_file(zip_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)


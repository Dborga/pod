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

UPLOAD_PASSWORD = os.getenv('UPLOAD_PASSWORD', 'Augerpods#1')

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
    "st-pierre fuels inc": lambda _, d: f"Stpierre {d}"
}

def extract_po_delivery(text, customer):
    po_number = None
    if customer == "crevier lubricants inc":
        po_match = re.search(r'PO\s*[:#]?\s*([5](?:\s*\d){5,})', text, re.IGNORECASE)
        if po_match:
            raw = po_match.group(1)
            po_number = re.sub(r'\s+', '', raw)
        else:
            po_match = re.search(r'\b(5(?:\s*\d){5})\b', text)
            if po_match:
                raw = po_match.group(1)
                po_number = re.sub(r'\s+', '', raw)

    delivery_number = None
    delivery_match = re.search(r'(9(?:\s*\d){6})', text)
    if delivery_match:
        raw = delivery_match.group(1)
        delivery_number = re.sub(r'\s+', '', raw)
    else:
        manual_delivery_match = re.search(r'(\d{8})[-]?(\d+)([A-Za-z]{1,2})\b', text)
        if manual_delivery_match:
            date_part = manual_delivery_match.group(1)
            sequence = manual_delivery_match.group(2)
            initials = manual_delivery_match.group(3).upper()
            delivery_number = f"{date_part}-{sequence}{initials}"
            logging.info(f"Found manual delivery number: {delivery_number}")

    return po_number, delivery_number

def detect_customer(text):
    text_lower = text.lower()
    for customer in customer_mapping:
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
            raw_text = page.get_text()
            if not raw_text.strip() or len(raw_text.strip()) < 20:
                text = perform_ocr(page)
                used_ocr = True
            else:
                text = raw_text
                used_ocr = False

            customer = detect_customer(text)
            if customer:
                po_number, delivery_number = extract_po_delivery(text, customer)
                if customer == "crevier lubricants inc":
                    if po_number and delivery_number:
                        filename = customer_mapping[customer](po_number, delivery_number)
                    else:
                        filename = f"Crevier_{page_number + 1}"
                else:
                    if delivery_number:
                        filename = customer_mapping[customer]("", delivery_number)
                    else:
                        fallback = customer_mapping[customer]("", "").strip()
                        filename = f"{fallback}_{page_number + 1}"

                saved = save_page_as_pdf(pdf_path, page_number, filename)
                if saved:
                    saved_files.append(saved)

            else:
                if used_ocr:
                    # Unreadable page -> save as Unread_{n}
                    filename = f"Unread_{page_number + 1}"
                    saved = save_page_as_pdf(pdf_path, page_number, filename)
                    if saved:
                        saved_files.append(saved)
                else:
                    # Readable but customer not on list -> skip
                    logging.info(f"Skipping page {page_number + 1}: readable but no matching customer")
                    continue

        doc.close()
    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
    return saved_files

# ------------------ Authentication & Routes ------------------

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
        file_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(file_path)
        saved_files = process_pdf(file_path)
        session['saved_files'] = saved_files
        session['login_time'] = datetime.utcnow().isoformat()
        if saved_files:
            return render_template('download.html', saved_files=saved_files)
        return redirect(url_for('upload_file'))

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
        for fname in saved_files:
            fpath = os.path.join(OUTPUT_FOLDER, fname)
            if os.path.exists(fpath):
                zipf.write(fpath, fname)
    return send_file(zip_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)


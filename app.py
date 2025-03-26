import os
import logging
import fitz  # PyMuPDF
import regex as re
from flask import Flask, request, redirect, url_for, flash, send_from_directory, render_template, send_file, session
from rapidfuzz import fuzz
import pytesseract
from PIL import Image
import io
import shutil
import zipfile

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = 'secret-key'

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
    "rav petroleum": lambda _, d: f"Rav {d}"
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
    # First try standard delivery numbers (starting with 9)
    delivery_match = re.search(r'(9(?:\s*\d){6})', text)
    if delivery_match:
        raw = delivery_match.group(1)
        delivery_number = re.sub(r'\s+', '', raw)
    else:
        # Try manual delivery number pattern (date followed by sequence and initials)
        # Patterns to match:
        # 01102025-4DB
        # 011020254DB
        # 01102025-4D
        # 011020254D
        manual_delivery_match = re.search(
            r'(\d{8})[-]?(\d+)([A-Za-z]{1,2})\b',
            text
        )
        if manual_delivery_match:
            date_part = manual_delivery_match.group(1)
            sequence = manual_delivery_match.group(2)
            initials = manual_delivery_match.group(3).upper()
            # Reconstruct the delivery number in standard format
            delivery_number = f"{date_part}-{sequence}{initials}"
            logging.info(f"Found manual delivery number: {delivery_number}")

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
            customer = detect_customer(text)
            if customer:
                po_number, delivery_number = extract_po_delivery(text, customer)
                if customer == "crevier lubricants inc" and (not po_number or not delivery_number):
                    continue
                elif customer != "crevier lubricants inc" and not delivery_number:
                    continue
                naming_func = customer_mapping.get(customer)
                output_filename = naming_func(po_number if po_number else "", delivery_number)
                saved = save_page_as_pdf(pdf_path, page_number, output_filename)
                if saved:
                    saved_files.append(saved)
        doc.close()
    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
    return saved_files

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == UPLOAD_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('upload_file'))
        else:
            flash('Incorrect password. Please try again.')
    return render_template('login.html')

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if not session.get('authenticated'):
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
            return render_template('download.html', saved_files=saved_files) if saved_files else redirect(url_for('upload_file'))
    return render_template('upload.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

@app.route('/download_all')
def download_all():
    zip_filename = "processed_files.zip"
    zip_path = os.path.join(OUTPUT_FOLDER, zip_filename)
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file_name in os.listdir(OUTPUT_FOLDER):
            if file_name.endswith(".pdf"):
                zipf.write(os.path.join(OUTPUT_FOLDER, file_name), file_name)
    return send_file(zip_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)


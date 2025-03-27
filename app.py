import os
import logging
import fitz  # PyMuPDF
import regex as re
from flask import Flask, request, redirect, url_for, flash, send_from_directory, render_template, send_file, session, make_response
from datetime import datetime, timedelta
from rapidfuzz import fuzz
import pytesseract
from PIL import Image, ImageOps, ImageFilter
import io
import shutil
import zipfile
import subprocess

pytesseract.pytesseract.tesseract_cmd = os.getenv('TESSERACT_CMD', '/usr/local/bin/tesseract')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = 'secret-key'

# --- Debug Endpoint ---
@app.route('/debug-tesseract')
def debug_tesseract():
    # Get PATH environment variable
    env_path = os.getenv("PATH", "Not set")
    # Try to locate tesseract using the shell command
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

    return po_number, delivery_number

# ---------------------------
# Added synonyms for "Parkland Fuel Corporation" to catch partial or smudged matches
# and still classify as "parkland fuel corporation".
# ---------------------------
def detect_customer(text):
    text_lower = text.lower()

    # Synonyms dictionary to improve detection of smudged or partial "Parkland"
    synonyms = {
        "parkland fuel corporation": [
            "parkland fuel corporation",
            "parkland corp",
            "parkland fuel corp",
            "parkland fuel corp.",
            "parkland"  # plain "parkland" as well
        ],
        "crevier lubricants inc": [
            "crevier lubricants inc"
        ],
        "catalina": ["catalina"],
        "econo gas": ["econo gas"],
        "fuel it": ["fuel it"],
        "les petroles belisle": ["les petroles belisle"],
        "petro montestrie": ["petro montestrie"],
        "petrole leger": ["petrole leger"],
        "rav petroleum": ["rav petroleum"]
    }

    # For each customer in the mapping, check synonyms if available
    for customer in customer_mapping.keys():
        possible_matches = synonyms.get(customer, [customer])
        for pm in possible_matches:
            score = fuzz.partial_ratio(pm, text_lower)
            if score >= 80:
                logging.info(f"Detected customer '{customer}' with score {score} using match '{pm}'")
                return customer
    return None

# ---------------------------
# Added mild preprocessing to enhance OCR results for slightly smudged text or good handwriting.
# ---------------------------
def perform_ocr(page):
    try:
        pix = page.get_pixmap()
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))

        # Convert to grayscale
        img = ImageOps.grayscale(img)

        # Binarize (threshold)
        img = img.point(lambda x: 0 if x < 128 else 255, '1')

        # Convert back to "L" for mild morphological operations
        img = img.convert("L")

        # Apply a mild dilation followed by erosion (or vice versa)
        # This helps reconnect broken letters or remove small noise.
        img = img.filter(ImageFilter.MinFilter(3))  # mild dilation
        img = img.filter(ImageFilter.MaxFilter(3))  # mild erosion

        # Run Tesseract with a standard config
        text_result = pytesseract.image_to_string(img, config="--psm 6")

        return text_result
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

# ------------------ Authentication Changes ------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == UPLOAD_PASSWORD:
            # Set a flag and record login time
            session['authenticated'] = True
            session['login_time'] = datetime.utcnow().isoformat()
            return redirect(url_for('upload_file'))
        else:
            flash('Incorrect password. Please try again.')
    return render_template('login.html')

def is_session_valid():
    """Check if the current session is authenticated and not older than 15 minutes."""
    login_time_str = session.get('login_time')
    if not session.get('authenticated') or not login_time_str:
        return False
    login_time = datetime.fromisoformat(login_time_str)
    if datetime.utcnow() - login_time > timedelta(minutes=15):
        return False
    return True

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    # Require a valid session on every request
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
            # Store the list of files from the current upload in the session
            session['saved_files'] = saved_files
            session['login_time'] = datetime.utcnow().isoformat()
            return render_template('download.html', saved_files=saved_files) if saved_files else redirect(url_for('upload_file'))
    # On GET requests, simply render the upload page without clearing the session.
    return render_template('upload.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

# Updated download_all route to only include files from the current upload session
@app.route('/download_all')
def download_all():
    saved_files = session.get('saved_files', [])
    if not saved_files:
        flash("No recent files available for download.")
        return redirect(url_for('upload_file'))

    zip_filename = "processed_files.zip"
    zip_path = os.path.join(OUTPUT_FOLDER, zip_filename)
    
    # Create the zip file with only the current session's files
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file_name in saved_files:
            file_path = os.path.join(OUTPUT_FOLDER, file_name)
            if os.path.exists(file_path):
                zipf.write(file_path, file_name)
    
    return send_file(zip_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)






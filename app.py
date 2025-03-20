import os
import logging
import fitz  # PyMuPDF
import regex as re  # using the regex module for advanced fuzzy matching
from flask import Flask, request, redirect, url_for, flash, send_from_directory, render_template
from rapidfuzz import fuzz
import pytesseract
from PIL import Image
import io
import pytesseract
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'







# Set up logging to print processing steps to your terminal.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = 'secret-key'  # needed for flash messages

# Directories for uploaded PDFs and output pages
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Mapping for customer naming conventions.
# The target customers are:
#   1. Crevier Lubricants Inc
#   2. Parkland Fuel Corporation
#   3. Catalina
#   4. Econo Gas
#   5. Fuel It
#   6. Les Petroles Belisle
#   7. Petro Montestrie
#   8. Petrole Leger
#   9. Rav Petroleum
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
    """
    Extracts the PO and delivery numbers from the text.

    For Crevier Lubricants Inc:
      - Looks for a PO number that follows a "PO" label (e.g. "PO: 507153" or "PO #507153")
      - Extracts a delivery number: a 7-digit number starting with 9 (allowing spaces)
      
    For other customers, only the delivery number is extracted.
    
    Returns a tuple (po_number, delivery_number). For non-Crevier customers, po_number is returned as None.
    """
    po_number = None
    if customer == "crevier lubricants inc":
        # First, try to capture the PO number that follows a "PO" label.
        po_match = re.search(r'PO\s*[:#]?\s*([5](?:\s*\d){5,})', text, re.IGNORECASE)
        if po_match:
            raw = po_match.group(1)
            po_number = re.sub(r'\s+', '', raw)
        else:
            # Fallback: if no label is found, try to capture any 6-digit number starting with 5.
            po_match = re.search(r'\b(5(?:\s*\d){5})\b', text)
            if po_match:
                raw = po_match.group(1)
                po_number = re.sub(r'\s+', '', raw)
    else:
        # For non-Crevier customers, PO is not required.
        po_number = None

    # Extract Delivery number: a 7-digit number starting with 9, allowing spacing.
    delivery_number = None
    delivery_match = re.search(r'(9(?:\s*\d){6})', text)
    if delivery_match:
        raw = delivery_match.group(1)
        delivery_number = re.sub(r'\s+', '', raw)

    return po_number, delivery_number

def detect_customer(text):
    """
    Uses fuzzy matching (via rapidfuzz) to check if any of the target customer names
    appear in the text. Returns the matching customer key (as in customer_mapping) or
    None if no match is found.
    """
    text_lower = text.lower()
    for customer in customer_mapping.keys():
        score = fuzz.partial_ratio(customer, text_lower)
        if score >= 80:  # threshold (adjustable)
            logging.info(f"Detected customer '{customer}' with score {score}")
            return customer
    return None

def save_page_as_pdf(input_pdf_path, page_number, output_filename):
    """
    Extracts a single page from the input PDF and saves it as a new PDF with the given filename.
    """
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
    """
    Converts a PDF page to an image and extracts text using pytesseract.
    """
    try:
        pix = page.get_pixmap()
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))
        ocr_text = pytesseract.image_to_string(img)
        return ocr_text
    except Exception as e:
        logging.error(f"Error during OCR: {e}")
        return ""

def process_pdf(pdf_path):
    """
    Processes each page of the PDF:
      - Extracts and logs a snippet of the text.
      - Uses fuzzy matching to detect a target customer.
      - Extracts the required numbers based on the customer.
      
    For Crevier, both PO and delivery numbers are required.
    For other customers, only the delivery number is required.
    
    If the required numbers are found, the page is saved as a PDF with a customer-specific filename.
    
    Returns a list of filenames for pages that were saved.
    """
    saved_files = []
    try:
        doc = fitz.open(pdf_path)
        logging.info(f"Processing PDF: {pdf_path} with {doc.page_count} pages.")
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            text = page.get_text()
            logging.info(f"Reading page {page_number + 1}...")

            # If extracted text is insufficient, try OCR.
            if not text.strip() or len(text.strip()) < 20:
                logging.info(f"Page {page_number + 1} has insufficient text, performing OCR...")
                text = perform_ocr(page)
            
            logging.info(f"Page {page_number + 1} snippet: {text[:200]}")  # log first 200 characters

            customer = detect_customer(text)
            if customer:
                po_number, delivery_number = extract_po_delivery(text, customer)
                # For Crevier, require both PO and delivery numbers.
                if customer == "crevier lubricants inc":
                    if not po_number or not delivery_number:
                        logging.info(f"Skipping page {page_number + 1} as required numbers were not found for Crevier.")
                        continue
                else:
                    # For non-Crevier customers, require only the delivery number.
                    if not delivery_number:
                        logging.info(f"Skipping page {page_number + 1} as delivery number was not found for {customer}.")
                        continue

                naming_func = customer_mapping.get(customer)
                # For non-Crevier, we can pass a dummy value (or empty string) for PO.
                if customer == "crevier lubricants inc":
                    output_filename = naming_func(po_number, delivery_number)
                else:
                    output_filename = naming_func("", delivery_number)
                logging.info(f"Extracted for {customer}: Delivery: {delivery_number}" + (f", PO: {po_number}" if po_number else ""))
                saved = save_page_as_pdf(pdf_path, page_number, output_filename)
                if saved:
                    saved_files.append(saved)
            else:
                logging.info(f"No matching customer found on page {page_number + 1}.")
        doc.close()
    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
    return saved_files

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    """
    Renders the modernized HTML form for file upload.
    On POST, saves the PDF, processes it,
    and then returns a page with download links for each processed page.
    """
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
            logging.info(f"Uploaded file saved to {file_path}")
            saved_files = process_pdf(file_path)
            if saved_files:
                # Pass the list of saved files to the download template.
                return render_template('download.html', saved_files=saved_files)
            else:
                flash("No pages were processed for download.")
                return redirect(url_for('upload_file'))
    return render_template('upload.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    """
    Serves the processed PDF file from the outputs folder for download.
    """
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)

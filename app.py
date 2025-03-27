import os
import logging
import fitz  # PyMuPDF
import regex as re
from flask import Flask, request, redirect, url_for, flash, send_from_directory, render_template, send_file, session, make_response
from datetime import datetime, timedelta
from rapidfuzz import fuzz
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
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
    env_path = os.getenv("PATH", "Not set")
    try:
        tesseract_path = subprocess.check_output(["which", "tesseract"]).decode().strip()
    except Exception as e:
        tesseract_path = f"Error: {e}"
    return f"PATH: {env_path}\nTesseract path: {tesseract_path}"

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

UPLOAD_PASSWORD = os.getenv('UPLOAD_PASSWORD', 'Augerpods#1')

# Enhanced customer mapping with more flexible patterns
customer_mapping = {
    r"(parkland|parkl[a-z]*d|p[a-z]*kland|parklnd)": lambda _, d: f"Parkland {d}",
    r"crevier lubricants inc": lambda po, d: f"Crevier {po}.{d}",
    r"catalina": lambda _, d: f"Catalina {d}",
    r"econo gas": lambda _, d: f"Econogas {d}",
    r"fuel it": lambda _, d: f"Fuel It {d}",
    r"les petroles belisle": lambda _, d: f"Belisle {d}",
    r"petro montestrie": lambda _, d: f"Petro Mont {d}",
    r"petrole leger": lambda _, d: f"Leger {d}",
    r"rav petroleum": lambda _, d: f"Rav {d}"
}

def preprocess_image_for_ocr(img):
    """Enhance image quality for better OCR results"""
    try:
        # Convert to grayscale
        img = img.convert('L')
        
        # Enhance contrast
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        # Apply slight sharpening
        img = img.filter(ImageFilter.SHARPEN)
        
        # Apply threshold to remove noise
        img = img.point(lambda x: 0 if x < 140 else 255)
        
        return img
    except Exception as e:
        logging.error(f"Image preprocessing error: {e}")
        return img

def extract_po_delivery(text, customer):
    po_number = None
    if "crevier lubricants" in customer.lower():
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
    # More flexible delivery number patterns
    delivery_match = re.search(r'([9#]\s*(?:\d\s*){6,7})', text)
    if delivery_match:
        raw = delivery_match.group(1)
        delivery_number = re.sub(r'[^\d]', '', raw)
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

def detect_customer(text):
    """Enhanced customer detection with fuzzy matching and pattern recognition"""
    text_lower = text.lower()
    
    # First try exact matches for known variations
    parkland_variations = ["parkland", "parklnd", "parkl d", "parkl and", "parklard"]
    for variation in parkland_variations:
        if variation in text_lower:
            return "parkland"
    
    # Then try regex patterns
    for pattern in customer_mapping.keys():
        if re.search(pattern, text_lower, re.IGNORECASE):
            return pattern
    
    # Finally, try fuzzy matching
    for customer in customer_mapping.keys():
        clean_customer = re.sub(r'[^a-z]', '', customer.lower())
        clean_text = re.sub(r'[^a-z]', '', text_lower)
        score = fuzz.partial_ratio(clean_customer, clean_text)
        if score >= 75:  # Lowered threshold for better matching
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
    """Enhanced OCR with image preprocessing"""
    try:
        pix = page.get_pixmap(dpi=300)  # Higher DPI for better quality
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))
        
        # Preprocess image
        img = preprocess_image_for_ocr(img)
        
        # Use Tesseract with custom configuration for better handwriting recognition
        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/- '
        text = pytesseract.image_to_string(img, config=custom_config)
        
        return text
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
            
            # If text extraction is poor or minimal, use OCR
            if not text.strip() or len(text.strip()) < 20:
                text = perform_ocr(page)
            
            customer = detect_customer(text)
            if customer:
                po_number, delivery_number = extract_po_delivery(text, customer)
                
                # Skip if we don't have required identifiers
                if "crevier" in customer.lower() and (not po_number or not delivery_number):
                    continue
                elif not delivery_number:
                    continue
                
                # Get the naming function (using the first matching pattern)
                naming_func = None
                for pattern in customer_mapping:
                    if re.search(pattern, customer, re.IGNORECASE):
                        naming_func = customer_mapping[pattern]
                        break
                
                if naming_func:
                    output_filename = naming_func(po_number if po_number else "", delivery_number)
                    saved = save_page_as_pdf(pdf_path, page_number, output_filename)
                    if saved:
                        saved_files.append(saved)
        
        doc.close()
    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
    return saved_files

# ... [rest of the Flask routes remain unchanged] ...

if __name__ == '__main__':
    app.run(debug=True)






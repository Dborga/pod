# Use an official Python image as base
FROM python:3.11-slim

# Install system dependencies, including Tesseract OCR
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng libtesseract-dev && \
    rm -rf /var/lib/apt/lists/*


# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port (if your app uses one, e.g., 5000)
EXPOSE 5000

# Command to run your app with gunicorn; adjust as needed
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]


# Use an official Python image as base
FROM python:3.11-slim

# Install system dependencies (Tesseract OCR, curl, and git)
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng libtesseract-dev curl git && \
    rm -rf /var/lib/apt/lists/* && \
    which tesseract && tesseract --version

# Set environment variables for Tesseract
ENV TESSERACT_CMD=/usr/bin/tesseract
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Set the working directory
WORKDIR /app

# Copy the requirements file and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    gunicorn --version

# Copy the rest of your application code into the container
COPY . .

# Copy the entrypoint script to the container root and make it executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose the default port (this is informational)
EXPOSE 5000

# Set the entrypoint to run your script
ENTRYPOINT ["/entrypoint.sh"]














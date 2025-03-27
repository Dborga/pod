# Use an official Python image as base
FROM python:3.11-slim

# Install system dependencies, including Tesseract OCR
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng libtesseract-dev && \
    rm -rf /var/lib/apt/lists/* && \
    which tesseract && tesseract --version

# Set the Tesseract command path and tessdata prefix environment variables
ENV TESSERACT_CMD=/usr/bin/tesseract
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port your app uses (5000)
EXPOSE 5000

# Command to run your app with gunicorn via Poetry
CMD ["poetry", "run", "gunicorn", "--bind", "0.0.0.0:5000", "app:app"]





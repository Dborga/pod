# Use an official Python image as base
FROM python:3.11-slim

# Install system dependencies, including Tesseract OCR and curl (to install Poetry)
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng libtesseract-dev curl && \
    rm -rf /var/lib/apt/lists/* && \
    which tesseract && tesseract --version

# Set the Tesseract command path and tessdata prefix environment variables
ENV TESSERACT_CMD=/usr/bin/tesseract
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Set the working directory to the repository root
WORKDIR /opt/render/project/src

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python - && \
    ln -s /root/.local/bin/poetry /usr/local/bin/poetry

# Copy Poetry configuration files and install dependencies using Poetry
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi && \
    which gunicorn && gunicorn --version

# Copy the rest of your project files into the working directory
COPY . .

# Explicitly copy the entrypoint script to the root directory
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose the default port (informational)
EXPOSE 5000

# Use the entrypoint script as the container's entrypoint
ENTRYPOINT ["/entrypoint.sh"]













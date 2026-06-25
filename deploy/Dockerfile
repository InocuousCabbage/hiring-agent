FROM python:3.12-slim

# Install LibreOffice for DOCX→PDF conversion + Playwright deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN pip install playwright && playwright install chromium --with-deps

COPY . .

# Default: run the agent
CMD ["python", "src/main.py"]

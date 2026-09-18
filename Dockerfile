FROM python:3.12-slim

# LibreOffice - для DOCX -> PDF
# ffmpeg - для видео/аудио конвертации
# tesseract-ocr - для распознавания текста со сканов (рус/укр/англ)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]

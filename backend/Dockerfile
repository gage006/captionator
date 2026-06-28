FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    espeak-ng \
    fonts-liberation \
    fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
# faster-whisper (CTranslate2 backend) replaces torch + openai-whisper: 3-4x faster
# on CPU via AVX2 int8 kernels, and a much smaller image (no torch wheel).
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

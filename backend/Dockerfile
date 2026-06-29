FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    espeak-ng \
    fonts-liberation \
    fonts-freefont-ttf \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
# faster-whisper (CTranslate2 backend) replaces torch + openai-whisper: 3-4x faster
# on CPU via AVX2 int8 kernels, and a much smaller image (no torch wheel).
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run the workload as an unprivileged user. The entrypoint stays root just long
# enough to fix ownership of the mounted volumes, then drops to "app" via gosu —
# so an FFmpeg bug or a malicious upload can't write to the host as root. HOME and
# HF_HOME live under the user's home so the non-root process can populate the
# Whisper model cache (the whisper_cache volume is mounted there).
RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app \
    && mkdir -p /home/app/.cache/huggingface \
    && chown -R app:app /home/app
ENV HOME=/home/app \
    HF_HOME=/home/app/.cache/huggingface

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Clipper Studio — AI Content Factory dashboard
FROM python:3.12-slim

# ffmpeg untuk render/cut, libgl+libglib untuk opencv-python,
# nodejs = runtime JS untuk ekstraksi YouTube oleh yt-dlp
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dashboard.txt requirements-upload.txt ./
RUN pip install --no-cache-dir \
        -r requirements.txt \
        -r requirements-dashboard.txt \
        -r requirements-upload.txt

COPY . .

ENV HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000
CMD ["python", "dashboard.py"]

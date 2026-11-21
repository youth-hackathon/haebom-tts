# 한국어 파일 API, 스트리밍 TTS, Live STT를 같은 포트에서 제공한다.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py gemini.py media.py streaming.py ./
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 TTS_HOST=0.0.0.0 TTS_PORT=8766
EXPOSE 8766
CMD ["python", "main.py"]

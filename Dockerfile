# TTS 서버. 문장을 받아 소리를 돌려준다. 엔진(지금은 espeak-ng)은 이 폴더 안에서만 바꾸면 된다.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY main.py .
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 TTS_HOST=0.0.0.0 TTS_PORT=8766
EXPOSE 8766
CMD ["python", "main.py"]

FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg srt-tools fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY scripts /app/scripts
COPY srtvast /app/srtvast
RUN chmod +x /app/scripts/*.sh

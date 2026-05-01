FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-stems.txt /app/requirements-stems.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements-stems.txt

COPY . /app

ENV STEM_RUNTIME_PYTHON=/usr/local/bin/python
ENV DEMUCS_DEVICE=cpu
ENV DEMUCS_MODEL=htdemucs
ENV DEMUCS_SEGMENT=6
ENV DEMUCS_JOBS=1

EXPOSE 8000

CMD ["python", "server.py"]

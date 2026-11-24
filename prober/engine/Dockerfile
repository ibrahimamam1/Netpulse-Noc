FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config.py anomaly.py sla_tracker.py main.py ./
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 9200
CMD ["python", "main.py"]
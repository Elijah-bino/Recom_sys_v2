# Build from repository root:
#   docker build -f deploy/docker/Dockerfile -t recomsys:v2 .
#
# Image is large (~2–4 GB) due to PyTorch + sentence-transformers.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface \
    CATALOG_PATH=/app/catalog_meta.csv \
    DB_PATH=/app/.data/recomsys.db \
    CACHE_PATH=/app/.cache/item_matrix.npy \
    FRONTEND_DIR=/app/frontend

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY api.py streamlit_app.py recommend.py export_bundle.py update_catalog_tmdb.py /app/
COPY recommender/ /app/recommender/
COPY server/ /app/server/
COPY frontend/ /app/frontend/
COPY catalog_meta.csv /app/catalog_meta.csv

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/.data /app/.cache/huggingface \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM python:3.12-slim

LABEL maintainer="Yash Choubey" \
      description="Trade Message Parser — Hybrid NLP + Deterministic Holdings Engine"

WORKDIR /app

# System deps for scikit-learn / numpy wheel compilation
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY pyproject.toml .
COPY src/ src/
COPY scripts/ scripts/
COPY models/ models/
COPY data/ data/
COPY app/ app/
COPY tests/ tests/

# Create storage directory for SQLite
RUN mkdir -p storage

# Default environment
ENV TRADE_DB_PATH=/app/storage/trade_holdings.db
ENV PYTHONUNBUFFERED=1

# Expose FastAPI port
EXPOSE 8000

# Default: start the FastAPI API server
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]

# Core app image (FastAPI control plane). Pinned to 3.12 — host Python 3.14 has
# no wheels yet for several deps; the container never uses the host interpreter.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Install core deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" \
        "pydantic>=2.7" "pydantic-settings>=2.3" \
        "sqlalchemy>=2.0" "psycopg[binary]>=3.1" \
        "python-dotenv>=1.0" "httpx>=0.27" "phonenumbers>=8.13" \
        "pytest>=8.2"

# RAG layer: pgvector ORM type + local ONNX embeddings (CPU, no GPU).
RUN pip install --no-cache-dir "pgvector>=0.3" "fastembed>=0.4"

# Observability: LangSmith tracing + OpenAI SDK (used as the traced Groq client).
RUN pip install --no-cache-dir "langsmith>=0.1.100" "openai>=1.40"

COPY app ./app
COPY web ./web
COPY scripts ./scripts
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

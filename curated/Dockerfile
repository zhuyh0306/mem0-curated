FROM python:3.12-slim

WORKDIR /app

# Build context is the repository root so the curated services run the forked SDK, not PyPI's.
COPY pyproject.toml README.md LICENSE poetry.lock ./
COPY mem0 ./mem0
RUN pip install --no-cache-dir .

COPY curated/requirements.txt ./curated/requirements.txt
RUN pip install --no-cache-dir -r curated/requirements.txt

COPY curated ./curated

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "curated.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

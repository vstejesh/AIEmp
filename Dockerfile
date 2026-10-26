# Playwright's image ships Chromium and its system dependencies.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
CMD uvicorn centra.web:app --host 0.0.0.0 --port ${PORT}

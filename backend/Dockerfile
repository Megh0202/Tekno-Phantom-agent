FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app/data /app/artifacts

COPY pyproject.toml ./
COPY app ./app
COPY run_server.py ./run_server.py

RUN pip install --no-cache-dir .
# RUN playwright install chromium
RUN playwright install --with-deps chromium

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

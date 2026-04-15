FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    fluxbox \
    novnc \
    websockify \
    x11vnc \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/data /app/artifacts

COPY pyproject.toml ./
COPY app ./app
COPY run_server.py ./run_server.py
COPY start_with_display.sh ./start_with_display.sh

RUN pip install --no-cache-dir .
RUN playwright install --with-deps chromium
RUN chmod +x /app/start_with_display.sh

EXPOSE 8000
EXPOSE 6080

CMD ["/app/start_with_display.sh"]

# Taleward-Server als Container (ohne KI-Pakete – die Transkription machen Worker oder die Cloud).
# Bauen:   docker build -t taleward-server .
# Starten: siehe deploy/docker-compose.yml und install.sh
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    TALEWARD_DOCKER=1

# ffmpeg: Länge von Stimmprofil-Aufnahmen, Audio für die Cloud-Transkription zusammensetzen, Hörproben schneiden
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

WORKDIR /app
# Erst nur die Abhängigkeiten (ändern sich selten → schnelle Updates dank Zwischenspeicher)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev \
 && useradd --system --uid 1000 --create-home taleward \
 && mkdir -p /data /kopplung && chown taleward:taleward /data /kopplung

USER taleward
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"
CMD ["chronik", "serve", "--host", "0.0.0.0", "--port", "8000"]

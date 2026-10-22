FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /app/runtime \
    && chown bot:bot /app/runtime
COPY campus_bot ./campus_bot
COPY data ./data
USER bot
CMD ["python", "-m", "campus_bot"]

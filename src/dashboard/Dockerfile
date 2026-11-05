FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt
COPY app/app.py app/backups.py app/honcho_config.py app/index.html /app/
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]

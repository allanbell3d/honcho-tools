FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt
COPY dream_nas.py /app/dream_nas.py

CMD ["python", "-u", "dream_nas.py"]

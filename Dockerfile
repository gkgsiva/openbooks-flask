FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py irc_client.py db.py library.py queue_worker.py gdrive.py metadata.py ./
COPY templates/ templates/

ENV OPENBOOKS_DOWNLOAD_DIR=/tmp/openbooks

EXPOSE 5000
CMD ["sh", "-c", "exec python app.py"]

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/dispatch.sqlite3 PORT=8080
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts/container_start.py ./container_start.py
RUN useradd --uid 10001 --create-home dispatch && mkdir /data && chown dispatch:dispatch /data
EXPOSE 8080
CMD ["python", "container_start.py"]

FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# gunicorn-free: uvicorn is fine for a single node. Scale horizontally
# behind a load balancer when you need to (SQLite → Postgres at that point).
CMD ["python", "server.py"]

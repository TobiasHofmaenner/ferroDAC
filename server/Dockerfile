FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gen /app/gen
COPY hub /app/hub
COPY tests /app/tests

# the app's own code (/app/hub) and the generated contract (/app/gen) on the path
ENV PYTHONPATH=/app:/app/gen
ENV PYTHONUNBUFFERED=1

EXPOSE 50051
CMD ["python", "-m", "hub.main"]

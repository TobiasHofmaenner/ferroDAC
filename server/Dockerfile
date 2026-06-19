# Build context is the REPO ROOT (see docker-compose.yml: context: ..), so the
# shared, Qt-free `ferrodac` engine sitting next to server/ is reachable by COPY.
FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/gen   /app/gen
COPY server/hub   /app/hub
COPY server/tests /app/tests
# the shared store/net engine — the hub runs the SAME ZarrStore as the app
# (sync target + read tier, §12.1). Qt UI code rides along but is never imported.
COPY ferrodac     /app/ferrodac

# the app's own code (/app/hub, /app/ferrodac) and the generated contract on the path
ENV PYTHONPATH=/app:/app/gen
ENV PYTHONUNBUFFERED=1
# durable Zarr store: the agents' store-and-forward sync target + read tier.
# Mount a volume at /data (docker-compose does) so it survives restarts.
ENV HUB_STORE_DIR=/data/hub.zarr

EXPOSE 50051
CMD ["python", "-m", "hub.main"]

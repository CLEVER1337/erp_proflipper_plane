#!/bin/bash
set -e

# ERP deploy entrypoint: single api-only container (no separate migrator/worker/
# beat/minio). Runs migrations itself, registers/configures the instance, and
# skips create_bucket because file storage is handled in ERP (Postgres bytea).

python manage.py wait_for_db

# Apply migrations in-process (there is no dedicated migrator container).
python manage.py migrate --noinput

# Register + configure the Plane instance (no admin UI is deployed).
HOSTNAME=$(hostname)
MAC_ADDRESS=$(ip link show | awk '/ether/ {print $2}' | head -n 1)
CPU_INFO=$(cat /proc/cpuinfo)
MEMORY_INFO=$(free -h)
DISK_INFO=$(df -h)
SIGNATURE=$(echo "$HOSTNAME$MAC_ADDRESS$CPU_INFO$MEMORY_INFO$DISK_INFO" | sha256sum | awk '{print $1}')
export MACHINE_SIGNATURE=$SIGNATURE
python manage.py register_instance "$MACHINE_SIGNATURE"
python manage.py configure_instance

# NOTE: create_bucket is intentionally skipped — S3 storage is neutralized and
# attachments live in ERP (Postgres bytea).

python manage.py clear_cache

# Idempotent ERP bootstrap: bot user, workspace, gateway token, project + default
# states, employee provisioning + mapping push. Self-heals a fresh erp_plane db;
# a no-op when everything already exists. Guarded so it never blocks startup.
python manage.py erp_bootstrap || true

python manage.py collectstatic --noinput

exec gunicorn -w "${GUNICORN_WORKERS:-1}" -k uvicorn.workers.UvicornWorker \
    plane.asgi:application \
    --bind 0.0.0.0:"${PORT:-8000}" \
    --max-requests 1200 --max-requests-jitter 1000 --access-logfile -

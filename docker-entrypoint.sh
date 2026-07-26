#!/bin/sh
set -eu

python scripts/initialize_database.py --check
exec uvicorn app.main:app --host 0.0.0.0 --port 8080

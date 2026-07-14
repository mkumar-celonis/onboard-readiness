#!/usr/bin/env bash
set -e
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn app.main:app --reload --port "${PORT:-8000}"

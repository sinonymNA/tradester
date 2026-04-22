#!/usr/bin/env bash
set -e

uvicorn bot:app --host 0.0.0.0 --port 8000 &
exec streamlit run app.py --server.address 0.0.0.0 --server.port "${PORT:-8501}" --server.headless true

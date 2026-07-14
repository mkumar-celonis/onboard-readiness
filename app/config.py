"""Env loader — reads .env if present, exposes credentials & runtime knobs."""
import os
from pathlib import Path

def _load_env():
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists(): return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)
_load_env()

JIRA_EMAIL   = os.getenv("JIRA_EMAIL", "")
JIRA_TOKEN   = os.getenv("JIRA_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DD_API_KEY   = os.getenv("DD_API_KEY", "")
DD_APP_KEY   = os.getenv("DD_APP_KEY", "")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "45"))
PORT         = int(os.getenv("PORT", "8000"))

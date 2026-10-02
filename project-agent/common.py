"""Общие функции: конфиг и запросы к «мозгу»."""
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Мозг живёт в локальной сети / Tailscale — системный прокси тут только мешает.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_config(path=None):
    p = Path(path or os.environ.get("PROJECT_AGENT_CONFIG") or HERE / "config.json")
    if not p.exists():
        sys.exit(f"Нет конфига {p}. Скопируй config.example.json в config.json и заполни.")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for key in ("machine", "brain_url", "token"):
        if not cfg.get(key):
            sys.exit(f"В {p} не заполнено поле '{key}'.")
    return cfg


def call_brain(cfg, path, payload=None, timeout=300):
    url = cfg["brain_url"].rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"},
    )
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def utf8_console():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

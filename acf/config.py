"""Loader konfigurasi. Membaca config.yaml dan mengembalikan dict biasa."""
from __future__ import annotations
import os
import yaml

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")


def load_config(path: str | None = None) -> dict:
    path = path or _DEFAULT_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # override via env (dipakai Docker: Ollama jalan di host, bukan di container)
    llm_host = os.environ.get("ACF_LLM_HOST")
    if llm_host:
        cfg.setdefault("llm", {})["host"] = llm_host
    return cfg

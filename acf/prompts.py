"""Prompt agent LLM — default + override pengguna.

Tiga agent memakai prompt teks: Analyzer (pilih highlight), SEO (metadata),
Compliance (cek kebijakan). Default-nya didefinisikan di masing-masing modul
agent (sumber tunggal). Pengguna bisa menimpanya lewat config.yaml:

  prompts:
    analyzer:
      system: "..."
      user: "... {body} ..."

resolve() mengembalikan (system, user) efektif: override bila ada, selain itu
default. Placeholder WAJIB ada di template user agar .format() tidak error.
"""
from __future__ import annotations

# label + placeholder yang boleh dipakai tiap template (untuk UI & validasi).
# 'required' = placeholder yang HARUS ada (kalau hilang, agent tak dapat input).
SPECS = {
    "analyzer": {
        "name": "Analyzer — highlight selection",
        "desc": "Chooses which moments become clips.",
        "vars": "{body}=transcript · {dur}=video length s · {n}=max clips · "
                "{mins}/{maxs}=clip length s",
        "required": ["body"],
        "sample": {"body": "[0.0] sample transcript line", "dur": 600.0,
                   "n": 10, "mins": 12, "maxs": 60},
    },
    "seo": {
        "name": "SEO — titles, descriptions, hashtags",
        "desc": "Writes publish-ready metadata per clip.",
        "vars": "{body}=clip transcript · {lang}=language · {n}=hashtag count",
        "required": ["body"],
        "sample": {"body": "sample clip transcript", "lang": "English", "n": 8},
    },
    "compliance": {
        "name": "Compliance — policy check",
        "desc": "Flags clips that may violate platform guidelines.",
        "vars": "{body}=clip transcript",
        "required": ["body"],
        "sample": {"body": "sample clip transcript"},
    },
}


def _defaults():
    # import lokal agar tak ada import melingkar saat agent memuat modul ini
    from .agents import analyzer, seo, compliance
    return {
        "analyzer": (analyzer.DEFAULT_SYSTEM, analyzer.DEFAULT_USER),
        "seo": (seo.DEFAULT_SYSTEM, seo.DEFAULT_USER),
        "compliance": (compliance.DEFAULT_SYSTEM, compliance.DEFAULT_USER),
    }


def default(key: str) -> tuple[str, str]:
    return _defaults()[key]


def resolve(cfg: dict, key: str) -> tuple[str, str]:
    """(system, user) efektif untuk agent `key`, override config bila ada."""
    dsys, duser = default(key)
    ov = ((cfg or {}).get("prompts") or {}).get(key) or {}
    sys = (ov.get("system") or "").strip() or dsys
    user = (ov.get("user") or "").strip() or duser
    return sys, user


def validate(key: str, system: str, user: str) -> str | None:
    """Kembalikan pesan error bila prompt tak valid, atau None bila OK."""
    spec = SPECS.get(key)
    if not spec:
        return f"unknown agent '{key}'"
    if not (system or "").strip():
        return "system prompt is empty"
    if not (user or "").strip():
        return "user prompt is empty"
    for ph in spec["required"]:
        if "{" + ph + "}" not in user and "{" + ph + ":" not in user:
            return f"user prompt must contain the {{{ph}}} placeholder"
    try:  # pastikan .format() dg nilai contoh tidak error (placeholder asing / spec salah)
        user.format(**spec["sample"])
    except (KeyError, IndexError, ValueError) as e:
        return f"unknown or invalid placeholder in user prompt ({e})"
    return None

"""State machine project — padanan Fase 1 §7.

Urutan status menggambarkan siklus hidup satu project dari masuk sampai selesai.
Manager menaikkan status sebelum menjalankan tiap agent, dan menandai FAILED bila gagal.
"""
from __future__ import annotations
from enum import Enum


class State(str, Enum):
    NEW = "NEW"
    IMPORTING = "IMPORTING"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING = "ANALYZING"
    EDITING = "EDITING"
    SUBTITLING = "SUBTITLING"
    SEO = "SEO"
    THUMBNAIL = "THUMBNAIL"
    QC = "QC"
    COMPLIANCE = "COMPLIANCE"
    UPLOAD = "UPLOAD"
    DONE = "DONE"
    FAILED = "FAILED"


# urutan normal (tanpa FAILED)
ORDER = [
    State.NEW,
    State.IMPORTING,
    State.TRANSCRIBING,
    State.ANALYZING,
    State.EDITING,
    State.SUBTITLING,
    State.SEO,
    State.THUMBNAIL,
    State.QC,
    State.COMPLIANCE,
    State.UPLOAD,
    State.DONE,
]

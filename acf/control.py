"""Kontrol job lintas-thread: STOP & PAUSE kooperatif.

Dashboard men-set/clear Event di sini; pipeline memanggil checkpoint() di titik
aman (antar agent, antar klip, antar segmen). Karena dashboard dan worker
berjalan dalam satu proses Python, Event modul ini dilihat keduanya.

  - STOP  : checkpoint() melempar JobCancelled; subprocess ffmpeg yang sedang
            berjalan langsung di-kill oleh util.ffmpeg.
  - PAUSE : checkpoint() menahan thread sampai resume (langkah berat yang
            sedang berjalan diselesaikan dulu, baru berhenti di titik aman).
"""
from __future__ import annotations
import threading
import time

CANCEL = threading.Event()
PAUSE = threading.Event()


class JobCancelled(Exception):
    """Job dihentikan pengguna — bukan error pipeline."""


def reset() -> None:
    CANCEL.clear()
    PAUSE.clear()


def checkpoint() -> None:
    """Panggil di titik aman: lempar JobCancelled bila stop, tahan bila pause."""
    if CANCEL.is_set():
        raise JobCancelled("dihentikan pengguna")
    while PAUSE.is_set():
        if CANCEL.is_set():
            raise JobCancelled("dihentikan pengguna")
        time.sleep(0.3)


def cancellable_post(url: str, payload: dict, timeout: float):
    """requests.post yang bisa di-STOP di tengah jalan.

    Panggilan LLM (Ollama) bisa berjalan 10-60+ detik di CPU; tanpa ini Stop
    harus menunggu panggilan selesai. Request dijalankan di thread; saat STOP
    kita lempar JobCancelled segera (respons yang sedang berjalan ditinggalkan).
    """
    import threading

    import requests

    box: dict = {}

    def _go():
        try:
            box["r"] = requests.post(url, json=payload, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_go, daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.4)
        if CANCEL.is_set():
            raise JobCancelled("dihentikan pengguna")
    if "e" in box:
        raise box["e"]
    return box["r"]

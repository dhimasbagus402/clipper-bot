"""Helper tipis untuk ffmpeg/ffprobe."""
from __future__ import annotations
import json
import shutil
import subprocess

from .. import control


def ensure_ffmpeg() -> None:
    """Pastikan ffmpeg & ffprobe ada; kalau tidak, error jelas."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"'{tool}' tidak ditemukan di PATH. Pasang ffmpeg dulu "
                f"(Ubuntu: sudo apt install ffmpeg | Windows: unduh dari ffmpeg.org)."
            )


def _run_cancellable(cmd: list[str]) -> tuple[str, str]:
    """Jalankan subprocess; kill segera bila job di-STOP. Return (stdout, stderr)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if control.CANCEL.is_set():
                proc.kill()
                proc.communicate()
                raise control.JobCancelled("dihentikan pengguna")
    if proc.returncode != 0:
        raise RuntimeError(
            "Perintah gagal:\n  " + " ".join(cmd) + "\n--- stderr ---\n" + err[-2000:]
        )
    return out, err


def run(cmd: list[str]) -> None:
    """Jalankan perintah; lempar error dengan stderr bila gagal."""
    _run_cancellable(cmd)


def run_capture(cmd: list[str]) -> str:
    """Seperti run(), tapi kembalikan stderr (ffmpeg menulis info filter ke sana)."""
    return _run_cancellable(cmd)[1]


def probe(path: str) -> dict:
    """Kembalikan info media (ffprobe JSON)."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe gagal untuk: " + path + "\n" + proc.stderr[-1000:])
    return json.loads(proc.stdout)


def video_dimensions(path: str) -> tuple[int, int]:
    info = probe(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError("Tidak ada stream video di: " + path)


def duration_seconds(path: str) -> float:
    info = probe(path)
    return float(info.get("format", {}).get("duration", 0.0))

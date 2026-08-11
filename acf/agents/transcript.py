"""Agent 02 — Transcript. Ubah audio video jadi transkrip ber-timestamp per kata.

Engine: faster-whisper. Output:
  - transcript/transcript.json  (segmen + kata-level timestamp; dipakai Analyzer & Subtitle)
  - transcript/subtitle.srt     (subtitle penuh, untuk referensi)

KUALITAS transkrip memengaruhi kualitas pemilihan highlight (LLM membaca transkrip ini).
Untuk konten BAHASA INGGRIS: "distil-large-v3" = hampir seakurat large-v3 tapi jauh lebih
cepat. Untuk konten multibahasa (mis. Indonesia): pakai "large-v3" (distil hanya Inggris).
Opsi `batch_size` mempercepat lagi lewat batched inference.
"""
from __future__ import annotations
import json

from .base import BaseAgent
from .. import control
from ..util import ffmpeg


class TranscriptAgent(BaseAgent):
    name = "Transcript"

    def run(self, project, ctx: dict) -> None:
        audio_path = project.path("audio", "audio.wav")
        self.log.info("Ekstrak audio -> %s", audio_path)
        ffmpeg.run([
            "ffmpeg", "-y", "-i", project.source_path,
            "-vn", "-ac", "1", "-ar", "16000", audio_path,
        ])

        model = self._load_model()
        tcfg = self.cfg["transcript"]
        lang = tcfg.get("language")
        batch_size = int(tcfg.get("batch_size", 0) or 0)

        self.log.info("Transkripsi (model=%s, batch=%s)...", tcfg["model"],
                      batch_size if batch_size else "off")
        if batch_size > 0:
            from faster_whisper import BatchedInferencePipeline
            pipe = BatchedInferencePipeline(model=model)
            segments, info = pipe.transcribe(
                audio_path, language=lang, word_timestamps=True, batch_size=batch_size)
        else:
            segments, info = model.transcribe(
                audio_path, language=lang, word_timestamps=True, vad_filter=True)

        seg_list = []
        srt_lines = []
        srt_idx = 1
        for seg in segments:
            control.checkpoint()  # stop/pause antar segmen
            words = []
            for w in (seg.words or []):
                words.append({
                    "word": w.word.strip(),
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                })
            seg_list.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
                "words": words,
            })
            srt_lines.append(self._srt_block(srt_idx, seg.start, seg.end, seg.text.strip()))
            srt_idx += 1

        transcript = {
            "language": getattr(info, "language", None),
            "duration": round(float(getattr(info, "duration", 0.0)), 2),
            "segments": seg_list,
        }

        tpath = project.path("transcript", "transcript.json")
        with open(tpath, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, indent=2)

        with open(project.path("transcript", "subtitle.srt"), "w", encoding="utf-8") as f:
            f.write("\n".join(srt_lines))

        ctx["transcript"] = transcript
        self.log.info("Transkrip selesai: %d segmen, bahasa=%s",
                      len(seg_list), transcript["language"])

    # ---- helpers ----
    def _load_model(self):
        from faster_whisper import WhisperModel  # import di sini agar paket lain tetap ringan
        tcfg = self.cfg["transcript"]
        device = tcfg.get("device", "auto")
        size = tcfg["model"]
        threads = int(tcfg.get("cpu_threads", 0) or 0)
        if device in ("auto", "cuda"):
            try:
                m = WhisperModel(size, device="cuda", compute_type="float16")
                self.log.info("Whisper memakai GPU (cuda).")
                return m
            except Exception as e:  # noqa: BLE001
                if device == "cuda":
                    raise
                # NB: kartu AMD (mis. RX 6700 XT) memang tak punya CUDA -> ini normal, bukan error.
                self.log.info("CUDA tidak ada (wajar utk non-NVIDIA) -> pakai CPU. [%s]",
                              str(e).split(":")[0])
        m = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads)
        self.log.info("Whisper memakai CPU (int8, threads=%s).", threads or "auto")
        return m

    @staticmethod
    def _ts(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int(round((seconds - int(seconds)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _srt_block(self, idx: int, start: float, end: float, text: str) -> str:
        return f"{idx}\n{self._ts(start)} --> {self._ts(end)}\n{text}\n"

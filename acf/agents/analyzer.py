"""Agent 03/06 — Content Analyzer. Pilih momen terbaik dari transkrip via LLM lokal (Ollama).

Output: metadata/highlights.json  -> daftar {start, end, score, reason}

PENTING: Ollama default hanya membaca 2048 token. Untuk video panjang itu memotong
transkrip -> model cuma "melihat" bagian awal. Kita naikkan `num_ctx` dan, bila perlu,
pecah transkrip jadi beberapa chunk. Skor di sini PEMERINGKAT RELATIF, bukan ramalan viral.
"""
from __future__ import annotations
import json

import requests

from .base import BaseAgent
from .. import control, prompts

DEFAULT_SYSTEM = (
    "You are a senior short-form video editor who finds viral-worthy moments in long videos. "
    "You understand what makes a YouTube Short / TikTok / Reel stop the scroll: "
    "a strong hook in the first seconds, emotional peaks, surprising or contrarian statements, "
    "humor, a complete self-contained insight, or a satisfying payoff. "
    "You AVOID intros, outros, throat-clearing, filler, rambling, and moments that only make "
    "sense with missing context. Return STRICT JSON only, no prose."
)

DEFAULT_USER = (
    "Below is a transcript. Each line is formatted as [start_seconds] text.\n"
    "The video is about {dur:.0f} seconds long.\n\n"
    "TASK: Select the {n} BEST standalone moments for short-form clips.\n"
    "Rules:\n"
    "- Each clip must be {mins}-{maxs} seconds long.\n"
    "- Each must START on a strong hook and be a COMPLETE thought (never cut mid-sentence).\n"
    "- SPREAD picks across the WHOLE timeline (beginning, middle, AND end) - do not cluster "
    "everything at the start.\n"
    "- Clips must NOT overlap each other.\n"
    "- Prefer genuinely engaging moments over merely informative ones.\n"
    "- 'score' = how strong/viral the moment is (0-100), used only to rank.\n\n"
    'Return JSON exactly: {{"highlights":[{{"start":<sec>,"end":<sec>,'
    '"score":<0-100>,"reason":"<why this hooks a viewer, short>"}}]}}\n\n'
    "TRANSCRIPT:\n{body}"
)


class AnalyzerAgent(BaseAgent):
    name = "Analyzer"

    def run(self, project, ctx: dict) -> None:
        transcript = ctx.get("transcript")
        if transcript is None:
            with open(project.path("transcript", "transcript.json"), encoding="utf-8") as f:
                transcript = json.load(f)

        segments = transcript["segments"]
        total_dur = transcript.get("duration") or (segments[-1]["end"] if segments else 0)
        lines = [f"[{s['start']:.1f}] {s['text']}" for s in segments if s["text"]]
        chunks = self._chunk(lines, self.cfg["llm"]["chunk_chars"])
        self.log.info("Transkrip dibagi jadi %d chunk untuk dianalisis.", len(chunks))

        ccfg = self.cfg["clips"]
        raw: list[dict] = []
        # minta cukup banyak per chunk supaya total memadai
        import math
        per_chunk = max(3, math.ceil(ccfg["max_clips"] / len(chunks)) + 2)
        for i, body in enumerate(chunks, 1):
            control.checkpoint()  # stop/pause antar chunk LLM
            self.log.info("Analisis chunk %d/%d ...", i, len(chunks))
            got = self._ask(body, per_chunk, ccfg["min_seconds"], ccfg["max_seconds"], total_dur)
            self.log.info("  chunk %d: LLM mengembalikan %d highlight mentah", i, len(got))
            raw.extend(got)

        highlights = self._clean(raw, ccfg, total_dur)
        self.log.info("Highlight: %d mentah -> %d terpilih (target maks %d)",
                      len(raw), len(highlights), ccfg["max_clips"])
        if not highlights:
            fb = self._segment_fallback(project, ccfg)
            if fb:
                highlights = fb
                self.log.info("Transkrip minim (musik/tanpa dialog) -> fallback: "
                              "%d video sumber gabungan dijadikan klip.", len(fb))
        if len(highlights) <= 1 and len(raw) <= 1 and not highlights:
            self.log.warning(
                "Hanya %d highlight. Kalau video jelas punya lebih banyak momen menarik, "
                "coba: naikkan llm.num_ctx, kecilkan llm.chunk_chars, atau pakai model lebih "
                "besar (mis. qwen2.5:14b).", len(highlights))

        out = project.path("metadata", "highlights.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(highlights, f, ensure_ascii=False, indent=2)
        ctx["highlights"] = highlights

    def _segment_fallback(self, project, ccfg: dict) -> list[dict]:
        """Sumber hasil gabungan folder (dashboard menulis <sumber>.segments.json):
        bila LLM tak menemukan momen (konser/musik -> transkrip kosong), tiap
        video ASLI dijadikan kandidat klip apa adanya."""
        import os
        path = project.source_path + ".segments.json"
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                segs = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        mins, maxs = float(ccfg["min_seconds"]), float(ccfg["max_seconds"])
        out = []
        for s in segs[:ccfg["max_clips"]]:
            try:
                start, end = float(s["start"]), float(s["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end - start < max(3.0, mins * 0.5):
                continue  # video asli terlalu pendek untuk jadi klip
            out.append({"start": round(start, 3),
                        "end": round(min(end, start + maxs), 3),
                        "score": 60.0,
                        "reason": f"source video: {s.get('name', 'segment')}"})
        return out

    # ---- LLM call ----
    def _ask(self, body: str, n: int, mins: int, maxs: int, dur: float) -> list[dict]:
        lcfg = self.cfg["llm"]
        sys_prompt, user_prompt = prompts.resolve(self.cfg, "analyzer")
        options = {
            "temperature": lcfg.get("temperature", 0.4),
            "num_ctx": lcfg.get("num_ctx", 8192),   # << kunci: jendela konteks lebih besar
        }
        payload = {
            "model": lcfg["model"],
            "stream": False,
            "format": "json",
            # model "thinking" (qwen3/3.5 dst.) bisa menghabiskan seluruh output
            # untuk reasoning -> content kosong -> 0 highlight. Matikan kecuali
            # llm.think: true di config.
            "think": bool(lcfg.get("think", False)),
            "options": options,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt.format(
                    n=n, mins=mins, maxs=maxs, dur=dur, body=body)},
            ],
        }
        brief = ((self.cfg.get("brief") or {}).get("text") or "").strip()
        if brief:
            payload["messages"][1]["content"] += (
                "\n\nCAMPAIGN BRIEF from the user — respect it when choosing moments "
                "(e.g. required themes, min/max duration, what to avoid):\n" + brief[:3000])
        # coba 3x: model kecil kadang meleset format walau format=json
        for attempt in (1, 2, 3):
            try:
                r = control.cancellable_post(f"{lcfg['host']}/api/chat", payload, 900)
                r.raise_for_status()
                content = r.json()["message"]["content"]
            except requests.RequestException as e:
                raise RuntimeError(
                    f"Gagal menghubungi Ollama di {lcfg['host']}. "
                    f"Pastikan Ollama jalan & model '{lcfg['model']}' sudah di-pull. ({e})"
                ) from e

            items = self._extract_items(content)
            result = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                low = {str(k).lower(): v for k, v in it.items()}
                try:
                    result.append({
                        "start": float(low["start"]),
                        "end": float(low["end"]),
                        "score": float(low.get("score", 50)),
                        "reason": str(low.get("reason", "")).strip(),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if result:
                if attempt > 1:
                    self.log.info("  format LLM benar pada percobaan ke-%d", attempt)
                return result
            payload = dict(payload)
            payload["messages"] = payload["messages"] + [
                {"role": "assistant", "content": str(content)[:1500]},
                {"role": "user", "content":
                 'Your previous reply was NOT in the required format. Return ONLY: '
                 '{"highlights":[{"start":<sec>,"end":<sec>,"score":<0-100>,'
                 '"reason":"..."}]} with real numbers.'},
            ]
        return []

    # ---- helpers ----
    @staticmethod
    def _extract_items(text: str) -> list:
        """Terima {"highlights":[...]} / array telanjang / kunci apa pun berisi list."""
        data = AnalyzerAgent._parse_json(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            low = {str(k).lower(): v for k, v in data.items()}
            for key in ("highlights", "clips", "moments", "results", "segments"):
                if isinstance(low.get(key), list):
                    return low[key]
            for v in data.values():  # kunci tak dikenal tapi isinya list dict
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return v
        return []

    @staticmethod
    def _parse_json(text: str):
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            for op, cl in (("[", "]"), ("{", "}")):
                a, b = text.find(op), text.rfind(cl)
                if a != -1 and b != -1 and b > a:
                    try:
                        return json.loads(text[a:b + 1])
                    except json.JSONDecodeError:
                        continue
            return {}

    @staticmethod
    def _chunk(lines: list[str], budget: int) -> list[str]:
        chunks, cur, size = [], [], 0
        for ln in lines:
            if size + len(ln) > budget and cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks or [""]

    @staticmethod
    def _clean(raw: list[dict], ccfg: dict, total_dur: float | None = None) -> list[dict]:
        mins, maxs = ccfg["min_seconds"], ccfg["max_seconds"]
        norm = []
        for h in raw:
            start, end = h["start"], h["end"]
            if end <= start or start < 0:
                continue
            # LLM kadang mengarang timestamp di luar durasi video -> buang/clamp
            if total_dur and start >= total_dur - 1:
                continue
            if total_dur:
                end = min(end, total_dur)
            if end - start > maxs:
                end = start + maxs
            if end - start < mins:
                end = start + mins
            if total_dur:
                end = min(end, total_dur)
            if end - start < 1.0:
                continue
            norm.append({**h, "start": round(start, 2), "end": round(end, 2)})

        # buang yang sangat tumpang tindih (>60%), pertahankan skor tertinggi
        norm.sort(key=lambda x: x["score"], reverse=True)
        kept: list[dict] = []
        for h in norm:
            if any(_overlap(h, k) > 0.6 for k in kept):
                continue
            kept.append(h)
            if len(kept) >= ccfg["max_clips"]:
                break

        kept.sort(key=lambda x: x["start"])  # kronologis untuk penomoran klip
        return kept


def _overlap(a: dict, b: dict) -> float:
    lo = max(a["start"], b["start"])
    hi = min(a["end"], b["end"])
    inter = max(0.0, hi - lo)
    shortest = min(a["end"] - a["start"], b["end"] - b["start"])
    return inter / shortest if shortest > 0 else 0.0

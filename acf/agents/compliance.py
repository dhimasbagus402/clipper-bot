"""Agent 14/19 — Content Compliance. Saring masalah SEBELUM upload. DUA LAPIS.

LAPIS 1 — Aturan kata (deterministik, cepat, tegas):
  - block_words -> FAIL (tolak otomatis)
  - review_words -> REVIEW (tinjau manusia)
  - judul ALL CAPS -> REVIEW (indikasi clickbait)

LAPIS 2 — Penilaian LLM (Qwen via Ollama, menangkap nuansa):
  - ujaran kebencian, kekerasan, konten seksual, self-harm, tindakan berbahaya,
    misinformasi, klaim medis/finansial berisiko, konten rawan demonetisasi.
  - PENGAMAN: default LLM maksimal REVIEW (tidak menolak sendiri). Ubah lewat
    compliance.llm_can_fail: true bila ingin LLM boleh FAIL.

mode: "wordlist" | "llm" | "both" (default "both"). Status final = tingkat paling parah
dari kedua lapis: PASS < REVIEW < FAIL.

APA YANG *TIDAK* BISA DICEK DI SINI (jujur): hak cipta / lisensi musik. Butuh sidik-jari
audio & database berlisensi (seperti YouTube Content ID) yang tidak ada versi lokalnya.
Jangan anggap klip "aman hak cipta" hanya karena lolos agent ini.
"""
from __future__ import annotations
import json
import re

import requests

from .base import BaseAgent
from .. import control, prompts

_SEVERITY = {"PASS": 0, "REVIEW": 1, "FAIL": 2}

DEFAULT_SYSTEM = (
    "You are a content moderation assistant for social video platforms "
    "(YouTube, TikTok, Instagram). You assess whether a short clip's spoken content "
    "could violate community guidelines or risk demonetization/age-restriction. "
    "Be fair and context-aware; do NOT over-flag ordinary strong opinions or mild language. "
    "Return STRICT JSON only."
)

DEFAULT_USER = (
    "Assess this clip transcript for policy risk. Consider: hate/harassment, violence/threats, "
    "sexual/adult content, self-harm, dangerous or illegal acts, medical or financial claims "
    "that could mislead, and clearly demonetizable content.\n\n"
    "Return JSON exactly: {{\"status\":\"PASS|REVIEW|FAIL\",\"categories\":[\"...\"],"
    "\"reason\":\"<short>\"}}\n"
    "- PASS: nothing concerning.\n"
    "- REVIEW: borderline / context-dependent, a human should check.\n"
    "- FAIL: clearly violates policy.\n\n"
    "TRANSCRIPT:\n{body}"
)


class ComplianceAgent(BaseAgent):
    name = "Compliance"

    def run(self, project, ctx: dict) -> None:
        ccfg = self.cfg.get("compliance", {})
        if not ccfg.get("enabled", True):
            self.log.info("Compliance dimatikan di config, dilewati.")
            return

        transcript = ctx.get("transcript")
        if transcript is None:
            with open(project.path("transcript", "transcript.json"), encoding="utf-8") as f:
                transcript = json.load(f)

        mode = ccfg.get("mode", "both")
        llm_can_fail = bool(ccfg.get("llm_can_fail", False))
        review_words = [w.lower() for w in ccfg.get("review_words", [])]
        block_words = [w.lower() for w in ccfg.get("block_words", [])]

        results = []
        for clip in ctx["clips"]:
            control.checkpoint()  # stop/pause antar klip (tiap klip = panggilan LLM)
            i = clip["idx"]
            text = self._clip_text(transcript, clip)
            issues = []
            status = "PASS"
            categories = []

            # --- LAPIS 1: aturan kata ---
            if mode in ("wordlist", "both"):
                low = text.lower()
                hit_block = [w for w in block_words if _has_word(low, w)]
                hit_review = [w for w in review_words if _has_word(low, w)]
                if hit_block:
                    status = _worse(status, "FAIL")
                    issues.append(f"kata terblokir: {', '.join(hit_block)}")
                if hit_review:
                    status = _worse(status, "REVIEW")
                    issues.append(f"kata sensitif: {', '.join(hit_review)}")
                title = (clip.get("seo") or {}).get("title", "")
                if title and title.isupper() and len(title) > 8:
                    status = _worse(status, "REVIEW")
                    issues.append("judul ALL CAPS (indikasi clickbait)")

            # --- LAPIS 2: penilaian LLM ---
            if mode in ("llm", "both"):
                try:
                    verdict = self._ask_llm(text)
                    llm_status = verdict.get("status", "PASS").upper()
                    if llm_status not in _SEVERITY:
                        llm_status = "REVIEW"
                    if llm_status == "FAIL" and not llm_can_fail:
                        llm_status = "REVIEW"  # pengaman: LLM tak boleh menolak sendiri
                    status = _worse(status, llm_status)
                    cats = verdict.get("categories", []) or []
                    if llm_status != "PASS":
                        categories += cats
                        reason = verdict.get("reason", "").strip()
                        issues.append(f"LLM: {llm_status.lower()}"
                                      + (f" ({', '.join(cats)})" if cats else "")
                                      + (f" - {reason}" if reason else ""))
                except RuntimeError as e:
                    # LLM gagal -> jangan gagalkan pipeline; tandai REVIEW agar aman
                    self.log.warning("Klip %02d: cek LLM gagal (%s) -> REVIEW.", i, e)
                    status = _worse(status, "REVIEW")
                    issues.append("cek LLM tidak tersedia -> perlu tinjau manual")

            clip["compliance_status"] = status
            clip["compliance_issues"] = issues
            results.append({"idx": i, "status": status, "issues": issues,
                            "categories": sorted(set(categories)),
                            "note": "hak cipta/musik TIDAK dicek di sini"})
            self.log.info("Klip %02d: %s %s", i, status,
                          ("- " + "; ".join(issues)) if issues else "")

        with open(project.path("metadata", "compliance.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        ctx["compliance"] = results

    # ---- LLM ----
    def _ask_llm(self, text: str) -> dict:
        lcfg = self.cfg["llm"]
        sys_prompt, user_prompt = prompts.resolve(self.cfg, "compliance")
        content = user_prompt.format(body=text[:4000] or "(no speech)")
        # aturan campaign (brief) ikut dicek: pelanggaran "don't" -> REVIEW/FAIL.
        # Kebijakan platform generik saja tidak menangkap larangan spesifik
        # campaign (mis. dilarang sebut kompetitor, calo, rumor tanggal).
        brief = ((self.cfg.get("brief") or {}).get("text") or "").strip()
        if brief:
            content += (
                "\n\nADDITIONALLY, the user runs this clip under a CAMPAIGN BRIEF. "
                "If the transcript clearly violates one of the brief's explicit "
                "don'ts/prohibitions, set status REVIEW (or FAIL if egregious) and "
                "name the violated rule in 'reason'. Ignore the brief's stylistic "
                "wishes — only hard rules count.\nBRIEF:\n" + brief[:3000])
        payload = {
            "model": lcfg["model"],
            "stream": False,
            "format": "json",
            "think": bool(lcfg.get("think", False)),  # lihat catatan di analyzer._ask
            "options": {"temperature": 0.1, "num_ctx": lcfg.get("num_ctx", 8192)},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": content},
            ],
        }
        # coba 3x: model kecil kadang meleset format walau format=json
        for attempt in (1, 2, 3):
            try:
                r = control.cancellable_post(f"{lcfg['host']}/api/chat", payload, 300)
                r.raise_for_status()
                content = r.json()["message"]["content"]
            except requests.RequestException as e:
                raise RuntimeError(str(e)) from e
            data = _parse_json(content)
            if isinstance(data, dict):
                low = {str(k).lower(): v for k, v in data.items()}
                if "status" in low:
                    if attempt > 1:
                        self.log.info("  format LLM benar pada percobaan ke-%d", attempt)
                    return low
            payload = dict(payload)
            payload["messages"] = payload["messages"] + [
                {"role": "assistant", "content": str(content)[:1200]},
                {"role": "user", "content":
                 'Your previous reply was NOT in the required format. Return ONLY: '
                 '{"status":"PASS|REVIEW|FAIL","categories":["..."],"reason":"<short>"}'},
            ]
        raise RuntimeError("respons LLM tidak sesuai format")

    @staticmethod
    def _clip_text(transcript: dict, clip: dict) -> str:
        s = clip.get("cut_start", clip["start"])
        e = clip.get("cut_end", clip["end"])
        parts = []
        for seg in transcript["segments"]:
            mid = (seg["start"] + seg["end"]) / 2
            if s <= mid <= e:
                parts.append(seg["text"])
        return " ".join(parts).strip()


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b


def _has_word(text: str, word: str) -> bool:
    """Cocokkan sebagai kata utuh agar tidak salah tangkap (mis. 'class' vs 'ass')."""
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                return {}
        return {}

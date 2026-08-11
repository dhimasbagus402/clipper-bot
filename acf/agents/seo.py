"""Agent 07/16 — SEO. Buat metadata siap-publish untuk TIAP klip via LLM lokal (Ollama).

Untuk setiap klip, agent membaca transkrip DI RENTANG WAKTU klip itu, lalu menghasilkan:
  - title      : judul pendek yang memancing rasa penasaran (bukan clickbait)
  - description: 1-2 kalimat
  - hashtags   : daftar tagar relevan
  - keywords   : kata kunci pencarian

Output disimpan ke tiap clip (ctx) dan ke metadata/seo.json. Bahasa mengikuti bahasa
transkrip (bisa dipaksa lewat config seo.language).
"""
from __future__ import annotations
import json
import re

import requests

from .base import BaseAgent
from .. import control, prompts

_LANG_NAME = {"en": "English", "id": "Indonesian", "es": "Spanish",
              "ja": "Japanese", "ko": "Korean", "ar": "Arabic"}

DEFAULT_SYSTEM = (
    "You are a YouTube Shorts / TikTok SEO specialist. "
    "You write metadata that maximizes curiosity and search discoverability WITHOUT clickbait "
    "or false promises. Titles are punchy and natural. Return STRICT JSON only, no prose."
)

DEFAULT_USER = (
    "This is the transcript of ONE short vertical clip. Write publish-ready metadata "
    "for it, in {lang}.\n\n"
    "Requirements:\n"
    "- title: <= 70 characters, hook curiosity, natural (NOT ALL CAPS, no clickbait).\n"
    "- description: 1-2 short sentences summarizing the clip's value.\n"
    "- hashtags: {n} relevant tags, no spaces, mix broad + specific, no '#' symbol.\n"
    "- keywords: 5-8 search keywords/phrases.\n\n"
    'Return JSON exactly: {{"title":"...","description":"...",'
    '"hashtags":["..."],"keywords":["..."]}}\n\n'
    "CLIP TRANSCRIPT:\n{body}"
)


class SEOAgent(BaseAgent):
    name = "SEO"

    def run(self, project, ctx: dict) -> None:
        transcript = ctx.get("transcript")
        if transcript is None:
            with open(project.path("transcript", "transcript.json"), encoding="utf-8") as f:
                transcript = json.load(f)

        scfg = self.cfg.get("seo", {})
        lang_cfg = scfg.get("language", "auto")
        lang_code = transcript.get("language") if lang_cfg == "auto" else lang_cfg
        lang_name = _LANG_NAME.get(lang_code, lang_code or "the video's language")
        n_tags = int(scfg.get("max_hashtags", scfg.get("hashtags", 5)))

        seo_all = []
        for clip in ctx["clips"]:
            control.checkpoint()  # stop/pause antar klip (tiap klip = panggilan LLM)
            i = clip["idx"]
            body = self._clip_text(transcript, clip)
            try:
                seo = self._ask(body, lang_name, n_tags)
            except RuntimeError as e:
                self.log.warning("Klip %02d: SEO gagal (%s) -> pakai fallback.", i, e)
                seo = self._fallback(clip)
            clip["seo"] = seo
            seo_all.append({"idx": i, **seo})
            self.log.info("Klip %02d: \"%s\"", i, seo.get("title", ""))

            with open(project.path("metadata", f"clip{i:02d}.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "idx": i, "start": clip["start"], "end": clip["end"],
                    "score": clip["score"], "reason": clip.get("reason", ""),
                    "render_path": clip.get("render_path"),
                    "seo": seo,
                }, f, ensure_ascii=False, indent=2)

        with open(project.path("metadata", "seo.json"), "w", encoding="utf-8") as f:
            json.dump(seo_all, f, ensure_ascii=False, indent=2)
        ctx["seo"] = seo_all

    # ---- LLM ----
    def _ask(self, body: str, lang_name: str, n_tags: int) -> dict:
        lcfg = self.cfg["llm"]
        sys_prompt, user_prompt = prompts.resolve(self.cfg, "seo")
        payload = {
            "model": lcfg["model"],
            "stream": False,
            "format": "json",
            "think": bool(lcfg.get("think", False)),  # lihat catatan di analyzer._ask
            "options": {"temperature": 0.6, "num_ctx": lcfg.get("num_ctx", 8192)},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt.format(
                    lang=lang_name, n=n_tags, body=body)},
            ],
        }
        brief = ((self.cfg.get("brief") or {}).get("text") or "").strip()
        if brief:
            payload["messages"][1]["content"] += (
                "\n\nCAMPAIGN BRIEF from the user — follow it for title style, wording, "
                "and REQUIRED hashtags (include them in 'hashtags'):\n" + brief[:3000])
        # model kecil kadang meleset format walau format=json -> coba 3x;
        # tiap kegagalan diberi pesan koreksi eksplisit.
        last = ""
        for attempt in (1, 2, 3):
            try:
                r = control.cancellable_post(f"{lcfg['host']}/api/chat", payload, 300)
                r.raise_for_status()
                content = r.json()["message"]["content"]
            except requests.RequestException as e:
                raise RuntimeError(str(e)) from e
            last = content
            data = _extract_meta(_parse_json(content))
            if data is not None:
                if attempt > 1:
                    self.log.info("   format LLM benar pada percobaan ke-%d", attempt)
                return {
                    "title": str(data.get("title", "")).strip()[:100],
                    "description": str(data.get("description", "")).strip(),
                    "hashtags": [str(h).lstrip("#").strip()
                                 for h in (data.get("hashtags") or [])][:n_tags],
                    "keywords": [str(k).strip() for k in (data.get("keywords") or [])][:8],
                }
            payload = dict(payload)
            payload["messages"] = payload["messages"] + [
                {"role": "assistant", "content": str(content)[:1500]},
                {"role": "user", "content":
                 'Your previous reply was NOT in the required format. Return ONLY this '
                 'JSON object, nothing else: {"title":"...","description":"...",'
                 '"hashtags":["..."],"keywords":["..."]}'},
            ]
        # penyelamatan terakhir: pungut "title"/"hashtags" via regex dari teks mentah
        salv = _salvage(last)
        if salv:
            self.log.info("   metadata diselamatkan dari respons tak-terstruktur")
            return salv
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
        text = " ".join(parts).strip()
        return text[:4000] if text else "(no speech detected)"

    @staticmethod
    def _fallback(clip: dict) -> dict:
        return {
            "title": (clip.get("reason") or "Highlight")[:70],
            "description": clip.get("reason", ""),
            "hashtags": ["shorts", "viral", "fyp"],
            "keywords": [],
        }


def _extract_meta(data):
    """Cari dict metadata di kedalaman berapa pun (kunci case-insensitive).
    Menerima {"title":...}, {"metadata":{...}}, [{...}], dll."""
    if isinstance(data, dict):
        low = {str(k).lower(): v for k, v in data.items()}
        if "title" in low:
            return low
        for v in data.values():
            got = _extract_meta(v)
            if got is not None:
                return got
    elif isinstance(data, list):
        for v in data:
            got = _extract_meta(v)
            if got is not None:
                return got
    return None


def _salvage(text: str) -> dict | None:
    """Pungut title/description/hashtags via regex dari respons yg gagal di-parse
    (mis. JSON terpotong). None bila tak ada title yang bisa diambil."""
    if not text:
        return None
    mt = re.search(r'"title"\s*:\s*"([^"]{3,150})"', text, re.I)
    if not mt:
        return None
    md = re.search(r'"description"\s*:\s*"([^"]{0,400})"', text, re.I)
    tags = []
    mh = re.search(r'"hashtags"\s*:\s*\[(.*?)(?:\]|$)', text, re.S)
    if mh:
        tags = [t.lstrip("#").strip() for t in re.findall(r'"([^"]{1,40})"', mh.group(1))]
    return {
        "title": mt.group(1).strip()[:100],
        "description": md.group(1).strip() if md else "",
        "hashtags": tags[:8],
        "keywords": [],
    }


def _parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                return {}
        return {}

"""Agent 05/10 — Subtitle Designer. Buat subtitle karaoke (ASS) lalu bakar ke klip.

Memakai timestamp per-kata dari transkrip. Kata dikelompokkan jadi baris pendek
(maks `words_per_line`), tiap kata diberi tag \\k (durasi centidetik) sehingga kata
"menyala" mengikuti ucapan. Hasil akhir: render/clipNN.mp4
"""
from __future__ import annotations
import json
import os

from .base import BaseAgent
from .. import control
from ..util import ffmpeg

# posisi overlay gambar: (x, y) memakai W,H (video) dan w,h (watermark)
_IMG_POS = {
    "top-left": "{m}:{m}",
    "top-center": "(W-w)/2:{m}",
    "top-right": "W-w-{m}:{m}",
    "center": "(W-w)/2:(H-h)/2",
    "bottom-left": "{m}:H-h-{m}",
    "bottom-center": "(W-w)/2:H-h-{m}",
    "bottom-right": "W-w-{m}:H-h-{m}",
}
# posisi drawtext: memakai w,h (video) dan text_w,text_h (teks)
_TXT_POS = {
    "top-left": "x={m}:y={m}",
    "top-center": "x=(w-text_w)/2:y={m}",
    "top-right": "x=w-text_w-{m}:y={m}",
    "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    "bottom-left": "x={m}:y=h-text_h-{m}",
    "bottom-center": "x=(w-text_w)/2:y=h-text_h-{m}",
    "bottom-right": "x=w-text_w-{m}:y=h-text_h-{m}",
}


class SubtitleAgent(BaseAgent):
    name = "Subtitle"

    def run(self, project, ctx: dict) -> None:
        transcript = ctx.get("transcript")
        if transcript is None:
            with open(project.path("transcript", "transcript.json"), encoding="utf-8") as f:
                transcript = json.load(f)
        words = self._flatten_words(transcript)

        scfg = self.cfg["subtitle"]
        rcfg = self.cfg["render"]
        wms = self._watermarks(project, rcfg)  # [] bila mati/tak valid
        for clip in ctx["clips"]:
            control.checkpoint()  # stop/pause antar klip
            i = clip["idx"]
            cs, ce = clip["cut_start"], clip["cut_end"]
            kept = clip.get("kept_intervals")
            if kept:
                # editor membuang jeda hening -> petakan ulang waktu kata ke
                # timeline klip terpotong; kata di dalam jeda dibuang.
                clip_words = []
                for w in words:
                    if w["start"] < cs or w["end"] > ce:
                        continue
                    rs, re_ = w["start"] - cs, w["end"] - cs      # waktu klip-mentah
                    if _in_gap((rs + re_) / 2, kept):
                        continue
                    ms, me = _map_time(rs, kept), _map_time(re_, kept)
                    if me <= ms:
                        me = ms + 0.08
                    clip_words.append({"word": w["word"], "start": ms, "end": me})
            else:
                clip_words = [
                    {"word": w["word"], "start": w["start"] - cs, "end": w["end"] - cs}
                    for w in words if w["start"] >= cs and w["end"] <= ce
                ]
            ass_path = project.path("subtitle", f"clip{i:02d}.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(self._build_ass(clip_words, scfg, rcfg))

            render = project.path("render", f"clip{i:02d}.mp4")
            self.log.info("Klip %02d: bakar subtitle%s -> %s", i,
                          f" + {len(wms)} watermark" if wms else "", render)
            # ffmpeg butuh path ASS yang di-escape; pakai filter 'ass'
            ass_arg = ass_path.replace("\\", "/").replace(":", r"\:")
            base = f"ass='{ass_arg}'"
            cmd = ["ffmpeg", "-y", "-i", clip["vertical_path"]]
            if wms:
                # subtitle dulu, lalu rangkaian overlay/drawtext watermark
                inputs, graph = wm_filter(wms, "b", "v")
                for p in inputs:
                    cmd += ["-i", p]
                cmd += ["-filter_complex", f"[0:v]{base}[b];{graph}",
                        "-map", "[v]", "-map", "0:a?"]
            else:
                cmd += ["-vf", base]
            cmd += ["-c:v", rcfg["video_codec"], "-preset", rcfg["preset"],
                    "-crf", str(rcfg["crf"]), "-c:a", "copy", render]
            ffmpeg.run(cmd)
            clip["render_path"] = render

    # ---- watermark ----
    def _watermarks(self, project, rcfg: dict) -> list[dict]:
        """Siapkan filter watermark sekali. [] bila mati / tak ada yang valid."""
        wms, warns = build_watermarks(self.cfg.get("watermark") or {}, rcfg,
                                      project.path("subtitle"))
        for warn in warns:
            self.log.warning("%s", warn)
        return wms

    # ---- ASS builder ----
    def _build_ass(self, words: list[dict], scfg: dict, rcfg: dict) -> str:
        bold = -1 if scfg.get("bold", True) else 0
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {rcfg['width']}\n"
            f"PlayResY: {rcfg['height']}\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Default,{scfg['font']},{scfg['fontsize']},{scfg['primary_color']},"
            f"{scfg['secondary_color']},{scfg['outline_color']},&H00000000,{bold},0,0,0,"
            f"100,100,0,0,1,{scfg['outline']},0,2,40,40,{scfg['margin_v']},1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n"
        )
        lines = []
        wpl = scfg["words_per_line"]
        for group in _groups(words, wpl):
            if not group:
                continue
            start = group[0]["start"]
            end = group[-1]["end"]
            text = "".join(
                f"{{\\k{max(1, int(round((w['end'] - w['start']) * 100)))}}}{w['word']} "
                for w in group
            ).strip()
            lines.append(
                f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text}"
            )
        return header + "\n".join(lines) + "\n"

    @staticmethod
    def _flatten_words(transcript: dict) -> list[dict]:
        out = []
        for seg in transcript["segments"]:
            for w in seg.get("words", []):
                if w.get("word"):
                    out.append(w)
        return out


def build_watermarks(wcfg: dict, rcfg: dict,
                     txt_dir: str) -> tuple[list[dict], list[str]]:
    """Bangun SEMUA watermark aktif dari config. Return (list wm, peringatan).

    Config baru: wcfg['items'] = daftar item (masing-masing punya type/path/
    text/position/x/y/scale/opacity...). Field level-atas menjadi DEFAULT untuk
    tiap item. Config lama (tanpa 'items') = satu item dari field level-atas.
    Bentuk wm:
      image -> {'mode':'image','path':..,'chain':..,'xy':..}   (chain TANPA label input)
      text  -> {'mode':'text','draw':..}
    Dipakai SubtitleAgent + preview/worker dashboard — satu sumber kebenaran.
    """
    if not wcfg.get("enabled"):
        return [], []
    items = wcfg.get("items")
    if not isinstance(items, list):
        # format lama: field level-atas adalah satu-satunya watermark
        items = [{}] if (wcfg.get("path") or wcfg.get("text")) else []
    defaults = {k: v for k, v in wcfg.items() if k not in ("items", "enabled")}
    wms, warns = [], []
    for idx, it in enumerate(items):
        merged = {**defaults, **(it if isinstance(it, dict) else {})}
        wm, warn = _build_one(merged, rcfg, txt_dir, idx)
        if warn:
            warns.append(f"Watermark #{idx + 1}: {warn}")
        if wm:
            wms.append(wm)
    return wms, warns


def wm_filter(wms: list[dict], in_label: str, out_label: str,
              first_input: int = 1) -> tuple[list[str], str]:
    """Rangkai overlay/drawtext berantai untuk beberapa watermark.

    Return (daftar path input gambar tambahan, potongan filtergraph dari
    [in_label] ke [out_label]). Gambar jadi input ffmpeg first_input..N.
    """
    inputs: list[str] = []
    parts: list[str] = []
    cur = in_label
    for k, w in enumerate(wms):
        nxt = out_label if k == len(wms) - 1 else f"wmc{k}"
        if w["mode"] == "image":
            idx = first_input + len(inputs)
            inputs.append(w["path"])
            parts.append(f"[{idx}:v]{w['chain']}[wm{k}]")
            parts.append(f"[{cur}][wm{k}]overlay={w['xy']}[{nxt}]")
        else:
            parts.append(f"[{cur}]{w['draw']}[{nxt}]")
        cur = nxt
    return inputs, ";".join(parts)


def _build_one(wcfg: dict, rcfg: dict, txt_dir: str,
               idx: int = 0) -> tuple[dict | None, str]:
    """Bangun satu watermark. position 'custom' memakai x/y (fraksi 0-1,
    PUSAT watermark, di-clamp agar tidak keluar frame)."""
    warn = ""
    pos = str(wcfg.get("position", "top-center")).lower()
    if pos not in _IMG_POS and pos != "custom":
        warn = f"position '{pos}' tak dikenal -> top-center."
        pos = "top-center"
    m = max(0, int(wcfg.get("margin", 60)))
    opacity = max(0.0, min(1.0, float(wcfg.get("opacity", 0.9))))
    if pos == "custom":
        xf = max(0.0, min(1.0, float(wcfg.get("x", 0.5))))
        yf = max(0.0, min(1.0, float(wcfg.get("y", 0.1))))
        # pusat watermark di (xf,yf); kutip agar koma min()/max() aman di filtergraph
        img_xy = (f"'min(max(W*{xf:.4f}-w/2,0),W-w)':"
                  f"'min(max(H*{yf:.4f}-h/2,0),H-h)'")
        txt_pos = (f"x='min(max(w*{xf:.4f}-text_w/2,0),w-text_w)':"
                   f"y='min(max(h*{yf:.4f}-text_h/2,0),h-text_h)'")
    else:
        img_xy = _IMG_POS[pos].format(m=m)
        txt_pos = _TXT_POS[pos].format(m=m)

    if str(wcfg.get("type", "image")).lower() == "image":
        path = str(wcfg.get("path", "")).strip()
        if not path or not os.path.isfile(path):
            return None, f"image tak ditemukan ({path!r}) -> dilewati."
        scale = max(0.02, min(1.0, float(wcfg.get("scale", 0.4))))
        wm_w = _even(max(2, int(rcfg["width"] * scale)))
        # skala logo, jaga aspek; beri opasitas (label input diberi wm_filter)
        chain = (f"scale={wm_w}:-1,format=rgba,"
                 f"colorchannelmixer=aa={opacity:.3f}")
        return {"mode": "image", "path": path, "chain": chain, "xy": img_xy}, warn

    # ---- text ----
    text = str(wcfg.get("text", "")).strip()
    if not text:
        return None, "type=text tapi 'text' kosong -> dilewati."
    ff = str(wcfg.get("font_path", "")).strip() or "C:/Windows/Fonts/arial.ttf"
    ff_arg = ff.replace("\\", "/").replace(":", r"\:")
    # tulis teks ke file -> hindari neraka escaping di filtergraph
    os.makedirs(txt_dir, exist_ok=True)
    txt_file = os.path.join(txt_dir, f"_watermark{idx}.txt")
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(text)
    tf_arg = txt_file.replace("\\", "/").replace(":", r"\:")
    fs = int(wcfg.get("font_size", 96))
    color = str(wcfg.get("font_color", "white"))
    draw = (f"drawtext=fontfile='{ff_arg}':textfile='{tf_arg}':"
            f"fontsize={fs}:fontcolor={color}:{txt_pos}"
            f":alpha={opacity:.3f}")
    if wcfg.get("box", True):
        draw += (f":box=1:boxcolor={wcfg.get('box_color', 'black@0.4')}"
                 f":boxborderw={max(4, fs // 6)}")
    return {"mode": "text", "draw": draw}, warn


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def _in_gap(rt: float, kept: list) -> bool:
    """True bila waktu klip-mentah rt jatuh di jeda yang dibuang."""
    return not any(s <= rt <= e for s, e in kept)


def _map_time(rt: float, kept: list) -> float:
    """Petakan waktu klip-mentah -> waktu klip terpotong (clamp ke segmen kept)."""
    off = 0.0
    for s, e in kept:
        if rt <= s:
            return off
        if rt <= e:
            return off + (rt - s)
        off += (e - s)
    return off


def _groups(words: list[dict], n: int):
    for i in range(0, len(words), n):
        yield words[i:i + n]


def _ts(seconds: float) -> str:
    """Format ASS: H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

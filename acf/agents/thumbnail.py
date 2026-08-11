"""Agent 08/17 — Thumbnail. Pilih frame terbaik tiap klip lalu tempel judul.

Untuk tiap klip: ambil beberapa sampel frame, skor berdasarkan ketajaman, kecerahan,
dan ada/tidaknya wajah, pilih yang terbaik, lalu tempelkan judul (dari SEO Agent) dengan
outline agar mudah dibaca. Output: thumbnail/clipNN.png

Catatan: untuk YouTube Shorts, thumbnail custom dampaknya kecil (Shorts jarang menampilkannya
seperti video biasa). Fitur ini lebih berguna untuk platform lain / versi horizontal.
"""
from __future__ import annotations
import os

from .base import BaseAgent
from .. import control


class ThumbnailAgent(BaseAgent):
    name = "Thumbnail"

    def run(self, project, ctx: dict) -> None:
        if not self.cfg.get("thumbnail", {}).get("enabled", True):
            self.log.info("Thumbnail dimatikan di config, dilewati.")
            return
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.log.warning("OpenCV tak terpasang -> thumbnail dilewati.")
            return
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.log.warning("Pillow tak terpasang (pip install Pillow) -> thumbnail dilewati.")
            return

        tcfg = self.cfg.get("thumbnail", {})
        for clip in ctx["clips"]:
            control.checkpoint()  # stop/pause antar klip
            i = clip["idx"]
            try:
                src = clip.get("vertical_path") or clip.get("render_path")
                if not src or not os.path.isfile(src):
                    self.log.warning("Klip %02d: sumber frame tidak ada, thumbnail dilewati.", i)
                    continue
                frame = self._best_frame(src)
                if frame is None:
                    self.log.warning("Klip %02d: gagal ambil frame.", i)
                    continue
                title = (clip.get("seo") or {}).get("title", "")
                out = project.path("thumbnail", f"clip{i:02d}.png")
                self._render(frame, title, out, tcfg)
                clip["thumbnail_path"] = out
                self.log.info("Klip %02d: thumbnail -> %s", i, out)
            except Exception as e:  # noqa: BLE001
                self.log.warning("Klip %02d: thumbnail gagal (%s) -> dilewati.", i, e)

    # ---- pemilihan frame ----
    def _best_frame(self, path: str):
        import cv2
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        cascade = cv2.CascadeClassifier(
            os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, total // 25) if total else 15
        best, best_score, idx = None, -1.0, 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
                bright = float(gray.mean())
                faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(80, 80))
                score = sharp * (1.0 if 45 < bright < 225 else 0.3)
                if len(faces) > 0:
                    score += 5000  # utamakan frame ber-wajah
                if score > best_score:
                    best_score, best = score, frame.copy()
            idx += 1
        cap.release()
        return best

    # ---- overlay teks ----
    def _render(self, frame_bgr, title: str, out: str, tcfg: dict) -> None:
        import cv2
        from PIL import Image, ImageDraw
        img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        W, H = img.size
        draw = ImageDraw.Draw(img)
        font = self._load_font(tcfg.get("font_path", ""), tcfg.get("font_size", 90))

        if title:
            lines = self._wrap(draw, title.upper(), font, int(W * 0.9))
            line_h = self._line_height(draw, font)
            total_h = line_h * len(lines)
            y = int(H * 0.06)
            for ln in lines:
                w = self._text_w(draw, ln, font)
                x = (W - w) // 2
                # outline hitam
                for dx in (-3, 0, 3):
                    for dy in (-3, 0, 3):
                        draw.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0))
                draw.text((x, y), ln, font=font, fill=(255, 255, 255))
                y += line_h
        img.save(out)

    @staticmethod
    def _load_font(font_path: str, size: int):
        from PIL import ImageFont
        candidates = [font_path] if font_path else []
        candidates += ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                       "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
        for c in candidates:
            if not c:
                continue
            try:
                return ImageFont.truetype(c, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    @staticmethod
    def _text_w(draw, text: str, font) -> int:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    @staticmethod
    def _line_height(draw, font) -> int:
        box = draw.textbbox((0, 0), "Ag", font=font)
        return int((box[3] - box[1]) * 1.35)

    def _wrap(self, draw, text: str, font, max_w: int) -> list[str]:
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if self._text_w(draw, test, font) <= max_w or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines[:3]  # maks 3 baris

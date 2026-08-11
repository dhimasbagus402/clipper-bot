"""Agent 08/09 — Clip Generator + Video Editor.

Untuk tiap highlight:
  1) Potong klip mentah dari sumber (dengan buffer) -> work/clipNN_raw.mp4
  2) Deteksi perpindahan kamera (scene cut) -> bagi klip menjadi shot
  3) Analisis wajah PER SHOT (OpenCV): posisi + "energi bicara" -> siapa yg bicara
  4) Reframe 9:16 per shot (follow speaker / wide blur)
  5) Post: audio loudnorm, color/sharpen, punch-in (opsional, toggle)

Semua fitur "advanced" default MATI — perilaku sama seperti sebelumnya kecuali
dinyalakan lewat config `editor:` (atau Settings UI):
  detector: haar | yunet          deteksi wajah (yunet lebih akurat)
  speaker_model: visual | av       av = korelasi gerak mulut vs audio
  smooth_pan: false                crop mengikuti gerak dalam-shot (halus)
  saliency_crop: false             shot tanpa wajah -> ikuti area gerak
  audio_normalize: false           loudnorm -14 LUFS (konsisten utk Shorts)
  enhance: false                   eq kontras/saturasi + unsharp
  punch_in: false                  zoom pelan (Ken Burns) untuk energi
"""
from __future__ import annotations
import os
import re

from .base import BaseAgent
from .. import control
from ..util import ffmpeg

_DET_W = 640          # lebar frame untuk deteksi (lebih kecil = lebih cepat)
_PAIR_GAP = 0.12      # jarak (detik) antara dua frame untuk mengukur gerak mulut
_YUNET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "models", "face_detection_yunet_2023mar.onnx")


class EditorAgent(BaseAgent):
    name = "Editor"

    def run(self, project, ctx: dict) -> None:
        highlights = ctx["highlights"]
        ccfg = self.cfg["clips"]
        rcfg = self.cfg["render"]
        ecfg = self.cfg.get("editor") or {}
        buf = ccfg["buffer_seconds"]
        src = project.source_path
        src_dur = ffmpeg.duration_seconds(src)

        gameplay = _flag(ecfg, "gameplay_mode")
        cam_file = self._resolve_cam(src) if gameplay else None
        if gameplay:
            self.log.info("Gameplay mode: %s", f"cam file '{os.path.basename(cam_file)}'"
                          if cam_file else "single-file facecam detect")

        clips = []
        for i, h in enumerate(highlights, 1):
            control.checkpoint()  # stop/pause antar klip
            start = max(0.0, h["start"] - buf)
            end = min(src_dur, h["end"] + buf)
            dur = round(end - start, 3)
            if dur < 1.0 or start >= src_dur:
                # highlight di luar durasi video (LLM mengarang) -> lewati, jangan
                # gagalkan seluruh stage dengan -t negatif.
                self.log.warning("Klip %02d: rentang di luar video (%.1f-%.1f / %ds) "
                                 "-> dilewati.", i, start, end, int(src_dur))
                continue
            raw = project.path("work", f"clip{i:02d}_raw.mp4")
            vert = project.path("work", f"clip{i:02d}_vert.mp4")

            self.log.info("Klip %02d: potong %.2f-%.2f (%.1fs)", i, start, end, dur)
            ffmpeg.run([
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
                "-c:v", rcfg["video_codec"], "-preset", rcfg["preset"],
                "-crf", str(rcfg["crf"]), "-c:a", "aac", "-ar", "44100", raw,
            ])

            kept = None
            if gameplay:
                # susun facecam (atas) + gameplay (bawah) -> 9:16
                self.log.info("Klip %02d: gameplay layout ...", i)
                self._gameplay_layout(raw, cam_file, start, dur, vert, rcfg, ecfg)
            else:
                # buang jeda hening (opsional). kept_intervals dlm waktu klip-mentah.
                reframe_src = raw
                if _flag(ecfg, "trim_silence"):
                    kept = self._plan_keep(raw, dur, ecfg, ccfg["min_seconds"])
                    if kept is not None:
                        trimmed = project.path("work", f"clip{i:02d}_trim.mp4")
                        self._trim_gaps(raw, trimmed, kept, rcfg)
                        reframe_src = trimmed
                        removed = round(dur - sum(e - s for s, e in kept), 1)
                        self.log.info("Klip %02d: buang %.1fs hening (%d segmen)",
                                      i, removed, len(kept))
                self.log.info("Klip %02d: reframe 9:16 ...", i)
                self._reframe(reframe_src, vert, rcfg)
            if _flag(ecfg, "audio_normalize"):
                self._normalize_audio(vert)

            clips.append({
                "idx": i, "start": h["start"], "end": h["end"],
                "cut_start": start, "cut_end": end,
                "kept_intervals": kept,  # None = tak ada trim -> subtitle linear
                "score": h["score"], "reason": h.get("reason", ""),
                "vertical_path": vert,
            })

        ctx["clips"] = clips

    # ================= reframe =================
    def _reframe(self, raw: str, out: str, rcfg: dict) -> None:
        W, H = rcfg["width"], rcfg["height"]
        iw, ih = ffmpeg.video_dimensions(raw)
        target_ratio = W / H
        ecfg = self.cfg.get("editor") or {}

        if iw / ih <= target_ratio:
            # sumber lebih tinggi/sempit -> crop tinggi, pusatkan (tak perlu tracking)
            cw = _even(iw)
            ch = _even(int(round(iw / target_ratio)))
            y = _clamp(int(round((ih - ch) / 2)), 0, max(0, ih - ch))
            vf = f"crop={cw}:{ch}:0:{y},scale={W}:{H},setsar=1"
            self._encode(raw, out, rcfg, vf)
            return

        # sumber lebih lebar -> rencanakan crop per shot
        cw = _even(int(round(ih * target_ratio)))
        ch = _even(ih)

        # zoom < 1.0 = lebih lebar: ambil area lebih luas lalu blur-pad atas/bawah
        zoom = max(0.5, min(1.0, float(ecfg.get("zoom", 1.0))))
        w2 = _even(min(iw, int(round(cw / zoom))))
        zoomed_out = w2 > cw

        def follow_vf(xspec: str) -> str:
            if not zoomed_out:
                return f"crop={cw}:{ch}:x='{xspec}':y=0,scale={W}:{H},setsar=1"
            return (f"crop={w2}:{ih}:x='{xspec}':y=0,"
                    f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},gblur=sigma=24[bg];[b]scale={W}:-2[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")

        dur = ffmpeg.duration_seconds(raw)
        cuts = self._scene_cuts(raw, float(ecfg.get("scene_threshold", 0.30)),
                                float(ecfg.get("min_shot_seconds", 0.6)), dur)
        bounds = [0.0] + cuts + [dur]
        shots = [(bounds[j], bounds[j + 1]) for j in range(len(bounds) - 1)]
        plan = self._plan_shots(raw, shots, iw, w2, ecfg)
        self.log.info("   %d shot: %d follow, %d wide (zoom %.2f, %s)", len(plan),
                      sum(1 for p in plan if p["mode"] == "follow"),
                      sum(1 for p in plan if p["mode"] == "wide"), zoom,
                      str(ecfg.get("detector", "haar")))

        single = all(p["mode"] == "follow" for p in plan) and not _flag(ecfg, "punch_in")
        if single:
            xs = [p["xexpr"] for p in plan]
            if len(set(xs)) == 1:
                expr = xs[0]
            else:
                expr = xs[-1]
                for j in range(len(xs) - 2, -1, -1):
                    expr = f"if(lt(t,{plan[j]['t1']:.3f}),{xs[j]},{expr})"
            self._encode(raw, out, rcfg, follow_vf(expr))
        else:
            self._render_segments(raw, out, rcfg, plan, follow_vf, W, H, ecfg)

    # ================= gameplay mode (facecam + gameplay) =================
    def _resolve_cam(self, src: str) -> str | None:
        """Cari file webcam terpisah: config gameplay.cam_file, atau companion
        <namasumber>.cam.<ext> di folder yang sama."""
        gcfg = self.cfg.get("gameplay") or {}
        p = (gcfg.get("cam_file") or "").strip()
        if p and os.path.isfile(p):
            return p
        stem, _ = os.path.splitext(src)
        for ext in (".mp4", ".mkv", ".mov", ".webm", ".m4v"):
            cand = stem + ".cam" + ext
            if os.path.isfile(cand):
                return cand
        return None

    def _gameplay_layout(self, gameplay_clip: str, cam_file: str | None,
                         seek: float, dur: float, out: str, rcfg: dict,
                         ecfg: dict) -> None:
        """Susun 9:16: facecam di atas, gameplay di bawah (vstack)."""
        W, H = rcfg["width"], rcfg["height"]
        gcfg = self.cfg.get("gameplay") or {}
        split = min(0.6, max(0.15, float(gcfg.get("split", 0.33))))
        top_h = _even(int(round(split * H)))
        bot_h = _even(H - top_h)
        top_h = H - bot_h

        # Panel atas — dua gaya (config gameplay.cam_fit):
        #  fill (default): SKALA-ISI penuh lalu crop; anchor atas (y=0) supaya
        #    kepala—yg selalu di bagian atas webcam—tetap utuh, TANPA celah.
        #  blur: muat kotak utuh di atas latar blur (ada pilar bila kotak potret).
        # Deteksi kotak cam sudah memberi headroom di atas wajah, jadi crop dari
        # atas menyisakan dahi/rambut dan membuang dada bawah.
        cam_fit = str(gcfg.get("cam_fit", "fill")).lower()
        if cam_fit == "blur":
            fill_top = (
                f"split[t1][t2];"
                f"[t1]scale={W}:{top_h}:force_original_aspect_ratio=increase,"
                f"crop={W}:{top_h},gblur=sigma=20[tbg];"
                f"[t2]scale={W}:{top_h}:force_original_aspect_ratio=decrease[tfg];"
                f"[tbg][tfg]overlay=(W-w)/2:(H-h)/2,setsar=1")
        else:
            # crop mengisi penuh, digeser sedikit ke ATAS (0.4 dari slack, bukan
            # 0.5 tengah) supaya kepala/rambut tak terpotong, dada bawah yg dibuang.
            fill_top = (f"scale={W}:{top_h}:force_original_aspect_ratio=increase,"
                        f"crop={W}:{top_h}:(iw-{W})/2:(ih-{top_h})*0.4,setsar=1")
        fill_bot = (f"scale={W}:{bot_h}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{bot_h},setsar=1")

        if cam_file and os.path.isfile(cam_file):
            # dua file: input0 = gameplay clip (0..dur), input1 = cam (seek sama)
            fc = (f"[1:v]{fill_top}[top];[0:v]{fill_bot}[bot];[top][bot]vstack=inputs=2[v]")
            cmd = ["ffmpeg", "-y", "-i", gameplay_clip,
                   "-ss", f"{seek:.3f}", "-t", f"{dur:.3f}", "-i", cam_file,
                   "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
        else:
            # satu file: crop area facecam dari klip itu sendiri
            x, y, w, h, fcx, fcy = self._facecam_region(gameplay_clip, gcfg)
            if cam_fit != "blur":
                # crop beraspek panel yg BENAR-BENAR terpusat di wajah (bukan di
                # tengah kotak yg bisa bergeser saat ke-clamp di sudut).
                iw, ih = ffmpeg.video_dimensions(gameplay_clip)
                A = W / top_h
                cw2 = _even(int(min(iw, w)))
                ch2 = _even(int(min(ih, round(cw2 / A))))
                cw2 = _even(int(min(iw, round(ch2 * A))))
                cx2 = _clamp(_even(int(round(fcx - cw2 / 2))), 0, iw - cw2)
                cy2 = _clamp(_even(int(round(fcy - ch2 / 2))), 0, ih - ch2)
                top = f"[0:v]crop={cw2}:{ch2}:{cx2}:{cy2},scale={W}:{top_h},setsar=1[top]"
            else:
                top = f"[0:v]crop={w}:{h}:{x}:{y},{fill_top}[top]"
            fc = f"{top};[0:v]{fill_bot}[bot];[top][bot]vstack=inputs=2[v]"
            cmd = ["ffmpeg", "-y", "-i", gameplay_clip,
                   "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
        if _flag(ecfg, "enhance"):
            pass  # enhance diterapkan via _encode; layout ini encode langsung
        cmd += ["-r", str(rcfg["fps"]), "-c:v", rcfg["video_codec"],
                "-preset", rcfg["preset"], "-crf", str(rcfg["crf"]),
                "-c:a", "aac", "-ar", "44100", out]
        ffmpeg.run(cmd)

    def _facecam_region(self, clip: str, gcfg: dict):
        """(x,y,w,h, fcx,fcy) facecam single-file — kotak + PUSAT WAJAH (utk crop
        yang benar-benar terpusat di wajah). auto | tl|tr|bl|br | 'x,y,w,h'."""
        iw, ih = ffmpeg.video_dimensions(clip)
        spec = str(gcfg.get("facecam", "auto")).strip().lower()
        zoom = float(gcfg.get("cam_zoom", 0.8))
        fw = _even(int(iw * float(gcfg.get("facecam_w", 0.28))))
        fh = _even(int(ih * float(gcfg.get("facecam_h", 0.35))))

        def _c(box):  # lengkapi dgn pusat kotak bila deteksi tak beri pusat wajah
            return box if len(box) == 6 else (*box, box[0] + box[2] / 2, box[1] + box[3] / 2)

        m = re.match(r"^(\d+),(\d+),(\d+),(\d+)$", spec)
        if m:
            x, y, w, h = (int(v) for v in m.groups())
            return _c((_clamp(x, 0, iw - 2), _clamp(y, 0, ih - 2),
                       _even(min(w, iw - x)), _even(min(h, ih - y))))
        if spec in ("tl", "tr", "bl", "br"):
            box = self._detect_facecam(clip, hint=spec, zoom=zoom)
            if box:
                return box
            corners = {"tl": (0, 0), "tr": (iw - fw, 0),
                       "bl": (0, ih - fh), "br": (iw - fw, ih - fh)}
            x, y = corners[spec]
            return _c((_clamp(_even(x), 0, iw - fw), _clamp(_even(y), 0, ih - fh), fw, fh))
        # auto: pindai keempat sudut, ambil yg paling konsisten ada wajahnya
        box = self._detect_facecam(clip, zoom=zoom)
        if box:
            return box
        self.log.warning("Facecam tak terdeteksi -> pakai sudut kanan-atas. "
                         "Pilih sudut manual di Settings bila salah.")
        return _c((_even(iw - fw), 0, fw, fh))

    def _detect_facecam(self, clip: str, hint: str | None = None,
                        zoom: float = 1.0):
        """Deteksi kotak facecam dengan memindai TIAP SUDUT pada resolusi penuh
        (facecam biasanya kecil di sudut -> mengecilkan seluruh frame membuatnya
        tak terdeteksi). hint = 'tl'|'tr'|'bl'|'br' membatasi ke satu sudut."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        iw, ih = ffmpeg.video_dimensions(clip)
        # wilayah tiap sudut (fraksi frame): cukup besar utk memuat kotak cam
        RW, RH = 0.42, 0.5
        regions = {
            "tl": (0, 0), "tr": (int(iw * (1 - RW)), 0),
            "bl": (0, int(ih * (1 - RH))), "br": (int(iw * (1 - RW)), int(ih * (1 - RH))),
        }
        if hint in regions:
            regions = {hint: regions[hint]}
        rw, rh = int(iw * RW), int(ih * RH)

        cascade = cv2.CascadeClassifier(os.path.join(
            cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        yunet = None
        if os.path.isfile(_YUNET):
            try:
                yunet = cv2.FaceDetectorYN.create(_YUNET, "", (rw, rh),
                                                  score_threshold=0.6)
            except Exception:  # noqa: BLE001
                yunet = None

        def faces_in(sub):
            out = []
            if yunet is not None:
                yunet.setInputSize((sub.shape[1], sub.shape[0]))
                _, ff = yunet.detect(sub)
                for f in (ff if ff is not None else []):
                    out.append((float(f[0]), float(f[1]), float(f[2]), float(f[3])))
            else:
                g = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
                for (x, y, w, h) in cascade.detectMultiScale(g, 1.15, 5, minSize=(60, 60)):
                    out.append((float(x), float(y), float(w), float(h)))
            return out

        cap = cv2.VideoCapture(clip)
        dur = ffmpeg.duration_seconds(clip)
        hits = {c: [] for c in regions}  # wajah dlm koordinat frame-penuh
        for k in range(14):
            control.checkpoint()  # deteksi facecam bisa ~belasan detik
            cap.set(cv2.CAP_PROP_POS_MSEC, dur * (k + 0.5) / 14 * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            for c, (ox, oy) in regions.items():
                sub = fr[oy:oy + rh, ox:ox + rw]
                if sub.size == 0:
                    continue
                for (x, y, w, h) in faces_in(sub):
                    if w >= 45 and h >= 45:            # abaikan wajah mini (UI game)
                        hits[c].append((ox + x, oy + y, w, h))
        cap.release()

        best = max(hits, key=lambda c: len(hits[c]), default=None)
        if best is None or len(hits[best]) < 4:
            return None
        b = hits[best]
        # posisi/ukuran wajah yg robust (median)
        fx = float(np.median([f[0] for f in b]))
        fy = float(np.median([f[1] for f in b]))
        fw = float(np.median([f[2] for f in b]))
        fh = float(np.median([f[3] for f in b]))
        fcx, fcy = fx + fw / 2, fy + fh / 2
        # kotak "webcam" (kepala+bahu). zoom<1 = kotak lebih besar = wajah lebih
        # kecil di panel (kurang zoom-in). Wajah dipusatkan agar crop-tengah 'fill'
        # menjaganya utuh.
        z = max(0.4, min(1.2, zoom))
        cw = min(iw, fw * 3.2 / z)
        ch = min(ih, fh * 3.0 / z)
        cx = _clamp(_even(int(fcx - cw / 2)), 0, iw - _even(int(cw)))
        cy = _clamp(_even(int(fcy - ch / 2)), 0, ih - _even(int(ch)))
        return (cx, cy, _even(int(cw)), _even(int(ch)), int(fcx), int(fcy))

    def _normalize_audio(self, path: str) -> None:
        """Loudness -14 LUFS (satu-pass), video di-copy — konsisten & cepat."""
        tmp = path + ".ln.mp4"
        try:
            ffmpeg.run(["ffmpeg", "-y", "-i", path, "-c:v", "copy",
                        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                        "-c:a", "aac", "-ar", "44100", tmp])
            os.replace(tmp, path)
        except control.JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            self.log.warning("loudnorm gagal (%s) -> lewati.", str(e).split(chr(10))[0][:80])
            if os.path.exists(tmp):
                os.remove(tmp)

    # ================= trim jeda hening =================
    def _plan_keep(self, raw: str, dur: float, ecfg: dict,
                   min_keep: float) -> list[tuple[float, float]] | None:
        """Segmen yang DIPERTAHANKAN (buang hening). None = jangan trim."""
        thr = float(ecfg.get("silence_threshold", -30))
        min_sil = float(ecfg.get("min_silence", 0.6))
        pad = max(0.0, float(ecfg.get("keep_padding", 0.1)))
        try:
            err = ffmpeg.run_capture([
                "ffmpeg", "-i", raw,
                "-af", f"silencedetect=noise={thr}dB:d={min_sil}", "-f", "null", "-"])
        except RuntimeError as e:
            self.log.warning("silencedetect gagal (%s) -> tak trim.",
                             str(e).split(chr(10))[0][:80])
            return None
        starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", err)]
        if len(starts) > len(ends):          # klip berakhir dalam hening
            ends.append(dur)
        sils = []
        for s, e in zip(starts, ends):
            rs, re_ = s + pad, e - pad        # sisakan padding di sekitar ucapan
            if re_ - rs >= 0.15:
                sils.append((max(0.0, rs), min(dur, re_)))
        if not sils:
            return None
        kept, cur = [], 0.0
        for rs, re_ in sils:
            if rs > cur + 0.05:
                kept.append((round(cur, 3), round(rs, 3)))
            cur = max(cur, re_)
        if cur < dur - 0.05:
            kept.append((round(cur, 3), round(dur, 3)))
        kept = [(s, e) for s, e in kept if e - s > 0.05]
        total = sum(e - s for s, e in kept)
        if not kept or total >= dur - 0.2 or total < max(3.0, min_keep * 0.5):
            return None    # tak ada yg dibuang / terlalu agresif -> aman: skip
        return kept

    def _trim_gaps(self, raw: str, out: str, kept: list[tuple[float, float]],
                   rcfg: dict) -> None:
        """Potong tiap segmen kept lalu gabung -> video tanpa jeda hening."""
        segs = []
        try:
            for k, (s, e) in enumerate(kept):
                control.checkpoint()
                seg = f"{out}.k{k:03d}.mp4"
                ffmpeg.run(["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", raw,
                            "-t", f"{e - s:.3f}", "-r", str(rcfg["fps"]),
                            "-c:v", rcfg["video_codec"], "-preset", rcfg["preset"],
                            "-crf", str(rcfg["crf"]), "-c:a", "aac", "-ar", "44100", seg])
                segs.append(seg)
            lst = out + ".cc.txt"
            with open(lst, "w", encoding="utf-8") as f:
                for s in segs:
                    f.write("file '" + s.replace("\\", "/").replace("'", r"'\''") + "'\n")
            ffmpeg.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", out])
        finally:
            for s in segs:
                if os.path.isfile(s):
                    os.remove(s)
            if os.path.isfile(out + ".cc.txt"):
                os.remove(out + ".cc.txt")

    def _encode(self, src: str, out: str, rcfg: dict, vf: str,
                seek: float | None = None, dur: float | None = None,
                punch: tuple[float, float] | None = None) -> None:
        ecfg = self.cfg.get("editor") or {}
        W, H = rcfg["width"], rcfg["height"]
        if _flag(ecfg, "enhance"):
            vf += ",eq=contrast=1.06:saturation=1.06:brightness=0.01,unsharp=5:5:0.6:5:5:0.0"
        if punch is not None:
            # Ken Burns: zoom pelan dari z0->z1 selama segmen (zoompan di 1080x1920)
            z0, z1 = punch
            d = max(1, int(round((dur or 3.0) * rcfg["fps"])))
            vf += (f",zoompan=z='min(zoom+{(z1 - z0) / d:.6f},{z1:.3f})'"
                   f":d={d}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={rcfg['fps']}")
        cmd = ["ffmpeg", "-y"]
        if seek is not None:
            cmd += ["-ss", f"{seek:.3f}"]
        cmd += ["-i", src]
        if dur is not None:
            cmd += ["-t", f"{dur:.3f}"]
        cmd += ["-vf", vf, "-r", str(rcfg["fps"]),
                "-c:v", rcfg["video_codec"], "-preset", rcfg["preset"],
                "-crf", str(rcfg["crf"]), "-c:a", "aac", "-ar", "44100", out]
        ffmpeg.run(cmd)

    def _render_segments(self, raw: str, out: str, rcfg: dict, plan: list[dict],
                         follow_vf, W: int, H: int, ecfg: dict) -> None:
        """Render tiap shot terpisah (mode bisa beda), lalu gabung tanpa re-encode."""
        wide_vf = (
            f"split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma=24[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        punch_on = _flag(ecfg, "punch_in")
        segs = []
        try:
            for j, p in enumerate(plan):
                control.checkpoint()
                seg = f"{out}.seg{j:03d}.mp4"
                vf = wide_vf if p["mode"] == "wide" else follow_vf(p["xexpr"])
                # punch-in hanya pada shot follow yg cukup panjang & statis
                punch = None
                if punch_on and p["mode"] == "follow" and (p["t1"] - p["t0"]) >= 1.5:
                    punch = (1.0, 1.08)
                self._encode(raw, seg, rcfg, vf, seek=p["t0"], dur=p["t1"] - p["t0"],
                             punch=punch)
                segs.append(seg)
            lst = out + ".concat.txt"
            with open(lst, "w", encoding="utf-8") as f:
                for s in segs:
                    f.write("file '" + s.replace("\\", "/").replace("'", r"'\''") + "'\n")
            ffmpeg.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c", "copy", out])
        finally:
            for s in segs:
                if os.path.isfile(s):
                    os.remove(s)
            if os.path.isfile(out + ".concat.txt"):
                os.remove(out + ".concat.txt")

    # ================= analisis =================
    def _scene_cuts(self, path: str, threshold: float,
                    min_gap: float, dur: float) -> list[float]:
        """Timestamp perpindahan kamera (detik), berjarak minimal min_gap."""
        try:
            err = ffmpeg.run_capture([
                "ffmpeg", "-i", path,
                "-vf", f"select='gt(scene,{threshold})',showinfo",
                "-an", "-f", "null", "-",
            ])
        except RuntimeError as e:
            self.log.warning("Deteksi scene gagal (%s) -> crop statis.",
                             str(e).split("\n")[0][:80])
            return []
        cuts: list[float] = []
        for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", err):
            t = float(m.group(1))
            if t < min_gap or t > dur - min_gap:
                continue
            if not cuts or t - cuts[-1] >= min_gap:
                cuts.append(t)
        return cuts

    def _plan_shots(self, raw: str, shots: list[tuple[float, float]],
                    iw: int, cw: int, ecfg: dict) -> list[dict]:
        """Per shot: mode ('follow'|'wide') + ekspresi crop x (relatif awal shot).

        cw = lebar area yang terlihat (sudah memperhitungkan editor.zoom).
        """
        speaker_on = bool(ecfg.get("speaker_detect", True))
        everyone = str(ecfg.get("everyone_mode", "blur")).lower()
        talk_min = float(ecfg.get("talk_threshold", 1.2))
        samples = int(ecfg.get("samples_per_shot", 5))
        smooth = _flag(ecfg, "smooth_pan")
        saliency = _flag(ecfg, "saliency_crop")

        analyses = self._analyze_shots(raw, shots, iw, samples, ecfg)

        plan: list[dict] = []
        deadband = 0.04 * iw
        for (t0, t1), an in zip(shots, analyses):
            tracks = an["tracks"]
            mode, cx, series = "follow", None, None
            if tracks:
                active = [t for t in tracks if t["talk"] > talk_min]
                active.sort(key=lambda t: -t["talk"])
                if (speaker_on and len(active) >= 2
                        and active[1]["talk"] > 0.5 * active[0]["talk"]):
                    lo = min(t["cx"] - t["w"] / 2 for t in active)
                    hi = max(t["cx"] + t["w"] / 2 for t in active)
                    if (hi - lo) > 0.92 * cw and everyone == "blur":
                        mode = "wide"
                    else:
                        cx = (lo + hi) / 2
                elif speaker_on and active:
                    cx = active[0]["cx"]
                    series = active[0].get("series") if smooth else None
                else:
                    big = max(tracks, key=lambda t: t["w"])
                    cx = big["cx"]
                    series = big.get("series") if smooth else None
            elif saliency and an.get("motion_cx") is not None:
                cx = an["motion_cx"]  # tak ada wajah -> ikuti area gerak
            plan.append({"t0": t0, "t1": t1, "mode": mode, "cx": cx, "series": series,
                         "n_faces": len(tracks),
                         "n_talk": len([t for t in tracks if t["talk"] > talk_min])})

        # isi shot tanpa posisi dari tetangga
        last = None
        for p in plan:
            if p["mode"] == "follow" and p["cx"] is None:
                p["cx"] = last
            elif p["mode"] == "follow":
                last = p["cx"]
        nxt = None
        for p in reversed(plan):
            if p["mode"] == "follow" and p["cx"] is None:
                p["cx"] = nxt if nxt is not None else iw / 2
            elif p["mode"] == "follow":
                nxt = p["cx"]

        # cx/series -> ekspresi x (relatif awal shot), clamp + deadband
        prev_x = None
        for p in plan:
            if p["mode"] != "follow":
                continue
            if smooth and p.get("series") and len(p["series"]) >= 2:
                p["xexpr"] = self._pan_expr(p["series"], p["t0"], cw, iw)
                prev_x = None  # ekspresi dinamis -> reset deadband
            else:
                x = _clamp(int(round(p["cx"] - cw / 2)), 0, iw - cw)
                if prev_x is not None and abs(x - prev_x) < deadband:
                    x = prev_x
                p["xexpr"] = str(x)
                prev_x = x

        for j, p in enumerate(plan):
            self.log.debug("   shot %d (%.1f-%.1fs): %d wajah, %d bicara -> %s %s",
                           j + 1, p["t0"], p["t1"], p["n_faces"], p["n_talk"],
                           p["mode"], p.get("xexpr", ""))
        return plan

    def _pan_expr(self, series: list[tuple[float, float]], t0: float,
                  cw: int, iw: int) -> str:
        """Ekspresi x(t) piecewise-linear halus dari sampel (t_rel, cx)."""
        pts = sorted(series)
        # EMA untuk meredam jitter
        sm, a = [], 0.5
        acc = pts[0][1]
        for t, c in pts:
            acc = a * c + (1 - a) * acc
            sm.append((t, _clamp(int(round(acc - cw / 2)), 0, iw - cw)))
        expr = str(sm[-1][1])
        for k in range(len(sm) - 2, -1, -1):
            t1, x1 = sm[k]
            t2, x2 = sm[k + 1]
            if t2 <= t1:
                continue
            slope = (x2 - x1) / (t2 - t1)
            lin = f"({x1}+({slope:.3f})*(t-{t1:.3f}))"
            expr = f"if(lt(t,{t2:.3f}),{lin},{expr})"
        return expr

    def _analyze_shots(self, video_path: str, shots: list[tuple[float, float]],
                       iw: int, samples: int, ecfg: dict) -> list[dict]:
        """Per shot: {tracks:[{cx,w,talk,series}], motion_cx}. Koordinat penuh."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.log.warning("OpenCV tak terpasang -> crop tengah.")
            return [{"tracks": [], "motion_cx": None} for _ in shots]

        detector = str(ecfg.get("detector", "haar")).lower()
        av = str(ecfg.get("speaker_model", "visual")).lower() == "av"
        saliency = _flag(ecfg, "saliency_crop")
        scale = min(1.0, _DET_W / iw)
        dw, dh = int(round(iw * scale)), 0  # dh diisi saat frame pertama

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return [{"tracks": [], "motion_cx": None} for _ in shots]

        yunet = None
        if detector == "yunet" and os.path.isfile(_YUNET):
            try:
                yunet = cv2.FaceDetectorYN.create(_YUNET, "", (dw, max(1, dw)))
            except Exception as e:  # noqa: BLE001
                self.log.warning("YuNet gagal dimuat (%s) -> Haar.", str(e)[:60])
        elif detector == "yunet":
            self.log.warning("Model YuNet tak ada -> Haar.")
        cascade = cv2.CascadeClassifier(os.path.join(
            cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))

        env = self._audio_env(video_path) if av else None

        def grab(t, gray=True):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                return None
            if scale < 1.0:
                frame = cv2.resize(frame, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if gray else frame

        def detect(gray):
            if yunet is not None:
                h, w = gray.shape[:2]
                yunet.setInputSize((w, h))
                bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                _, faces = yunet.detect(bgr)
                out = []
                if faces is not None:
                    for f in faces:
                        out.append((int(f[0]), int(f[1]), int(f[2]), int(f[3])))
                return out
            return [tuple(int(v) for v in r)
                    for r in cascade.detectMultiScale(gray, 1.2, 5, minSize=(40, 40))]

        results: list[dict] = []
        for (t0, t1) in shots:
            control.checkpoint()  # stop/pause antar shot (analisis bisa lama)
            span = t1 - t0
            pad = min(0.15, span * 0.15)
            n = max(1, min(samples, int(span * 3) or 1))
            obs = []      # (t_abs, cx_full, w_full, talk)
            for k in range(n):
                t = t0 + pad + (span - 2 * pad - _PAIR_GAP) * (k + 0.5) / n
                g1, g2 = grab(t), grab(t + _PAIR_GAP)
                if g1 is None or g2 is None:
                    continue
                for (x, y, w, h) in detect(g1):
                    if w < 20 or h < 20:
                        continue
                    m1 = g1[y + int(.60 * h):y + int(.95 * h), x + int(.25 * w):x + int(.75 * w)]
                    m2 = g2[y + int(.60 * h):y + int(.95 * h), x + int(.25 * w):x + int(.75 * w)]
                    b1 = g1[max(0, y + int(.10 * h)):y + int(.40 * h), x + int(.25 * w):x + int(.75 * w)]
                    b2 = g2[max(0, y + int(.10 * h)):y + int(.40 * h), x + int(.25 * w):x + int(.75 * w)]
                    if m1.size == 0 or m1.shape != m2.shape or b1.size == 0 or b1.shape != b2.shape:
                        continue
                    talk = (float(np.mean(cv2.absdiff(m1, m2)))
                            - float(np.mean(cv2.absdiff(b1, b2))))
                    obs.append((t, (x + w / 2) / scale, w / scale, talk))
            # cluster jadi track berdasarkan posisi-x
            tracks = []
            for t, cx, w, talk in obs:
                tr = next((z for z in tracks if abs(z["cx"] - cx) < 0.10 * iw), None)
                if tr is None:
                    tracks.append({"cx": cx, "w": w, "series": [(t - t0, cx)],
                                   "talks": [talk], "mt": [(t, talk)], "n": 1})
                else:
                    tr["cx"] = (tr["cx"] * tr["n"] + cx) / (tr["n"] + 1)
                    tr["w"] = max(tr["w"], w)
                    tr["series"].append((t - t0, cx))
                    tr["talks"].append(talk)
                    tr["mt"].append((t, talk))
                    tr["n"] += 1
            min_obs = max(1, n // 3)
            final = []
            for tr in tracks:
                if tr["n"] < min_obs:
                    continue
                talks = sorted(tr["talks"])
                base = talks[len(talks) // 2]  # median gerak mulut
                if av and env is not None and len(tr["mt"]) >= 3:
                    corr = _corr([env(t) for t, _ in tr["mt"]], [v for _, v in tr["mt"]])
                    base *= (1.0 + max(0.0, corr))   # naikkan bila selaras audio
                tr["talk"] = base
                final.append(tr)
            motion_cx = None
            if saliency and not final:
                motion_cx = self._motion_cx(grab, t0 + pad, t1 - pad, iw, scale)
            results.append({"tracks": final, "motion_cx": motion_cx})
        cap.release()
        return results

    def _audio_env(self, path: str):
        """Kembalikan fungsi env(t)->energi audio (RMS ternormalisasi) atau None."""
        try:
            import numpy as np
            import subprocess
            p = subprocess.run(["ffmpeg", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
                                "-f", "s16le", "-"], capture_output=True)
            a = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
            if a.size < 800:
                return None
            sr, win = 8000, 800  # 0.1s
            e = np.array([np.sqrt(np.mean(a[i:i + win] ** 2))
                          for i in range(0, len(a) - win, win)])
            if e.max() > 0:
                e = e / e.max()
            return lambda t: float(e[min(len(e) - 1, max(0, int(t / 0.1)))])
        except Exception:  # noqa: BLE001
            return None

    def _motion_cx(self, grab, t0: float, t1: float, iw: int, scale: float):
        """Posisi-x kolom dengan gerak terbanyak (untuk shot tanpa wajah)."""
        try:
            import numpy as np
            acc = None
            ts = [t0 + (t1 - t0) * k / 4 for k in range(5)]
            for a, b in zip(ts, ts[1:]):
                g1, g2 = grab(a), grab(b)
                if g1 is None or g2 is None or g1.shape != g2.shape:
                    continue
                d = np.abs(g1.astype(np.int16) - g2.astype(np.int16)).sum(axis=0)
                acc = d if acc is None else acc + d
            if acc is None or acc.sum() == 0:
                return None
            col = int(np.argmax(np.convolve(acc, np.ones(21) / 21, mode="same")))
            return col / scale
        except Exception:  # noqa: BLE001
            return None


def _corr(a, b):
    import numpy as np
    a, b = np.array(a, float), np.array(b, float)
    if a.std() < 1e-6 or b.std() < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _flag(ecfg: dict, key: str) -> bool:
    return bool(ecfg.get(key, False))


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

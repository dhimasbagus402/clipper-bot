"""Agent 12 — Quality Checker. Pemeriksaan dasar tiap klip hasil render.

Cek: file ada, durasi masuk akal, resolusi sesuai target. PASS/FAIL + alasan.
(v1 sengaja sederhana; cek black-frame/audio/retention menyusul.)
"""
from __future__ import annotations

from .base import BaseAgent
from ..util import ffmpeg


class QCAgent(BaseAgent):
    name = "QC"

    def run(self, project, ctx: dict) -> None:
        rcfg = self.cfg["render"]
        ccfg = self.cfg["clips"]
        results = []
        for clip in ctx["clips"]:
            render = clip.get("render_path")
            issues = []
            status = "PASS"
            try:
                w, h = ffmpeg.video_dimensions(render)
                dur = ffmpeg.duration_seconds(render)
                if (w, h) != (rcfg["width"], rcfg["height"]):
                    issues.append(f"resolusi {w}x{h} != {rcfg['width']}x{rcfg['height']}")
                if dur < ccfg["min_seconds"] - 2 or dur > ccfg["max_seconds"] + 5:
                    issues.append(f"durasi {dur:.1f}s di luar rentang wajar")
            except Exception as e:  # noqa: BLE001
                issues.append(f"tidak bisa di-probe: {e}")

            if issues:
                status = "FAIL"
            clip["qc_status"] = status
            clip["qc_issues"] = issues
            results.append((clip["idx"], status, issues))
            self.log.info("Klip %02d: %s %s", clip["idx"], status,
                          ("- " + "; ".join(issues)) if issues else "")

        ctx["qc"] = results

"""Manager Agent (Orchestrator) — padanan Manager di dokumen Anda.

Tanggung jawab: menjalankan agent dalam urutan benar, menaikkan status (state machine),
retry+backoff bila gagal, mencatat log, dan menulis laporan akhir. Manager TIDAK mengedit
video atau menganalisis konten sendiri — hanya mengoordinasi.
"""
from __future__ import annotations
import json
import logging
import time

from . import control
from .states import State
from .db import DB
from .project import Project
from .agents.transcript import TranscriptAgent
from .agents.analyzer import AnalyzerAgent
from .agents.editor import EditorAgent
from .agents.subtitle import SubtitleAgent
from .agents.seo import SEOAgent
from .agents.thumbnail import ThumbnailAgent
from .agents.compliance import ComplianceAgent
from .agents.upload import UploadAgent
from .agents.qc import QCAgent


class Manager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = logging.getLogger("Manager")
        # (agent, status sebelum dijalankan)
        self.pipeline = [
            (TranscriptAgent(cfg), State.TRANSCRIBING),
            (AnalyzerAgent(cfg), State.ANALYZING),
            (EditorAgent(cfg), State.EDITING),
            (SubtitleAgent(cfg), State.SUBTITLING),
            (SEOAgent(cfg), State.SEO),
            (ThumbnailAgent(cfg), State.THUMBNAIL),
            (QCAgent(cfg), State.QC),
            (ComplianceAgent(cfg), State.COMPLIANCE),
            (UploadAgent(cfg), State.UPLOAD),
        ]

    def process(self, source_path: str, name: str | None = None) -> Project:
        projects_dir = self.cfg["paths"]["projects"]
        db = DB(projects_dir)
        project = Project.create(projects_dir, source_path, name)
        db.add_project(project.id, project.name, project.source_path)
        self._fh = self._add_file_logger(project)
        self.log.info("=== Project %s (%s) ===", project.id, project.name)

        ctx: dict = {}
        self._t0 = time.time()
        control.reset()
        try:
            for agent, state in self.pipeline:
                control.checkpoint()
                db.set_status(project.id, state.value)
                self.log.info("-> %s (%s)", state.value, agent.name)
                self._run_with_retry(agent, project, ctx)
            db.set_status(project.id, State.DONE.value)
            self._save_clips(db, project, ctx)
            self.log.info("=== SELESAI: %d klip -> %s ===",
                          len(ctx.get("clips", [])), project.path("render"))
        except control.JobCancelled:
            db.set_status(project.id, "STOPPED")
            self.log.info("=== DIHENTIKAN pengguna ===")
            raise
        except Exception as e:  # noqa: BLE001
            db.set_status(project.id, State.FAILED.value)
            self.log.error("GAGAL: %s", e)
            raise
        finally:
            self._write_report(project, ctx)
            db.close()
            self._remove_file_logger()
        return project

    # ---- internal ----
    def _run_with_retry(self, agent, project, ctx) -> None:
        mcfg = self.cfg["manager"]
        retries = mcfg["max_retries"]
        backoff = mcfg["backoff_seconds"]
        last = None
        for attempt in range(1, retries + 1):
            control.checkpoint()
            t0 = time.time()
            try:
                agent.run(project, ctx)
                self.log.info("   %s OK (%.1fs)", agent.name, time.time() - t0)
                return
            except control.JobCancelled:
                raise  # stop oleh pengguna -> jangan retry
            except Exception as e:  # noqa: BLE001
                last = e
                self.log.warning("   %s gagal (percobaan %d/%d): %s",
                                 agent.name, attempt, retries, e)
                if attempt < retries:
                    wait = backoff[min(attempt - 1, len(backoff) - 1)]
                    time.sleep(wait)
        raise RuntimeError(f"Agent {agent.name} gagal setelah {retries}x: {last}")

    def _save_clips(self, db: DB, project: Project, ctx: dict) -> None:
        for clip in ctx.get("clips", []):
            cid = db.add_clip(project.id, clip["idx"], clip["start"], clip["end"],
                              clip["score"], clip.get("reason", ""))
            db.set_clip_render(cid, clip.get("render_path", ""),
                               clip.get("qc_status", "UNKNOWN"))

    def _write_report(self, project: Project, ctx: dict) -> None:
        report = {
            "project_id": project.id,
            "name": project.name,
            "source": project.source_path,
            "runtime_seconds": round(time.time() - getattr(self, "_t0", time.time()), 1),
            "clips": [
                {k: c.get(k) for k in
                 ("idx", "start", "end", "score", "reason", "render_path",
                  "qc_status", "qc_issues", "seo",
                  "thumbnail_path", "compliance_status", "compliance_issues",
                  "youtube_url")}
                for c in ctx.get("clips", [])
            ],
        }
        with open(project.path("metadata", "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def _add_file_logger(self, project: Project):
        fh = logging.FileHandler(project.path("logs", "run.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        return fh

    def _remove_file_logger(self) -> None:
        fh = getattr(self, "_fh", None)
        if fh is not None:
            logging.getLogger().removeHandler(fh)
            fh.close()
            self._fh = None

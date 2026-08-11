"""Model Project — membuat struktur folder per project (padanan Fase 1 Â§6).

Tiap video punya satu folder sendiri berisi sub-direktori untuk tiap tahap.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime

SUBDIRS = [
    "input", "audio", "transcript", "clips",
    "work", "render", "subtitle", "thumbnail", "logs", "metadata",
]


def make_project_id() -> str:
    # contoh: 20260629-143501
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class Project:
    id: str
    name: str
    root: str          # path absolut ke folder project
    source_path: str   # path video sumber

    @classmethod
    def create(cls, projects_dir: str, source_path: str, name: str | None = None) -> "Project":
        pid = make_project_id()
        root = os.path.abspath(os.path.join(projects_dir, pid))
        os.makedirs(root, exist_ok=True)
        for d in SUBDIRS:
            os.makedirs(os.path.join(root, d), exist_ok=True)
        return cls(
            id=pid,
            name=name or os.path.basename(source_path),
            root=root,
            source_path=os.path.abspath(source_path),
        )

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

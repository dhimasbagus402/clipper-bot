"""Kelas dasar untuk semua agent.

Prinsip dari desain Anda: tiap agent satu tanggung jawab, tidak memanggil agent lain.
Agent hanya menerima (project, ctx, cfg), mengerjakan tugasnya, lalu menulis hasil ke
`ctx` (state bersama yang dipegang Manager) dan/atau ke disk.
"""
from __future__ import annotations
import logging
from abc import ABC, abstractmethod


class BaseAgent(ABC):
    name: str = "BaseAgent"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = logging.getLogger(self.name)

    @abstractmethod
    def run(self, project, ctx: dict) -> None:
        """Kerjakan tugas. Boleh raise Exception bila gagal (Manager yang retry)."""
        raise NotImplementedError

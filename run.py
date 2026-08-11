#!/usr/bin/env python3
"""Entry point CLI.

Pakai:
    python run.py incoming/podcast.mp4
    python run.py incoming/podcast.mp4 --name "Podcast Eps 12"
"""
from __future__ import annotations
import argparse
import logging
import os
import sys

from acf.config import load_config
from acf.manager import Manager
from acf.util import ffmpeg


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Content Factory — walking skeleton")
    parser.add_argument("video", help="path video sumber")
    parser.add_argument("--name", default=None, help="nama project (opsional)")
    parser.add_argument("--config", default=None, help="path config.yaml (opsional)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.path.isfile(args.video):
        print(f"File tidak ditemukan: {args.video}", file=sys.stderr)
        return 1

    try:
        ffmpeg.ensure_ffmpeg()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    try:
        project = Manager(cfg).process(args.video, args.name)
    except Exception as e:  # noqa: BLE001
        print(f"\nProses gagal: {e}", file=sys.stderr)
        return 2

    print(f"\nSelesai. Hasil di: {project.path('render')}")
    print(f"Laporan: {project.path('metadata', 'report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

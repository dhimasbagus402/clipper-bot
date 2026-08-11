#!/usr/bin/env python3
"""Bandingkan beberapa model Ollama untuk tahap pemilihan highlight — TANPA transkripsi ulang.

Membaca transcript.json dari sebuah project yang sudah ada, lalu menjalankan Analyzer
dengan tiap model dan menampilkan hasilnya berdampingan.

Pakai:
    python compare_models.py                         # project terbaru, model: 7b vs 14b
    python compare_models.py --models qwen2.5:7b qwen2.5:14b
    python compare_models.py --project projects\20260701-003324
"""
from __future__ import annotations
import argparse
import glob
import json
import logging
import os
import time

from acf.config import load_config
from acf.project import Project
from acf.agents.analyzer import AnalyzerAgent


def latest_project(projects_dir: str) -> str | None:
    dirs = [d for d in glob.glob(os.path.join(projects_dir, "*")) if os.path.isdir(d)]
    dirs = [d for d in dirs if os.path.isfile(os.path.join(d, "transcript", "transcript.json"))]
    return max(dirs, key=os.path.getmtime) if dirs else None


def run_one(cfg: dict, proj: Project, transcript: dict, model: str) -> tuple[list, float]:
    cfg = json.loads(json.dumps(cfg))  # salinan dalam supaya tidak saling timpa
    cfg["llm"]["model"] = model
    ctx = {"transcript": transcript}
    agent = AnalyzerAgent(cfg)
    t0 = time.time()
    agent.run(proj, ctx)
    elapsed = time.time() - t0
    safe = model.replace(":", "_").replace("/", "_")
    out = proj.path("metadata", f"highlights_{safe}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ctx["highlights"], f, ensure_ascii=False, indent=2)
    return ctx["highlights"], elapsed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None, help="folder project (default: terbaru)")
    ap.add_argument("--models", nargs="+", default=["qwen2.5:7b", "qwen2.5:14b"])
    args = ap.parse_args()

    cfg = load_config()
    proj_dir = args.project or latest_project(cfg["paths"]["projects"])
    if not proj_dir or not os.path.isdir(proj_dir):
        print("Tidak menemukan project dengan transcript.json. Jalankan run.py dulu.")
        return 1

    tpath = os.path.join(proj_dir, "transcript", "transcript.json")
    with open(tpath, encoding="utf-8") as f:
        transcript = json.load(f)
    print(f"Project : {proj_dir}")
    print(f"Durasi  : {transcript.get('duration')}s, {len(transcript['segments'])} segmen\n")

    proj = Project(id=os.path.basename(proj_dir), name="compare",
                   root=os.path.abspath(proj_dir), source_path="")

    results = {}
    for model in args.models:
        print(f"=== {model} ===")
        try:
            hl, sec = run_one(cfg, proj, transcript, model)
        except RuntimeError as e:
            print(f"  GAGAL: {e}")
            print(f"  (sudah di-pull? coba: ollama pull {model})\n")
            continue
        results[model] = (hl, sec)
        print(f"  {len(hl)} highlight dalam {sec:.1f}s")
        for h in hl:
            print(f"   [{_fmt(h['start'])}-{_fmt(h['end'])}] skor {h['score']:.0f}  {h['reason']}")
        print()

    if len(results) >= 2:
        print("Ringkasan:")
        for model, (hl, sec) in results.items():
            avg = sum(h["score"] for h in hl) / len(hl) if hl else 0
            print(f"  {model:18s} {len(hl):2d} klip  |  {sec:5.1f}s  |  rata-rata skor {avg:.0f}")
        print("\nBuka file metadata\\highlights_<model>.json untuk membandingkan detail,")
        print("lalu tonton 2-3 klip dari masing-masing untuk menilai mana yang lebih 'nendang'.")
    return 0


def _fmt(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


if __name__ == "__main__":
    raise SystemExit(main())

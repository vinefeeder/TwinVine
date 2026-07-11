#!/usr/bin/env python3
"""
Recursively convert form .mkv files to mp4 using ffmpeg.

Default behavior:
- Find all *.mkv under a given root
- Convert to mp4 with the same base name, in the same folder
  e.g. "Taggart S01E02.mkv" -> "Taggart S01E02.mp4"

Requires:
- ffmpeg  available on PATH
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert mkv files to mp4 (recursively) using ffmpeg."
    )
    p.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Root directory to scan (default: current directory).",
    )
    p.add_argument(
        "--ext",
        default=".mkv",
        help="Input extension to scan for (default: .mkv).",
    )
    p.add_argument(
        "--out-ext",
        default=".mp4",
        help="Output extension to write (default: .mp4).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done, but don't run ffmpeg.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print ffmpeg output for each file.",
    )
    return p.parse_args()


def run_convert(
    ffmpeg_path: str,
    in_file: Path,
    out_file: str,
    overwrite: bool,
    dry_run: bool,
    verbose: bool,
    ) -> bool:
    
    out_file = Path(out_file)
    in_file = Path(in_file)

    if out_file.exists() and not overwrite:
        print(f"SKIP (exists): {out_file}")
        return True

    cmd = [
        ffmpeg_path,
        "-i",
        in_file,
        "-c",
        "copy",
        out_file,
    ]

    if dry_run:
        print("DRY:", " ".join(map(str, cmd)))
        return True

    # Ensure parent exists (it should, but just in case you change output logic later)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("ERROR: ffmpeg not found (is MKVToolNix installed and on PATH?)", file=sys.stderr)
        return False

    if verbose and proc.stdout:
        print(proc.stdout.rstrip())

    if proc.returncode == 0 and out_file.exists():
        print(f"OK  : {in_file.name} -> {out_file.name}")
        return True

    print(f"FAIL: {in_file}", file=sys.stderr)
    if proc.stdout:
        print(proc.stdout.rstrip(), file=sys.stderr)
    return False


def main() -> int:
    args = parse_args()

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("ERROR: ffmpeg not found on PATH. Install MKVToolNix.", file=sys.stderr)
        return 2

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: Root path does not exist: {root}", file=sys.stderr)
        return 2

    in_ext = args.ext if args.ext.startswith(".") else f".{args.ext}"
    out_ext = args.out_ext if args.out_ext.startswith(".") else f".{args.out_ext}"

    files = sorted(p for p in root.rglob(f"*{in_ext}") if p.is_file())
    if not files:
        print(f"No {in_ext} files found under {root}")
        return 0

    ok = 0
    fail = 0

    for in_file in files:
        out_file = in_file.with_suffix(out_ext)
        success = run_convert(
            ffmpeg_path=ffmpeg_path,
            in_file=in_file,
            out_file=out_file,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        if success:
            ok += 1
        else:
            fail += 1

    print(f"\nDone. OK={ok}, FAIL={fail}, TOTAL={ok+fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

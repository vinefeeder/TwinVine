#!/usr/bin/env python3
"""
Selecting -S (subtitles only) as a download option results in an mks file
which needs convertion to something acceptable for adding to a video playback.
This is a post processing routine that operates in the root folder of any number of
mks files.
The sceipt will recursively extract subtitle tracks from .mks files using mkvextract.

A CLI for example would  mkvextract "Taggart S01E02.mks"  tracks 0:tS01E02.srt but the script
finds each title and run the CLI on it.


Default behavior:
- Find all *.mks under a given root
- Extract track 0 to an .srt with the same base name, in the same folder
  e.g. "Taggart S01E02.mks" -> "Taggart S01E02.srt"

Requires:
- mkvextract (part of MKVToolNix) available on PATH
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract SRT subtitles from .mks files (recursively) using mkvextract."
    )
    p.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Root directory to scan (default: current directory).",
    )
    p.add_argument(
        "--track",
        type=int,
        default=0,
        help="Track index to extract (default: 0).",
    )
    p.add_argument(
        "--ext",
        default=".mks",
        help="Input extension to scan for (default: .mks).",
    )
    p.add_argument(
        "--out-ext",
        default=".srt",
        help="Output extension to write (default: .srt).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done, but don't run mkvextract.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print mkvextract output for each file.",
    )
    return p.parse_args()


def run_extract(
    mkvextract_path: str,
    in_file: Path,
    out_file: Path,
    track: int,
    overwrite: bool,
    dry_run: bool,
    verbose: bool,
) -> bool:
    if out_file.exists() and not overwrite:
        print(f"SKIP (exists): {out_file}")
        return True

    cmd = [
        mkvextract_path,
        str(in_file),
        "tracks",
        f"{track}:{out_file}",
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
        print("ERROR: mkvextract not found (is MKVToolNix installed and on PATH?)", file=sys.stderr)
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

    mkvextract_path = shutil.which("mkvextract")
    if not mkvextract_path:
        print("ERROR: mkvextract not found on PATH. Install MKVToolNix.", file=sys.stderr)
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
        success = run_extract(
            mkvextract_path=mkvextract_path,
            in_file=in_file,
            out_file=out_file,
            track=args.track,
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

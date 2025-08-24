#!/usr/bin/env python3
"""
fix_imports.py — Make intra-package imports in a src/ layout relative.

Usage:
  python tools/fix_imports.py \
      --root packages/vinefeeder/src \
      --pkg-name vinefeeder \
      [--apply]

By default it prints a plan (dry-run). Add --apply to rewrite files.
Backups (*.bak) are created when applying changes.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import shutil
from pathlib import Path
from typing import Iterable, List, Tuple


def find_package_modules(pkg_dir: Path) -> set[str]:
    """
    Return a set of top-level module/package names inside pkg_dir.
    Example: { 'pretty', 'parsing_utils', 'batchloader', 'services', ... }
    """
    names: set[str] = set()
    for p in pkg_dir.iterdir():
        if p.name.startswith("_"):
            continue
        if p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
        elif p.suffix == ".py":
            names.add(p.stem)
    return names


def rewrite_imports(
    src: str,
    file_path: Path,
    pkg_name: str,
    local_modules: set[str],
) -> Tuple[str, List[str]]:
    """
    Return (new_source, notes). Keeps formatting line-by-line as much as possible.
    We only rewrite simple import statements using AST nodes to decide the intent,
    then patch lines textually.
    """
    notes: List[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        notes.append(f"SKIP (syntax error): {e}")
        return src, notes

    # Collect edits as (lineno-1, new_line) for lines we fully replace
    edits: dict[int, str] = {}

    # We’ll need original lines to reconstruct edits
    lines = src.splitlines(keepends=False)

    for node in tree.body:
        # Handle: import X [, Y] [as Z]
        if isinstance(node, ast.Import):
            # If *all* top-level names refer to local modules, we can rewrite:
            #   import pretty                -> from . import pretty
            #   import pretty as p           -> from . import pretty as p
            #   import pretty, parsing_utils -> from . import pretty, parsing_utils
            local_aliases = []
            non_local = False
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in local_modules or top == pkg_name:
                    local_aliases.append(alias)
                else:
                    non_local = True
                    break

            if non_local or not local_aliases:
                continue  # leave it

            # Build a single "from . import ..." line preserving aliases
            parts = []
            for alias in node.names:
                top = alias.name.split(".")[0]
                # Only rewrite locals, leave non-locals untouched (rare mixed case)
                if top in local_modules or top == pkg_name:
                    if alias.asname:
                        parts.append(f"{alias.name} as {alias.asname}")
                    else:
                        parts.append(alias.name)
                else:
                    # If we ever hit mixed, bail out and don't rewrite
                    parts = []
                    break
            if not parts:
                continue

            new_line = f"from . import {', '.join(parts)}"
            # Replace the entire original line (best-effort: single-line import)
            lineno = node.lineno - 1
            old = lines[lineno].strip()
            edits[lineno] = new_line
            notes.append(f"import→relative: `{old}`  ->  `{new_line}`")

        # Handle: from X import Y
        elif isinstance(node, ast.ImportFrom):
            # Already relative?
            if node.level and node.level > 0:
                continue

            if node.module is None:
                continue

            top = node.module.split(".")[0]

            # Case A: from vinefeeder.something import thing
            if top == pkg_name:
                # Convert to from .something import thing (preserve the tail)
                tail = node.module.split(".", 1)[1] if "." in node.module else ""
                dot_path = f".{tail}" if tail else "."
                names = []
                for alias in node.names:
                    if alias.asname:
                        names.append(f"{alias.name} as {alias.asname}")
                    else:
                        names.append(alias.name)
                new_line = f"from {dot_path} import {', '.join(names)}"
                lineno = node.lineno - 1
                old = lines[lineno].strip()
                edits[lineno] = new_line
                notes.append(f"abs→relative: `{old}`  ->  `{new_line}`")
                continue

            # Case B: from sibling import thing  (e.g., from pretty import foo)
            if top in local_modules:
                # Keep subpath if present (e.g., services.util)
                tail = node.module
                new_line = f"from .{tail} import " + ", ".join(
                    f"{n.name} as {n.asname}" if n.asname else n.name for n in node.names
                )
                lineno = node.lineno - 1
                old = lines[lineno].strip()
                edits[lineno] = new_line
                notes.append(f"sibling→relative: `{old}`  ->  `{new_line}`")

    if not edits:
        return src, notes

    # Apply edits (line-level replacement)
    new_lines = []
    for i, line in enumerate(lines):
        new_lines.append(edits.get(i, line))
    new_src = "\n".join(new_lines) + ("\n" if src.endswith("\n") else "")

    return new_src, notes


def iter_python_files(pkg_dir: Path) -> Iterable[Path]:
    for p in pkg_dir.rglob("*.py"):
        yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Path to the src/ root (e.g., packages/vinefeeder/src)")
    ap.add_argument("--pkg-name", required=True, help="Top-level package name (e.g., vinefeeder)")
    ap.add_argument("--apply", action="store_true", help="Rewrite files in place (creates .bak backups)")
    args = ap.parse_args()

    src_root = Path(args.root).resolve()
    pkg_dir = (src_root / args.pkg_name).resolve()

    if not pkg_dir.exists():
        print(f"[error] Package dir not found: {pkg_dir}")
        return 2

    local_modules = find_package_modules(pkg_dir)
    print(f"[info] Package: {args.pkg_name}")
    print(f"[info] Package dir: {pkg_dir}")
    print(f"[info] Local modules found: {sorted(local_modules)}")
    print()

    total_changes = 0

    for py in iter_python_files(pkg_dir):
        old = py.read_text(encoding="utf-8")
        new, notes = rewrite_imports(old, py, args.pkg_name, local_modules)
        if not notes:
            continue

        total_changes += 1
        print(f"--- {py}")
        for n in notes:
            print("  -", n)

        if args.apply and new != old:
            bak = py.with_suffix(py.suffix + ".bak")
            shutil.copy2(py, bak)
            py.write_text(new, encoding="utf-8")

            # Show a small unified diff for context
            diff = difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=str(py),
                tofile=str(py),
                lineterm=""
            )
            print("\n".join(diff))
        print()

    if total_changes == 0:
        print("[info] No imports to rewrite.")
    else:
        print(f"[done] Files with changes: {total_changes} {'(dry-run)' if not args.apply else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

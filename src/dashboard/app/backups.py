"""Backup copies for every file the dashboard writes.

A copy is <file>.bak_<YYYYMMDD_HHMMSS> in the kind's folder under backups/. Rotation keeps the newest 10 copies of
one file plus any copy younger than 7 days. Files that do not match that name (older copies, other files) are never
deleted here.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

KEEP_NEWEST = 10
KEEP_DAYS = 7
STAMP = re.compile(r"\.bak_(\d{8}_\d{6})(?:_\d+)?$")


def bak_pattern(filename: str) -> re.Pattern:
    return re.compile("^" + re.escape(filename) + r"\.bak_\d{8}_\d{6}(?:_\d+)?$")


def is_bak_name(name: str) -> bool:
    return STAMP.search(name) is not None


def bak_target(directory: Path, filename: str) -> Path:
    """A free name for a new copy. A second copy in the same second gets _1, _2, ..."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{filename}.bak_{stamp}"
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = directory / f"{filename}.bak_{stamp}_{suffix}"
    return candidate


def copies(directory: Path, filename: str) -> list[Path]:
    """This file's copies, newest first."""
    if not directory.is_dir():
        return []
    pattern = bak_pattern(filename)
    found = [item for item in directory.iterdir() if item.is_file() and pattern.match(item.name)]
    found.sort(key=_order, reverse=True)
    return found


def _order(path: Path):
    match = STAMP.search(path.name)
    stamp = match.group(1) if match else ""
    tail = path.name[match.end(1):] if match else ""
    number = int(tail[1:]) if tail.startswith("_") and tail[1:].isdigit() else 0
    return stamp, number


def rotate(directory: Path, filename: str, *, now: float | None = None) -> list[str]:
    """Delete copies beyond the newest 10 that are also older than 7 days. Returns the deleted names."""
    now = time.time() if now is None else now
    removed = []
    for index, item in enumerate(copies(directory, filename)):
        if index < KEEP_NEWEST:
            continue
        try:
            age = now - item.stat().st_mtime
        except OSError:
            continue
        if age <= KEEP_DAYS * 86400:
            continue
        try:
            item.unlink()
            removed.append(item.name)
        except OSError:
            pass
    return removed


def backup_file(path: Path, directory: Path) -> str | None:
    """Copy path into directory under a new bak name and rotate. None when there is nothing to copy."""
    path = Path(path)
    if not path.exists():
        return None
    directory.mkdir(parents=True, exist_ok=True)
    target = bak_target(directory, path.name)
    shutil.copyfile(path, target)
    rotate(directory, path.name)
    return target.name


def backup_text(text: str, directory: Path, filename: str) -> str:
    """Write text as a new copy of `filename` (for a value that is not a file, such as the workspace configuration)."""
    directory.mkdir(parents=True, exist_ok=True)
    target = bak_target(directory, filename)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    rotate(directory, filename)
    return target.name

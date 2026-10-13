"""Reading and writing files on this computer, limited to allowed folders."""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LocalPathError(Exception):
    pass


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned or "file"


def _check(path: Path, roots: list[Path]) -> Path:
    resolved = path.expanduser().resolve()
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    allowed = ", ".join(str(r) for r in roots) or "(none configured)"
    raise LocalPathError(f"{resolved} is outside the folders this server may use: {allowed}")


def read_file(path: str, roots: list[Path]) -> tuple[str, bytes]:
    p = _check(Path(path), roots)
    if not p.is_file():
        raise LocalPathError(f"No such file: {p}")
    return p.name, p.read_bytes()


def write_file(folder: str, filename: str, data: bytes, roots: list[Path], overwrite: bool = False) -> Path:
    """Write data into folder, adding ' (1)', ' (2)'… instead of overwriting."""
    target_dir = _check(Path(folder), roots)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    target = target_dir / name
    if not overwrite:
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            target = target_dir / f"{stem} ({n}){suffix}"
            n += 1
    target.write_bytes(data)
    return target


def delete_file(path: str, roots: list[Path]) -> None:
    """Delete one file inside the allowed folders. Folders are never deleted."""
    p = _check(Path(path), roots)
    if not p.is_file():
        raise LocalPathError(f"No such file: {p}")
    p.unlink()

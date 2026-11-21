"""Reading and writing files on this computer, limited to allowed folders."""

from __future__ import annotations

import os
import re
from pathlib import Path

from send2trash import send2trash

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
    stem, suffix, n = target.stem, target.suffix, 1
    while True:
        try:
            _write_new(target, data, replace=overwrite)
            return target
        except FileExistsError:
            if overwrite:
                raise
            target = target_dir / f"{stem} ({n}){suffix}"
            n += 1
        except OSError as exc:
            raise LocalPathError(f"Couldn't save {target}: {exc.strerror or exc}") from None


def _write_new(target: Path, data: bytes, replace: bool) -> None:
    """Write without following a symlink at `target` (even a broken one), so nothing lands
    outside the allowed folders. Without `replace`, an existing name raises FileExistsError."""
    if target.is_symlink():
        if not replace:
            raise FileExistsError(str(target))
        raise LocalPathError(f"{target} is a shortcut (symlink); not writing through it.")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def trash_file(path: str, roots: list[Path]) -> None:
    """Move one file inside the allowed folders to the Bin (Trash / Recycle Bin), so it can be
    put back. Folders are never removed."""
    p = _check(Path(path), roots)
    if not p.is_file():
        raise LocalPathError(f"No such file: {p}")
    try:
        send2trash(str(p))
    except Exception as exc:  # send2trash raises its own and OS errors
        raise LocalPathError(f"Couldn't move {p} to the Bin, so it was kept: {exc}") from None

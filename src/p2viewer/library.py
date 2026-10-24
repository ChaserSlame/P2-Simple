from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator


MEDIA_EXTENSIONS = frozenset(
    {
        ".avi",
        ".m2ts",
        ".mkv",
        ".mov",
        ".mp4",
        ".mxf",
        ".mts",
        ".wav",
    }
)

VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".m2ts",
        ".mkv",
        ".mov",
        ".mp4",
        ".mxf",
        ".mts",
    }
)


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size_bytes: int
    modified_at: datetime
    category: str


ScanProgress = Callable[[int, int, Path], None]


def _category_for(path: Path) -> str:
    parts = tuple(part.casefold() for part in path.parts)
    if any(
        parts[index : index + 2] == ("contents", "avclip")
        for index in range(len(parts) - 1)
    ):
        return "P2 / AVCLIP"
    if any(
        parts[index : index + 2] == ("contents", "clip")
        for index in range(len(parts) - 1)
    ):
        return "P2 / CLIP"
    if any(part == "avchd" for part in parts):
        return "AVCHD"
    return "Video"


def iter_media(
    root: Path,
    on_progress: ScanProgress | None = None,
) -> Iterator[MediaFile]:
    """Yield supported media files and report traversal progress when requested."""
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    pending = [root]
    directories_scanned = 0
    files_found = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries, start=1):
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and Path(entry.name).suffix.casefold() in MEDIA_EXTENSIONS
                ):
                    path = Path(entry.path)
                    stat = entry.stat(follow_symlinks=False)
                    files_found += 1
                    yield MediaFile(
                        path=path,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime),
                        category=_category_for(path),
                    )
                if on_progress is not None and index % 128 == 0:
                    on_progress(directories_scanned, files_found, directory)
        directories_scanned += 1
        if on_progress is not None:
            on_progress(directories_scanned, files_found, directory)


def scan_media(root: Path) -> list[MediaFile]:
    """Find supported media files under root in deterministic path order."""
    return sorted(
        iter_media(root),
        key=lambda item: str(item.path).casefold(),
    )

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from p2viewer.library import MediaFile, VIDEO_EXTENSIONS
from p2viewer.p2_metadata import P2ClipMetadata, P2ScanResult


@dataclass(frozen=True)
class ClipAssembly:
    video: MediaFile
    audio_files: tuple[Path, ...]
    metadata: P2ClipMetadata | None


def build_clip_assemblies(
    media_files: list[MediaFile],
    p2_result: P2ScanResult,
) -> list[ClipAssembly]:
    """Build logical video/audio groupings without writing media files."""
    assemblies: list[ClipAssembly] = []
    for video in media_files:
        if video.path.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        metadata = p2_result.by_media_path.get(video.path)
        if metadata is not None and metadata.video_file != video.path:
            continue
        assemblies.append(
            ClipAssembly(
                video=video,
                audio_files=metadata.audio_files if metadata is not None else (),
                metadata=metadata,
            )
        )
    return assemblies

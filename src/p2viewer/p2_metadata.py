from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable


MAX_CLIP_XML_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class P2ClipMetadata:
    clip_xml: Path
    clip_id: str
    global_clip_id: str | None
    clip_name: str | None
    duration_frames: int | None
    duration_seconds: Fraction | None
    edit_unit: str | None
    frame_rate: str | None
    start_timecode: str | None
    audio_track_count: int
    video_file: Path | None
    audio_files: tuple[Path, ...]


@dataclass(frozen=True)
class P2ScanResult:
    by_media_path: dict[Path, P2ClipMetadata]
    clips: tuple[P2ClipMetadata, ...]
    warnings: tuple[str, ...]


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    return next(
        (element for element in parent if _local_name(element) == name.casefold()),
        None,
    )


def _text(parent: ET.Element | None, name: str) -> str | None:
    element = _child(parent, name)
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _parse_clip_xml(path: Path) -> tuple[dict[str, object], int]:
    if path.stat().st_size > MAX_CLIP_XML_BYTES:
        raise ValueError(f"el XML supera el límite de {MAX_CLIP_XML_BYTES} bytes")
    data = path.read_bytes()
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)\b", data, re.IGNORECASE):
        raise ValueError("el XML contiene una declaración DTD o ENTITY no admitida")

    root = ET.fromstring(data)
    clip_content = root if _local_name(root) == "clipcontent" else _child(root, "ClipContent")
    if clip_content is None:
        raise ValueError("falta el elemento ClipContent")

    duration_text = _text(clip_content, "Duration")
    try:
        duration_frames = int(duration_text) if duration_text is not None else None
    except ValueError:
        duration_frames = None

    edit_unit = _text(clip_content, "EditUnit")
    duration_seconds: Fraction | None = None
    if duration_frames is not None and duration_frames >= 0 and edit_unit:
        try:
            duration_seconds = duration_frames * Fraction(edit_unit)
        except (ValueError, ZeroDivisionError):
            duration_seconds = None

    essence_list = _child(clip_content, "EssenceList")
    video = _child(essence_list, "Video")
    audio_track_count = (
        sum(1 for element in essence_list if _local_name(element) == "audio")
        if essence_list is not None
        else 0
    )

    metadata: dict[str, object] = {
        "global_clip_id": _text(clip_content, "GlobalClipID"),
        "clip_name": _text(clip_content, "ClipName"),
        "duration_frames": duration_frames,
        "duration_seconds": duration_seconds,
        "edit_unit": edit_unit,
        "frame_rate": _text(video, "FrameRate"),
        "start_timecode": _text(video, "StartTimecode"),
        "audio_track_count": audio_track_count,
    }
    return metadata, audio_track_count


def _media_index(media_paths: Iterable[Path]) -> dict[tuple[str, str], Path]:
    return {
        (str(path.parent).casefold(), path.name.casefold()): path
        for path in media_paths
    }


def _find_essence(
    index: dict[tuple[str, str], Path],
    folder: Path,
    basename: str,
) -> Path | None:
    return index.get((str(folder).casefold(), basename.casefold()))


def scan_p2_metadata(root: Path, media_paths: Iterable[Path]) -> P2ScanResult:
    """Read P2 Clip XML and link only essence files present in the scanned media."""
    root = root.expanduser().resolve(strict=True)
    media_index = _media_index(media_paths)
    clips: list[P2ClipMetadata] = []
    warnings: list[str] = []
    pending = [root]

    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                child_directories: list[Path] = []
                clip_xml_paths: list[Path] = []
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        child_directories.append(Path(entry.path))
                    elif (
                        directory.name.casefold() == "clip"
                        and entry.is_file(follow_symlinks=False)
                        and Path(entry.name).suffix.casefold() == ".xml"
                    ):
                        clip_xml_paths.append(Path(entry.path))
        except OSError as error:
            warnings.append(f"No se pudo leer {directory}: {error}")
            continue

        pending.extend(child_directories)
        for xml_path in clip_xml_paths:
            try:
                fields, audio_track_count = _parse_clip_xml(xml_path)
            except (OSError, ET.ParseError, ValueError) as error:
                warnings.append(f"No se pudo interpretar {xml_path.name}: {error}")
                continue

            content_root = xml_path.parent.parent
            clip_id = xml_path.stem
            video_file = _find_essence(
                media_index,
                content_root / "VIDEO",
                f"{clip_id}.MXF",
            )
            audio_files = tuple(
                audio_file
                for track_number in range(audio_track_count)
                if (
                    audio_file := _find_essence(
                        media_index,
                        content_root / "AUDIO",
                        f"{clip_id}{track_number:02d}.MXF",
                    )
                )
                is not None
            )
            clips.append(
                P2ClipMetadata(
                    clip_xml=xml_path,
                    clip_id=clip_id,
                    video_file=video_file,
                    audio_files=audio_files,
                    **fields,
                )
            )

    by_media_path: dict[Path, P2ClipMetadata] = {}
    ambiguous_paths: set[Path] = set()
    for clip in clips:
        for media_path in (clip.video_file, *clip.audio_files):
            if media_path is None:
                continue
            if media_path in by_media_path:
                ambiguous_paths.add(media_path)
                by_media_path.pop(media_path)
            elif media_path not in ambiguous_paths:
                by_media_path[media_path] = clip

    for media_path in sorted(ambiguous_paths):
        warnings.append(
            f"{media_path.name} está asociado a más de un XML P2; "
            "no se asignó una correspondencia automática."
        )

    return P2ScanResult(
        by_media_path=by_media_path,
        clips=tuple(clips),
        warnings=tuple(warnings),
    )

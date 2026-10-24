from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from p2viewer.p2_metadata import P2ClipMetadata


FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True)
class AudioSync:
    path: Path
    offset_seconds: Fraction | None
    timecode_offset_seconds: Fraction | None
    timecode: str | None
    duration_seconds: Fraction | None


@dataclass(frozen=True)
class SyncAnalysis:
    clip: P2ClipMetadata
    video_timecode: str | None
    video_duration_seconds: Fraction | None
    audio: tuple[AudioSync, ...]
    issues: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return (
            not self.issues
            and self.clip.video_file is not None
            and self.clip.audio_track_count > 0
            and len(self.audio) == self.clip.audio_track_count
            and all(track.offset_seconds is not None for track in self.audio)
        )

    @property
    def max_abs_offset_seconds(self) -> Fraction | None:
        offsets = [track.offset_seconds for track in self.audio]
        if not offsets or any(offset is None for offset in offsets):
            return None
        return max(abs(offset) for offset in offsets if offset is not None)


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    command: tuple[str, ...]


class MediaProbeError(RuntimeError):
    pass


def _fraction(value: object) -> Fraction | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def _timecode_seconds(value: str, edit_unit: str) -> Fraction:
    edit_unit_fraction = Fraction(edit_unit)
    if edit_unit_fraction <= 0:
        raise ValueError(f"EditUnit inválido: {edit_unit}")
    rate = 1 / edit_unit_fraction
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})", value)
    if match is None:
        raise ValueError(f"formato de timecode no admitido: {value}")

    hours, minutes, seconds, separator, frames = match.groups()
    h, m, s, f = int(hours), int(minutes), int(seconds), int(frames)
    nominal_rate = round(float(rate))
    if nominal_rate <= 0 or m >= 60 or s >= 60 or f >= nominal_rate:
        raise ValueError(f"timecode fuera de rango: {value}")

    total_frames = ((h * 60 + m) * 60 + s) * nominal_rate + f
    if separator == ";":
        dropped_frames_by_minute = {30: 2, 60: 4}.get(nominal_rate)
        if dropped_frames_by_minute is None or rate == nominal_rate:
            raise ValueError(f"timecode drop-frame no compatible con {edit_unit}")
        if m % 10 and s == 0 and f < dropped_frames_by_minute:
            raise ValueError(f"timecode drop-frame fuera de rango: {value}")
        total_minutes = h * 60 + m
        total_frames -= dropped_frames_by_minute * (
            total_minutes - total_minutes // 10
        )
    return Fraction(total_frames, 1) / rate


def _wrapped_delta(value: Fraction, reference: Fraction) -> Fraction:
    delta = value - reference
    day = Fraction(24 * 60 * 60)
    if delta > day / 2:
        return delta - day
    if delta < -day / 2:
        return delta + day
    return delta


def _probe(path: Path, executable: str) -> dict[str, object]:
    command = [
        executable,
        "-v",
        "error",
        "-show_entries",
        (
            "format=start_time,duration:format_tags=timecode,material_package_umid:"
            "stream=codec_type,codec_name,start_time,duration,sample_rate:"
            "stream_tags=data_type"
        ),
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise MediaProbeError(f"No se encontró {executable} en PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise MediaProbeError(f"ffprobe excedió el tiempo al leer {path.name}.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"ffprobe devolvió código {error.returncode}"
        raise MediaProbeError(f"{path.name}: {detail}") from error

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MediaProbeError(f"ffprobe devolvió JSON inválido para {path.name}.") from error


def _essence_stream(
    path: Path,
    kind: str,
    executable: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    probe = _probe(path, executable)
    streams = probe.get("streams")
    fmt = probe.get("format")
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        raise MediaProbeError(f"ffprobe no identificó streams válidos en {path.name}.")

    stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == kind
        ),
        None,
    )
    if stream is None:
        raise MediaProbeError(f"{path.name} no contiene un stream {kind}.")
    format_tags = fmt.get("tags")
    stream_tags = stream.get("tags")
    return (
        stream,
        format_tags if isinstance(format_tags, dict) else {},
        stream_tags if isinstance(stream_tags, dict) else {},
    )


def analyze_clip_sync(
    clip: P2ClipMetadata,
    ffprobe: str = "ffprobe",
) -> SyncAnalysis:
    issues: list[str] = []
    if clip.video_file is None:
        return SyncAnalysis(clip, None, None, (), ("No se encontró el MXF de video.",))
    if clip.audio_track_count == 0:
        return SyncAnalysis(clip, None, None, (), ("El XML P2 no declara pistas de audio.",))
    if not clip.start_timecode or not clip.edit_unit:
        return SyncAnalysis(
            clip,
            None,
            None,
            (),
            ("El XML P2 no contiene timecode inicial o EditUnit.",),
        )

    try:
        expected_timecode = _timecode_seconds(clip.start_timecode, clip.edit_unit)
        video_stream, video_tags, _ = _essence_stream(
            clip.video_file, "video", ffprobe
        )
        video_timecode = video_tags.get("timecode")
        video_time = (
            _timecode_seconds(str(video_timecode), clip.edit_unit)
            if video_timecode
            else None
        )
        video_start = _fraction(video_stream.get("start_time"))
        video_duration = _fraction(video_stream.get("duration"))
        video_umid = video_tags.get("material_package_umid")
        if video_time is None:
            issues.append("El MXF de video no informa timecode.")
        elif video_time != expected_timecode:
            issues.append(
                f"El timecode del MXF ({video_timecode}) no coincide con "
                f"el XML P2 ({clip.start_timecode})."
            )
        if video_start is None:
            issues.append("ffprobe no pudo determinar el inicio del stream de video.")
        if video_duration is None:
            issues.append("ffprobe no informó duración para el stream de video.")
        if not video_umid:
            issues.append("El MXF de video no informa material_package_umid.")
        if clip.duration_seconds is None or clip.duration_seconds <= 0:
            issues.append("El XML P2 no informa una duración válida.")
        elif video_duration is not None:
            frame_duration = Fraction(clip.edit_unit)
            tolerance = max(Fraction(1, 1000), frame_duration)
            if abs(video_duration - clip.duration_seconds) > tolerance:
                issues.append("La duración del video no coincide con el XML P2 dentro de un cuadro.")

        audio_results: list[AudioSync] = []
        if len(clip.audio_files) != clip.audio_track_count:
            issues.append(
                f"El XML declara {clip.audio_track_count} pistas de audio, "
                f"pero se encontraron {len(clip.audio_files)} esencias."
            )

        for audio_path in clip.audio_files:
            try:
                stream, tags, _ = _essence_stream(audio_path, "audio", ffprobe)
            except MediaProbeError as error:
                issues.append(str(error))
                audio_results.append(AudioSync(audio_path, None, None, None, None))
                continue

            audio_timecode_value = tags.get("timecode")
            audio_timecode: str | None = (
                str(audio_timecode_value) if audio_timecode_value else None
            )
            audio_start = _fraction(stream.get("start_time"))
            audio_duration = _fraction(stream.get("duration"))
            audio_umid = tags.get("material_package_umid")
            timecode_delta: Fraction | None = None
            offset: Fraction | None = None

            if not audio_timecode:
                issues.append(f"{audio_path.name} no informa timecode.")
            else:
                try:
                    audio_time = _timecode_seconds(
                        audio_timecode,
                        clip.edit_unit,
                    )
                    timecode_delta = _wrapped_delta(audio_time, expected_timecode)
                except ValueError as error:
                    issues.append(f"{audio_path.name}: {error}")

            if audio_start is None:
                issues.append(
                    f"ffprobe no pudo determinar el inicio de audio en {audio_path.name}."
                )
            if video_start is not None and audio_start is not None and timecode_delta is not None:
                offset = timecode_delta + audio_start - video_start

            if not audio_umid or not video_umid or str(audio_umid).casefold() != str(
                video_umid
            ).casefold():
                issues.append(
                    f"{audio_path.name} no comparte material_package_umid "
                    "con el video."
                )

            if audio_duration is None:
                issues.append(f"ffprobe no informó duración para {audio_path.name}.")
            elif clip.duration_seconds is not None:
                frame_duration = Fraction(clip.edit_unit)
                tolerance = max(Fraction(1, 1000), frame_duration)
                if abs(audio_duration - clip.duration_seconds) > tolerance:
                    issues.append(
                        f"La duración de {audio_path.name} no coincide con "
                        "el XML P2 dentro de un cuadro."
                    )
                if (
                    video_duration is not None
                    and abs(audio_duration - video_duration) > tolerance
                ):
                    issues.append(
                        f"La duración de {audio_path.name} no coincide con "
                        "el video dentro de un cuadro."
                    )

            audio_results.append(
                AudioSync(
                    path=audio_path,
                    offset_seconds=offset,
                    timecode_offset_seconds=timecode_delta,
                    timecode=audio_timecode,
                    duration_seconds=audio_duration,
                )
            )

        return SyncAnalysis(
            clip=clip,
            video_timecode=str(video_timecode) if video_timecode else None,
            video_duration_seconds=video_duration,
            audio=tuple(audio_results),
            issues=tuple(dict.fromkeys(issues)),
        )
    except (MediaProbeError, ValueError, ZeroDivisionError) as error:
        return SyncAnalysis(
            clip,
            None,
            None,
            (),
            (str(error),),
        )


def _offset_argument(value: Fraction) -> str:
    return f"{float(value):.9f}"


def mxf_output_name(clip_id: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", clip_id).strip("._")
    if not safe_stem:
        raise ValueError("El ID de clip no puede usarse como nombre de salida.")
    return f"{safe_stem}.mxf"


def build_synced_mxf(
    analysis: SyncAnalysis,
    output_directory: Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> BuildResult:
    if not analysis.eligible:
        raise ValueError("El clip no pasó la validación de sincronía P2.")

    output_directory = output_directory.expanduser().resolve(strict=True)
    if not output_directory.is_dir():
        raise NotADirectoryError(f"No es una carpeta: {output_directory}")
    clip = analysis.clip
    assert clip.video_file is not None

    output_path = output_directory / mxf_output_name(clip.clip_id)
    if output_path.exists():
        raise FileExistsError(f"La salida ya existe y no se sobrescribirá: {output_path}")

    tc_offsets = [
        track.timecode_offset_seconds
        for track in analysis.audio
        if track.timecode_offset_seconds is not None
    ]
    base_offset = min([Fraction(0), *tc_offsets])
    video_input_offset = -base_offset

    if video_input_offset < 0:
        raise ValueError("El cálculo de timecode produjo un desplazamiento inválido.")

    temporary_path = output_directory / (
        f".{output_path.stem}.{uuid.uuid4().hex}.building.mxf"
    )
    command = [ffmpeg, "-nostdin", "-hide_banner", "-n", "-loglevel", "error"]
    if video_input_offset:
        command.extend(("-itsoffset", _offset_argument(video_input_offset)))
    command.extend(("-i", str(clip.video_file)))

    for input_index, track in enumerate(analysis.audio, start=1):
        assert track.timecode_offset_seconds is not None
        input_offset = track.timecode_offset_seconds - base_offset
        if input_offset < 0:
            raise ValueError(
                f"El timecode de {track.path.name} no se pudo normalizar."
            )
        if input_offset:
            command.extend(("-itsoffset", _offset_argument(input_offset)))
        command.extend(("-i", str(track.path)))

    command.extend(("-map", "0:v:0"))
    for input_index in range(1, len(analysis.audio) + 1):
        command.extend(("-map", f"{input_index}:a:0"))
    command.extend(
        (
            "-map_metadata",
            "0",
            "-metadata",
            f"title={clip.clip_name or clip.clip_id}",
            "-metadata",
            f"timecode={analysis.video_timecode or clip.start_timecode}",
            "-c",
            "copy",
            "-f",
            "mxf",
            str(temporary_path),
        )
    )

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        del result
        output_analysis = _probe_output(temporary_path, ffprobe)
        _validate_output(analysis, output_analysis)
        os.link(temporary_path, output_path)
        temporary_path.unlink()
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"ffmpeg excedió el tiempo al construir {clip.clip_id}.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"ffmpeg devolvió código {error.returncode}"
        raise RuntimeError(f"No se pudo construir {clip.clip_id}: {detail}") from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return BuildResult(output_path=output_path, command=tuple(command))


def _probe_output(path: Path, executable: str) -> dict[str, object]:
    return _probe(path, executable)


def _validate_output(
    analysis: SyncAnalysis,
    probe: dict[str, object],
) -> None:
    streams = probe.get("streams")
    fmt = probe.get("format")
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        raise MediaProbeError("No se pudieron verificar los streams del MXF generado.")

    video_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audio_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1:
        raise MediaProbeError("El MXF generado no contiene exactamente un stream de video.")
    if len(audio_streams) != len(analysis.audio):
        raise MediaProbeError(
            f"El MXF generado contiene {len(audio_streams)} pistas de audio; "
            f"se esperaban {len(analysis.audio)}."
        )

    video_start = _fraction(video_streams[0].get("start_time"))
    if video_start is None:
        raise MediaProbeError("No se pudo verificar el inicio del video generado.")
    video_duration = _fraction(video_streams[0].get("duration"))
    if video_duration is None or analysis.video_duration_seconds is None:
        raise MediaProbeError("No se pudo verificar la duración del video generado.")
    tolerance = Fraction(1, 1000)
    if abs(video_duration - analysis.video_duration_seconds) > tolerance:
        raise MediaProbeError("La duración del video generado no coincide con el original.")

    for index, audio_stream in enumerate(audio_streams):
        audio_start = _fraction(audio_stream.get("start_time"))
        audio_duration = _fraction(audio_stream.get("duration"))
        expected_offset = analysis.audio[index].offset_seconds
        if audio_start is None or expected_offset is None:
            raise MediaProbeError(
                f"No se pudo verificar el inicio de la pista de audio {index + 1}."
            )
        expected_duration = analysis.audio[index].duration_seconds
        if (
            audio_duration is None
            or expected_duration is None
            or abs(audio_duration - expected_duration) > tolerance
        ):
            raise MediaProbeError(
                f"La duración de la pista de audio {index + 1} no coincide "
                "con el original."
            )
        if abs((audio_start - video_start) - expected_offset) > Fraction(1, 1000):
            raise MediaProbeError(
                f"La pista de audio {index + 1} no quedó con el desfase esperado."
            )

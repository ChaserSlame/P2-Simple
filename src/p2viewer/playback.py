from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from p2viewer.synchronization import AudioSync


PREVIEW_RENDER_TIMEOUT_SECONDS = 60 * 30
logger = logging.getLogger(__name__)


class PreviewRenderError(RuntimeError):
    pass


class PreviewRenderCancelled(RuntimeError):
    pass


class _FFmpegEncodingError(PreviewRenderError):
    pass


def render_playback_proxy(
    video_path: Path,
    audio_tracks: tuple[AudioSync, ...],
    output_path: Path,
    ffmpeg: str = "ffmpeg",
    cancel_event: threading.Event | None = None,
) -> Path:
    """Render a low-resolution temporary playback file with P2 audio mixed in."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    offsets = [track.timecode_offset_seconds for track in audio_tracks]
    if any(offset is None for offset in offsets):
        raise ValueError("No se puede previsualizar audio con desfases desconocidos.")

    known_offsets = [offset for offset in offsets if offset is not None]
    base_offset = min([0, *known_offsets])
    video_offset = -base_offset

    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-n",
        "-loglevel",
        "error",
    ]
    if video_offset:
        command.extend(("-itsoffset", f"{float(video_offset):.9f}"))
    command.extend(("-i", str(video_path)))

    for track in audio_tracks:
        offset = track.timecode_offset_seconds
        assert offset is not None
        input_offset = offset - base_offset
        if input_offset:
            command.extend(("-itsoffset", f"{float(input_offset):.9f}"))
        command.extend(("-i", str(track.path)))

    command.extend(("-map", "0:v:0"))
    if audio_tracks:
        audio_inputs = "".join(
            f"[{index}:a:0]" for index in range(1, len(audio_tracks) + 1)
        )
        command.extend(
            (
                "-filter_complex",
                f"{audio_inputs}amix=inputs={len(audio_tracks)}:"
                "duration=longest:dropout_transition=0:normalize=1,"
                "aresample=48000[aout]",
                "-map",
                "[aout]",
            )
        )

    video_copy_path = output_path.with_name(f"{output_path.stem}-copy.mov")
    copy_command = [
        *command,
        "-map_metadata",
        "0",
        "-c:v",
        "copy",
    ]
    if audio_tracks:
        copy_command.extend(("-c:a", "aac", "-b:a", "192k", "-ac", "2"))
    copy_command.extend(("-movflags", "+faststart", str(video_copy_path)))

    render_started = time.perf_counter()
    try:
        try:
            copy_started = time.perf_counter()
            _run_ffmpeg(
                copy_command,
                video_path,
                cancel_event,
            )
            _validate_output(video_copy_path, video_path)
            logger.info(
                "Preview video stream copy completed: video=%s elapsed=%.3fs "
                "size_mb=%.2f",
                video_path.name,
                time.perf_counter() - copy_started,
                video_copy_path.stat().st_size / (1024 * 1024),
            )
            final_path = video_copy_path
        except _FFmpegEncodingError as error:
            _remove_partial_output(video_copy_path, error)
            logger.warning(
                "Preview stream copy unavailable: video=%s reason=%s; "
                "falling back to video transcode",
                video_path.name,
                error,
            )
            transcode_command = [
                *command,
                "-vf",
                "scale=640:360:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-pix_fmt",
                "yuv420p",
            ]
            if audio_tracks:
                transcode_command.extend(("-c:a", "aac", "-b:a", "192k", "-ac", "2"))
            transcode_command.extend(("-movflags", "+faststart", str(output_path)))
            _run_ffmpeg(
                transcode_command,
                video_path,
                cancel_event,
            )
            _validate_output(output_path, video_path)
            final_path = output_path
    except (PreviewRenderCancelled, PreviewRenderError) as error:
        _remove_partial_output(video_copy_path, error)
        _remove_partial_output(output_path, error)
        raise
    finally:
        logger.info(
            "Preview render finished: video=%s audio_tracks=%d elapsed=%.3fs",
            video_path.name,
            len(audio_tracks),
            time.perf_counter() - render_started,
        )

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise PreviewRenderCancelled("Se canceló la generación de preview.")
        if not final_path.is_file() or final_path.stat().st_size == 0:
            raise PreviewRenderError(
                f"ffmpeg no generó una preview válida para {video_path.name}."
            )
    except (PreviewRenderCancelled, PreviewRenderError) as error:
        _remove_partial_output(final_path, error)
        raise
    return final_path


def _run_ffmpeg(
    command: list[str],
    video_path: Path,
    cancel_event: threading.Event | None,
) -> None:
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            deadline = time.monotonic() + PREVIEW_RENDER_TIMEOUT_SECONDS
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise PreviewRenderCancelled("Se canceló la generación de preview.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.communicate()
                    raise PreviewRenderError(
                        f"ffmpeg excedió el tiempo al preparar {video_path.name}."
                    )
                try:
                    _, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                detail = stderr.strip() or f"ffmpeg devolvió código {process.returncode}"
                raise _FFmpegEncodingError(
                    f"No se pudo preparar la preview de {video_path.name}: {detail}"
                )
    except (PreviewRenderCancelled, PreviewRenderError):
        raise
    except FileNotFoundError as error:
        raise PreviewRenderError("No se encontró ffmpeg en PATH.") from error
    except OSError as error:
        raise PreviewRenderError(f"No se pudo ejecutar ffmpeg: {error}") from error


def _validate_output(output_path: Path, video_path: Path) -> None:
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise PreviewRenderError(
            f"ffmpeg no generó una preview válida para {video_path.name}."
        )


def _remove_partial_output(output_path: Path, cause: Exception) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError as error:
        raise PreviewRenderError(
            f"{cause} No se pudo eliminar el archivo temporal {output_path}: {error}"
        ) from error

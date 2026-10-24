from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from p2viewer.playback import (
    PreviewRenderError,
    render_playback_proxy,
    render_preview_audio,
    render_preview_thumbnail,
)
from p2viewer.synchronization import AudioSync


class _SuccessfulProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.returncode = 0

    def __enter__(self) -> _SuccessfulProcess:
        Path(self.command[-1]).write_bytes(b"preview")
        return self

    def __exit__(self, *_args: object) -> None:
        del _args
        return None

    def communicate(self, timeout: float) -> tuple[None, str]:
        del timeout
        return None, ""


class _FailedProcess(_SuccessfulProcess):
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.returncode = 1

    def communicate(self, timeout: float) -> tuple[None, str]:
        del timeout
        return None, "unsupported video codec"


class PlaybackProxyTests(unittest.TestCase):
    def test_audio_mix_rejects_unmeasured_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            track = AudioSync(
                Path(temporary_directory) / "audio.mxf",
                Fraction(0),
                None,
                None,
                Fraction(10),
            )
            with patch("p2viewer.playback._run_ffmpeg") as run_ffmpeg:
                with self.assertRaisesRegex(ValueError, "desfases desconocidos"):
                    render_preview_audio(
                        (track,),
                        Path(temporary_directory) / "audio.wav",
                    )
            run_ffmpeg.assert_not_called()

    def test_audio_mix_applies_positive_and_negative_offsets_without_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audio_tracks = (
                AudioSync(
                    root / "audio00.mxf",
                    Fraction(0),
                    Fraction(2, 25),
                    "00:00:00:02",
                    Fraction(10),
                ),
                AudioSync(
                    root / "audio01.mxf",
                    Fraction(0),
                    Fraction(-1, 25),
                    "23:59:59:24",
                    Fraction(10),
                ),
            )
            output = root / "audio-mix.wav"

            def write_audio_mix(
                command: list[str],
                video_path: Path,
                cancel_event: object,
            ) -> None:
                del video_path, cancel_event
                Path(command[-1]).write_bytes(b"wav")

            with patch(
                "p2viewer.playback._run_ffmpeg",
                side_effect=write_audio_mix,
            ) as run_ffmpeg:
                result = render_preview_audio(audio_tracks, output)

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"wav")
            command = run_ffmpeg.call_args.args[0]
            filter_complex = command[command.index("-filter_complex") + 1]
            self.assertIn("adelay=3840S:all=1", filter_complex)
            self.assertIn("atrim=start=0.040000000", filter_complex)
            self.assertIn("amix=inputs=2", filter_complex)
            self.assertIn("pcm_s16le", command)
            self.assertEqual(command.count("-i"), len(audio_tracks))

    def test_thumbnail_extracts_one_scaled_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "thumbnail.jpg"

            def write_thumbnail(
                command: list[str],
                video_path: Path,
                cancel_event: object,
            ) -> None:
                del video_path, cancel_event
                Path(command[-1]).write_bytes(b"thumbnail")

            with patch(
                "p2viewer.playback._run_ffmpeg",
                side_effect=write_thumbnail,
            ) as run_ffmpeg:
                result = render_preview_thumbnail(root / "video.mxf", output)

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"thumbnail")
            command = run_ffmpeg.call_args.args[0]
            self.assertIn("0:v:0", command)
            self.assertIn("1", command)
            self.assertTrue(any(argument.startswith("scale=600:400") for argument in command))

    def test_proxy_mixes_verified_audio_and_applies_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "video.mxf"
            audio = (
                AudioSync(
                    path=root / "audio00.mxf",
                    offset_seconds=Fraction(0),
                    timecode_offset_seconds=Fraction(0),
                    timecode="00:00:00:00",
                    duration_seconds=Fraction(10),
                ),
                AudioSync(
                    path=root / "audio01.mxf",
                    offset_seconds=Fraction(2, 25),
                    timecode_offset_seconds=Fraction(2, 25),
                    timecode="00:00:00:02",
                    duration_seconds=Fraction(10),
                ),
            )
            output = root / "preview.mp4"

            with patch(
                "p2viewer.playback.subprocess.Popen",
                side_effect=lambda command, **kwargs: self._successful_process(
                    command,
                    kwargs,
                ),
            ) as popen:
                result = render_playback_proxy(video, audio, output)

            self.assertEqual(result, output.with_name("preview-copy.mov"))
            self.assertEqual(result.read_bytes(), b"preview")
            command = popen.call_args.args[0]
            self.assertIn("0.080000000", command)
            self.assertTrue(
                any(
                    argument.startswith("[1:a:0][2:a:0]amix=inputs=2")
                    for argument in command
                )
            )
            self.assertIn("copy", command)
            self.assertNotIn("-vf", command)
            self.assertEqual(result.suffix, ".mov")
            self.assertIn("aac", command)

    def test_proxy_rejects_unmeasured_audio_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audio = (
                AudioSync(
                    path=root / "audio00.mxf",
                    offset_seconds=None,
                    timecode_offset_seconds=None,
                    timecode=None,
                    duration_seconds=None,
                ),
            )
            with patch("p2viewer.playback.subprocess.Popen") as popen:
                with self.assertRaises(ValueError):
                    render_playback_proxy(root / "video.mxf", audio, root / "preview.mp4")
            popen.assert_not_called()

    def test_proxy_reports_ffmpeg_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch(
                "p2viewer.playback.subprocess.Popen",
                side_effect=lambda command, **kwargs: self._failed_process(
                    command,
                    kwargs,
                ),
            ):
                with self.assertRaisesRegex(
                    PreviewRenderError,
                    "unsupported video codec",
                ):
                    render_playback_proxy(
                        root / "video.mxf",
                        (),
                        root / "preview.mp4",
                    )

    def test_copy_failure_falls_back_to_scaled_mpeg4_and_keeps_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "video.mxf"
            audio = (
                AudioSync(
                    path=root / "audio00.mxf",
                    offset_seconds=Fraction(0),
                    timecode_offset_seconds=Fraction(0),
                    timecode="00:00:00:00",
                    duration_seconds=Fraction(10),
                ),
            )
            attempts: list[list[str]] = []

            def fail_copy_then_transcode(
                command: list[str],
                **kwargs: object,
            ) -> _SuccessfulProcess:
                attempts.append(command)
                if len(attempts) == 1:
                    return self._failed_process(command, kwargs)
                return self._successful_process(command, kwargs)

            with patch(
                "p2viewer.playback.subprocess.Popen",
                side_effect=fail_copy_then_transcode,
            ):
                result = render_playback_proxy(
                    video,
                    audio,
                    root / "preview.mp4",
                )

            self.assertEqual(result.suffix, ".mp4")
            self.assertEqual(len(attempts), 2)
            transcode_command = attempts[1]
            self.assertIn("mpeg4", transcode_command)
            self.assertIn("aac", transcode_command)

    @staticmethod
    def _successful_process(
        command: list[str],
        kwargs: dict[str, object],
    ) -> _SuccessfulProcess:
        if kwargs and set(kwargs) != {"stdout", "stderr", "text"}:
            raise AssertionError(f"Unexpected Popen options: {kwargs}")
        return _SuccessfulProcess(command)

    @staticmethod
    def _failed_process(
        command: list[str],
        kwargs: dict[str, object],
    ) -> _FailedProcess:
        if set(kwargs) != {"stdout", "stderr", "text"}:
            raise AssertionError(f"Unexpected Popen options: {kwargs}")
        return _FailedProcess(command)


if __name__ == "__main__":
    unittest.main()

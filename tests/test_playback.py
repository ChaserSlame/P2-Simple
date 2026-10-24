from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from p2viewer.playback import PreviewRenderError, render_playback_proxy
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

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"preview")
            command = popen.call_args.args[0]
            self.assertIn("0.080000000", command)
            self.assertTrue(
                any(
                    argument.startswith("[1:a:0][2:a:0]amix=inputs=2")
                    for argument in command
                )
            )
            self.assertIn("mpeg4", command)
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

    @staticmethod
    def _successful_process(command: list[str], kwargs: dict[str, object]) -> _SuccessfulProcess:
        if set(kwargs) != {"stdout", "stderr", "text"}:
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

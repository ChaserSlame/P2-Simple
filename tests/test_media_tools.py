from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from p2viewer.media_tools import resolve_media_tool
from p2viewer.synchronization import MediaProbeError, _probe


class MediaToolResolutionTests(unittest.TestCase):
    def test_source_run_keeps_path_lookup(self) -> None:
        with patch("p2viewer.media_tools.sys", frozen=False, create=True):
            self.assertEqual(resolve_media_tool("ffmpeg"), "ffmpeg")
            self.assertEqual(resolve_media_tool("ffprobe"), "ffprobe")

    def test_frozen_windows_run_uses_tools_in_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            ffmpeg_directory = bundle_root / "ffmpeg"
            ffmpeg_directory.mkdir()
            ffmpeg_path = ffmpeg_directory / "ffmpeg.exe"
            ffprobe_path = ffmpeg_directory / "ffprobe.exe"
            ffmpeg_path.touch()
            ffprobe_path.touch()

            with (
                patch("p2viewer.media_tools.sys.frozen", True, create=True),
                patch(
                    "p2viewer.media_tools.sys._MEIPASS",
                    str(bundle_root),
                    create=True,
                ),
                patch("p2viewer.media_tools.sys.platform", "win32"),
            ):
                self.assertEqual(resolve_media_tool("ffmpeg"), str(ffmpeg_path))
                self.assertEqual(resolve_media_tool("ffprobe"), str(ffprobe_path))

    def test_frozen_run_reports_a_missing_bundled_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch("p2viewer.media_tools.sys.frozen", True, create=True),
                patch(
                    "p2viewer.media_tools.sys._MEIPASS",
                    temporary_directory,
                    create=True,
                ),
                patch("p2viewer.media_tools.sys.platform", "win32"),
            ):
                with self.assertRaisesRegex(
                    FileNotFoundError,
                    "No se encontró ffmpeg.exe",
                ):
                    resolve_media_tool("ffmpeg")

    def test_probe_reports_a_missing_bundled_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch("p2viewer.media_tools.sys.frozen", True, create=True),
                patch(
                    "p2viewer.media_tools.sys._MEIPASS",
                    temporary_directory,
                    create=True,
                ),
                patch("p2viewer.media_tools.sys.platform", "win32"),
            ):
                with self.assertRaisesRegex(
                    MediaProbeError,
                    "No se encontró ffprobe.exe",
                ):
                    _probe(Path("video.mxf"), "ffprobe")

    def test_custom_executable_path_is_preserved(self) -> None:
        custom_path = r"C:\tools\custom-ffmpeg.exe"
        with patch("p2viewer.media_tools.sys.frozen", True, create=True):
            self.assertEqual(resolve_media_tool(custom_path), custom_path)

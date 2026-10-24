from __future__ import annotations

import tempfile
import unittest
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from p2viewer.app import MainWindow
from p2viewer.synchronization import AudioSync


class PreviewCacheTests(unittest.TestCase):
    def test_cache_key_changes_when_source_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video_path = root / "video.mxf"
            audio_path = root / "audio.mxf"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")
            audio_tracks = (
                AudioSync(
                    audio_path,
                    Fraction(0),
                    Fraction(0),
                    "00:00:00:00",
                    Fraction(1),
                ),
            )

            original_key = MainWindow._preview_cache_key(video_path, audio_tracks)
            audio_path.write_bytes(b"updated audio")
            updated_key = MainWindow._preview_cache_key(video_path, audio_tracks)

        self.assertNotEqual(original_key, updated_key)

    def test_cache_key_changes_when_audio_offset_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video_path = root / "video.mxf"
            audio_path = root / "audio.mxf"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")
            first_track = AudioSync(
                audio_path,
                Fraction(0),
                Fraction(0),
                "00:00:00:00",
                Fraction(1),
            )
            shifted_track = AudioSync(
                audio_path,
                Fraction(1, 25),
                Fraction(1, 25),
                "00:00:00:01",
                Fraction(1),
            )

            first_key = MainWindow._preview_cache_key(video_path, (first_track,))
            shifted_key = MainWindow._preview_cache_key(video_path, (shifted_track,))

        self.assertNotEqual(first_key, shifted_key)

    def test_cached_preview_is_reused_without_starting_a_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video_path = root / "video.mxf"
            video_path.write_bytes(b"video")
            cached_preview = root / "cached.mp4"
            cached_preview.write_bytes(b"preview")
            audio_tracks: tuple[AudioSync, ...] = ()
            cache_key = MainWindow._preview_cache_key(video_path, audio_tracks)
            activate_preview = Mock()
            fake_window = SimpleNamespace(
                _preview_render_thread=None,
                _wanted_playback=("token", video_path, audio_tracks),
                _preview_cache_key=MainWindow._preview_cache_key,
                _preview_cache=OrderedDict(((cache_key, cached_preview),)),
                _activate_playback_preview=activate_preview,
            )

            with patch("p2viewer.app._PlaybackProxyThread") as render_thread:
                MainWindow._start_playback_render(fake_window)

        activate_preview.assert_called_once_with(
            "token",
            cached_preview,
            cache_hit=True,
        )
        render_thread.assert_not_called()

    def test_large_proxy_is_not_kept_in_session_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            preview_path = Path(temporary_directory) / "large.mov"
            preview_path.write_bytes(b"large proxy")
            fake_window = SimpleNamespace(
                _preview_cache=OrderedDict(),
                _active_preview_path=None,
                _delete_preview_file=Mock(),
            )
            key = (("video", 1, 1), ())
            with patch("p2viewer.app.PREVIEW_CACHE_MAX_BYTES", 4):
                MainWindow._cache_preview(fake_window, key, preview_path)

        self.assertEqual(fake_window._preview_cache, {})
        fake_window._delete_preview_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()

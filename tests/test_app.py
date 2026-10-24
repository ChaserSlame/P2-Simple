from __future__ import annotations

import tempfile
import unittest
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from p2viewer.app import MainWindow
from p2viewer.p2_metadata import P2ClipMetadata
from p2viewer.synchronization import AudioSync


class ClipDurationDisplayTests(unittest.TestCase):
    def test_duration_uses_p2_metadata_seconds(self) -> None:
        metadata = P2ClipMetadata(
            clip_xml=Path("clip.xml"),
            clip_id="clip",
            global_clip_id=None,
            clip_name=None,
            duration_frames=474,
            duration_seconds=Fraction(474, 25),
            edit_unit=None,
            frame_rate="25",
            start_timecode=None,
            audio_track_count=0,
            video_file=None,
            audio_files=(),
        )

        self.assertEqual(
            MainWindow._format_clip_table_duration(metadata),
            "18.960 s",
        )

    def test_duration_falls_back_to_frames_or_dash(self) -> None:
        base_metadata = {
            "clip_xml": Path("clip.xml"),
            "clip_id": "clip",
            "global_clip_id": None,
            "clip_name": None,
            "duration_seconds": None,
            "edit_unit": None,
            "frame_rate": None,
            "start_timecode": None,
            "audio_track_count": 0,
            "video_file": None,
            "audio_files": (),
        }
        frame_duration = P2ClipMetadata(
            **base_metadata,
            duration_frames=474,
        )
        missing_duration = P2ClipMetadata(
            **base_metadata,
            duration_frames=None,
        )

        self.assertEqual(
            MainWindow._format_clip_table_duration(frame_duration),
            "474 cuadros",
        )
        self.assertEqual(
            MainWindow._format_clip_table_duration(missing_duration),
            "—",
        )


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

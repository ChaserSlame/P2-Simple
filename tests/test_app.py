from __future__ import annotations

import csv
import tempfile
import unittest
from collections import OrderedDict
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from p2viewer.app import MainWindow
from p2viewer.clips import ClipAssembly
from p2viewer.library import MediaFile
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

    def test_clock_display_uses_hours_minutes_and_seconds(self) -> None:
        self.assertEqual(
            MainWindow._format_timecode_clock("01:02:03:12"),
            "01:02:03",
        )
        self.assertEqual(
            MainWindow._format_timecode_clock("01:02:03;12"),
            "01:02:03",
        )
        self.assertEqual(
            MainWindow._format_timecode_clock("invalid"),
            "—",
        )
        self.assertEqual(
            MainWindow._format_duration_clock(Fraction(19)),
            "00:00:19",
        )
        self.assertEqual(
            MainWindow._format_duration_clock(Fraction(199, 10)),
            "00:00:20",
        )
        self.assertEqual(
            MainWindow._format_duration_clock(Fraction(3601)),
            "01:00:01",
        )

    def test_clip_row_summary_includes_timecode_and_duration_not_clip_id(self) -> None:
        metadata = P2ClipMetadata(
            clip_xml=Path("clip.xml"),
            clip_id="INTERNAL-ID",
            global_clip_id=None,
            clip_name="Coastal aerial",
            duration_frames=474,
            duration_seconds=Fraction(474, 25),
            edit_unit="1/25",
            frame_rate="25",
            start_timecode="01:02:03:12",
            audio_track_count=0,
            video_file=None,
            audio_files=(),
        )

        summary = MainWindow._format_clip_row_summary(metadata, None)

        self.assertIn("TC 01:02:03", summary)
        self.assertIn("Dur. 00:00:19", summary)
        self.assertIn("25p", summary)
        self.assertNotIn("INTERNAL-ID", summary)

    def test_duration_fallback_handles_invalid_edit_unit(self) -> None:
        metadata = P2ClipMetadata(
            clip_xml=Path("clip.xml"),
            clip_id="clip",
            global_clip_id=None,
            clip_name=None,
            duration_frames=100,
            duration_seconds=None,
            edit_unit="invalid",
            frame_rate=None,
            start_timecode=None,
            audio_track_count=0,
            video_file=None,
            audio_files=(),
        )

        self.assertIsNone(MainWindow._clip_duration_seconds(metadata, None))


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
            audio_path = root / "audio.mxf"
            audio_path.write_bytes(b"audio")
            cached_preview = root / "cached.wav"
            cached_preview.write_bytes(b"preview")
            audio_tracks = (
                AudioSync(
                    audio_path,
                    Fraction(0),
                    Fraction(0),
                    "00:00:00:00",
                    Fraction(1),
                ),
            )
            cache_key = MainWindow._preview_cache_key(video_path, audio_tracks)
            activate_preview = Mock()
            fake_window = SimpleNamespace(
                _preview_render_thread=None,
                _wanted_playback=("token", video_path, audio_tracks),
                _preview_cache_key=MainWindow._preview_cache_key,
                _preview_cache=OrderedDict(((cache_key, cached_preview),)),
                _activate_playback_preview=activate_preview,
            )

            with patch("p2viewer.app._PreviewAssetsThread") as render_thread:
                MainWindow._start_playback_render(fake_window)

        activate_preview.assert_called_once_with(
            "token",
            cached_preview,
            cache_hit=True,
        )
        render_thread.assert_not_called()

    def test_large_audio_mix_is_not_kept_in_session_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            preview_path = Path(temporary_directory) / "large.wav"
            preview_path.write_bytes(b"large audio mix")
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


class PlaybackShortcutBehaviorTests(unittest.TestCase):
    def test_seek_shortcut_moves_video_and_ready_audio_by_one_second(self) -> None:
        video_player = Mock()
        video_player.duration.return_value = 5000
        video_player.position.return_value = 2500
        audio_player = Mock()
        fake_window = SimpleNamespace(
            _player=video_player,
            _audio_player=audio_player,
            _audio_ready_token="token",
            _active_preview_token="token",
        )

        MainWindow._seek_playback_by(fake_window, -1000)

        video_player.setPosition.assert_called_once_with(1500)
        audio_player.setPosition.assert_called_once_with(1500)

    def test_seek_shortcut_clamps_to_media_duration(self) -> None:
        video_player = Mock()
        video_player.duration.return_value = 5000
        video_player.position.return_value = 4800
        fake_window = SimpleNamespace(
            _player=video_player,
            _audio_player=Mock(),
            _audio_ready_token=None,
            _active_preview_token="token",
        )

        MainWindow._seek_playback_by(fake_window, 1000)

        video_player.setPosition.assert_called_once_with(5000)
        fake_window._audio_player.setPosition.assert_not_called()

    def test_muting_preserves_volume_slider_level_for_unmute(self) -> None:
        fake_window = SimpleNamespace(
            _playback_muted=False,
            _has_external_audio=True,
            _volume_slider=SimpleNamespace(value=Mock(return_value=70)),
            _audio_output=Mock(),
            _audio_mix_output=Mock(),
            _volume_button=Mock(),
            style=Mock(return_value=SimpleNamespace(standardIcon=Mock())),
            _set_playback_volume=Mock(),
        )

        MainWindow._toggle_playback_mute(fake_window)

        self.assertTrue(fake_window._playback_muted)
        fake_window._set_playback_volume.assert_called_once_with(70)


class ClipCsvExportTests(unittest.TestCase):
    def test_export_writes_all_clips_and_uses_folder_prefixed_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video_path = root / "clip01.mxf"
            metadata = P2ClipMetadata(
                clip_xml=root / "clip01.xml",
                clip_id="internal-id",
                global_clip_id=None,
                clip_name="Lighthouse, aerial",
                duration_frames=250,
                duration_seconds=Fraction(10),
                edit_unit="1/25",
                frame_rate="25",
                start_timecode="01:02:03:12",
                audio_track_count=0,
                video_file=video_path,
                audio_files=(),
            )
            assembly = ClipAssembly(
                MediaFile(video_path, 100, datetime.now(), "P2 / CLIP"),
                (),
                metadata,
            )
            metadata_less_assembly = ClipAssembly(
                MediaFile(
                    root / "clip02.mov",
                    200,
                    datetime.now(),
                    "Video",
                ),
                (),
                None,
            )
            output = root / "clips.csv"
            fake_window = SimpleNamespace(
                _clip_assemblies=[assembly, metadata_less_assembly],
                _selected_folder=root / "Card A",
                _sync_by_video={},
                _clip_export_rows=MainWindow._clip_export_rows,
                statusBar=Mock(return_value=Mock()),
            )

            with patch(
                "p2viewer.app.QFileDialog.getSaveFileName",
                return_value=(str(output), "CSV (*.csv)"),
            ):
                MainWindow._export_clips_csv(fake_window)

            with output.open(encoding="utf-8-sig", newline="") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(
            rows,
            [
                ["Clip", "Timecode", "Duración"],
                ["Card A_Lighthouse, aerial", "01:02:03:12", "00:00:10"],
                ["Card A_clip02", "—", "—"],
            ],
        )


if __name__ == "__main__":
    unittest.main()

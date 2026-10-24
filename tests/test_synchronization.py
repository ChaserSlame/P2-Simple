from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from p2viewer.p2_metadata import P2ClipMetadata
from p2viewer.synchronization import (
    AudioSync,
    SyncAnalysis,
    _timecode_seconds,
    _wrapped_delta,
    analyze_clip_sync,
    build_synced_mxf,
)


class SynchronizationTests(unittest.TestCase):
    def _clip(self, directory: Path) -> P2ClipMetadata:
        return P2ClipMetadata(
            clip_xml=directory / "clip.XML",
            clip_id="A001",
            global_clip_id="urn:uuid:clip",
            clip_name="Test clip",
            duration_frames=250,
            duration_seconds=Fraction(10),
            edit_unit="1/25",
            frame_rate="25",
            start_timecode="00:00:00:00",
            audio_track_count=1,
            video_file=directory / "video.mxf",
            audio_files=(directory / "audio00.mxf",),
        )

    @staticmethod
    def _probe_result(
        media_kind: str,
        timecode: str,
        *,
        start_time: str = "0",
        duration: str = "10",
    ) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_type": media_kind,
                    "start_time": start_time,
                    "duration": duration,
                }
            ],
            "format": {
                "tags": {
                    "timecode": timecode,
                    "material_package_umid": "urn:smpte:umid:shared",
                }
            },
        }

    def test_timecode_parsing_supports_drop_frame_and_rejects_skipped_frames(self) -> None:
        self.assertEqual(
            _timecode_seconds("00:01:00;02", "1001/30000"),
            Fraction(1800 * 1001, 30000),
        )
        with self.assertRaises(ValueError):
            _timecode_seconds("00:01:00;00", "1001/30000")

    def test_timecode_delta_wraps_across_midnight(self) -> None:
        self.assertEqual(
            _wrapped_delta(
                _timecode_seconds("00:00:00:00", "1/25"),
                _timecode_seconds("23:59:59:24", "1/25"),
            ),
            Fraction(1, 25),
        )

    def test_analysis_measures_nonzero_offset_and_keeps_clip_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clip = self._clip(root)

            def fake_run(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(
                    set(kwargs),
                    {"check", "capture_output", "text", "timeout"},
                )
                path = Path(command[-1])
                if path == clip.video_file:
                    probe = self._probe_result("video", "00:00:00:00")
                else:
                    probe = self._probe_result("audio", "00:00:00:02")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(probe),
                    "",
                )

            with patch("p2viewer.synchronization.subprocess.run", side_effect=fake_run):
                analysis = analyze_clip_sync(clip)

            self.assertTrue(analysis.eligible, analysis.issues)
            self.assertEqual(analysis.audio[0].timecode_offset_seconds, Fraction(2, 25))
            self.assertEqual(analysis.audio[0].offset_seconds, Fraction(2, 25))

    def test_builder_applies_measured_offset_and_verifies_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clip = self._clip(root)
            analysis = SyncAnalysis(
                clip=clip,
                video_timecode="00:00:00:00",
                video_duration_seconds=Fraction(10),
                audio=(
                    AudioSync(
                        path=clip.audio_files[0],
                        offset_seconds=Fraction(2, 25),
                        timecode_offset_seconds=Fraction(2, 25),
                        timecode="00:00:00:02",
                        duration_seconds=Fraction(10),
                    ),
                ),
                issues=(),
            )
            output_directory = root / "output"
            output_directory.mkdir()
            commands: list[list[str]] = []

            def fake_run(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(
                    set(kwargs),
                    {"check", "capture_output", "text", "timeout"},
                )
                commands.append(command)
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"simulated MXF")
                    return subprocess.CompletedProcess(command, 0, "", "")
                output_probe = {
                    "streams": [
                        {"codec_type": "video", "start_time": "0", "duration": "10"},
                        {"codec_type": "audio", "start_time": "0.08", "duration": "10"},
                    ],
                    "format": {},
                }
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(output_probe),
                    "",
                )

            with patch("p2viewer.synchronization.subprocess.run", side_effect=fake_run):
                result = build_synced_mxf(
                    analysis,
                    output_directory,
                    ffmpeg="ffmpeg",
                    ffprobe="ffprobe",
                )

            self.assertTrue(result.output_path.is_file())
            self.assertEqual(result.output_path.read_bytes(), b"simulated MXF")
            self.assertEqual(len(commands), 2)
            ffmpeg_command = commands[0]
            self.assertIn("-c", ffmpeg_command)
            self.assertEqual(ffmpeg_command[ffmpeg_command.index("-c") + 1], "copy")
            audio_input_index = ffmpeg_command.index(str(clip.audio_files[0]))
            self.assertEqual(ffmpeg_command[audio_input_index - 3], "-itsoffset")
            self.assertEqual(ffmpeg_command[audio_input_index - 2], "0.080000000")
            self.assertFalse(list(output_directory.glob("*.building.mxf")))

    def test_builder_never_overwrites_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clip = self._clip(root)
            analysis = SyncAnalysis(
                clip=clip,
                video_timecode="00:00:00:00",
                video_duration_seconds=Fraction(10),
                audio=(
                    AudioSync(
                        path=clip.audio_files[0],
                        offset_seconds=Fraction(0),
                        timecode_offset_seconds=Fraction(0),
                        timecode="00:00:00:00",
                        duration_seconds=Fraction(10),
                    ),
                ),
                issues=(),
            )
            output_directory = root / "output"
            output_directory.mkdir()
            existing_output = output_directory / "A001.mxf"
            existing_output.write_bytes(b"keep")

            with patch("p2viewer.synchronization.subprocess.run") as run:
                with self.assertRaises(FileExistsError):
                    build_synced_mxf(analysis, output_directory)

            run.assert_not_called()
            self.assertEqual(existing_output.read_bytes(), b"keep")

    def test_builder_normalizes_negative_offset_without_losing_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            clip = self._clip(root)
            analysis = SyncAnalysis(
                clip=clip,
                video_timecode="00:00:00:00",
                video_duration_seconds=Fraction(10),
                audio=(
                    AudioSync(
                        path=clip.audio_files[0],
                        offset_seconds=Fraction(-2, 25),
                        timecode_offset_seconds=Fraction(-2, 25),
                        timecode="23:59:59:23",
                        duration_seconds=Fraction(10),
                    ),
                ),
                issues=(),
            )
            output_directory = root / "output"
            output_directory.mkdir()
            commands: list[list[str]] = []

            def fake_run(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(
                    set(kwargs),
                    {"check", "capture_output", "text", "timeout"},
                )
                commands.append(command)
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"simulated MXF")
                    return subprocess.CompletedProcess(command, 0, "", "")
                output_probe = {
                    "streams": [
                        {"codec_type": "video", "start_time": "0.08", "duration": "10"},
                        {"codec_type": "audio", "start_time": "0", "duration": "10"},
                    ],
                    "format": {},
                }
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(output_probe),
                    "",
                )

            with patch("p2viewer.synchronization.subprocess.run", side_effect=fake_run):
                build_synced_mxf(analysis, output_directory)

            command = commands[0]
            video_input_index = command.index(str(clip.video_file))
            self.assertEqual(
                command[video_input_index - 3 : video_input_index],
                ["-itsoffset", "0.080000000", "-i"],
            )
            self.assertNotIn("-itsoffset", command[video_input_index + 1 :])


if __name__ == "__main__":
    unittest.main()

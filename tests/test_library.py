from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from p2viewer.clips import build_clip_assemblies
from p2viewer.library import iter_media, scan_media
from p2viewer.p2_metadata import scan_p2_metadata


class ScanMediaTests(unittest.TestCase):
    def test_finds_supported_files_and_classifies_p2_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            p2_clip = root / "CONTENTS" / "CLIP" / "A001" / "clip.MXF"
            av_clip = root / "CONTENTS" / "AVCLIP" / "B001" / "video.MTS"
            ignored = root / "CONTENTS" / "CLIP" / "metadata.XML"
            for path in (p2_clip, av_clip, ignored):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"media")

            results = scan_media(root)

            self.assertEqual(
                [item.path for item in results],
                sorted((p2_clip, av_clip), key=lambda path: str(path).casefold()),
            )
            categories = {item.path: item.category for item in results}
            self.assertEqual(categories[p2_clip], "P2 / CLIP")
            self.assertEqual(categories[av_clip], "P2 / AVCLIP")
            self.assertTrue(all(item.size_bytes == 5 for item in results))

    def test_rejects_a_file_as_the_scan_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            file_path = Path(temporary_directory).resolve() / "not-a-folder"
            file_path.write_text("x", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                scan_media(file_path)

    def test_iterator_reports_progress_and_yields_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            media_path = root / "CONTENTS" / "CLIP" / "clip.mxf"
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(b"media")
            progress: list[tuple[int, int, Path]] = []

            results = list(
                iter_media(
                    root,
                    lambda directories, found, current: progress.append(
                        (directories, found, current)
                    ),
                )
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].path, media_path)
            self.assertEqual(progress[-1], (3, 1, media_path.parent))

    def test_p2_xml_metadata_links_video_and_audio_essences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            contents = root / "CONTENTS"
            clip_id = "A001AA"
            clip_xml = contents / "CLIP" / f"{clip_id}.XML"
            video_path = contents / "VIDEO" / f"{clip_id}.MXF"
            audio_paths = [
                contents / "AUDIO" / f"{clip_id}{track:02d}.MXF"
                for track in range(2)
            ]
            for path in (video_path, *audio_paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mxf")
            clip_xml.parent.mkdir(parents=True, exist_ok=True)
            clip_xml.write_text(
                """<?xml version="1.0"?>
                <P2Main xmlns="urn:panasonic:p2">
                  <ClipContent>
                    <GlobalClipID>urn:uuid:example-clip</GlobalClipID>
                    <ClipName>Take 12</ClipName>
                    <Duration>250</Duration>
                    <EditUnit>1/25</EditUnit>
                    <EssenceList>
                      <Video>
                        <FrameRate>25</FrameRate>
                        <StartTimecode>01:02:03:04</StartTimecode>
                      </Video>
                      <Audio><ChannelCount>2</ChannelCount></Audio>
                      <Audio><ChannelCount>2</ChannelCount></Audio>
                    </EssenceList>
                  </ClipContent>
                </P2Main>""",
                encoding="utf-8",
            )

            result = scan_p2_metadata(root, (video_path, *audio_paths))

            self.assertEqual(len(result.clips), 1)
            clip = result.clips[0]
            self.assertEqual(clip.global_clip_id, "urn:uuid:example-clip")
            self.assertEqual(clip.duration_frames, 250)
            self.assertEqual(float(clip.duration_seconds), 10.0)
            self.assertEqual(clip.start_timecode, "01:02:03:04")
            self.assertEqual(clip.video_file, video_path)
            self.assertEqual(clip.audio_files, tuple(audio_paths))
            self.assertEqual(result.by_media_path[video_path], clip)
            self.assertEqual(result.by_media_path[audio_paths[1]], clip)
            self.assertEqual(result.warnings, ())

            media_files = scan_media(root)
            assemblies = build_clip_assemblies(media_files, result)
            self.assertEqual(len(assemblies), 1)
            self.assertEqual(assemblies[0].video.path, video_path)
            self.assertEqual(assemblies[0].audio_files, tuple(audio_paths))

    def test_clip_assembly_includes_unmatched_video_but_not_audio_only_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            video_path = root / "orphan.mov"
            audio_path = root / "orphan.wav"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")

            assemblies = build_clip_assemblies(
                scan_media(root),
                scan_p2_metadata(root, ()),
            )

            self.assertEqual(len(assemblies), 1)
            self.assertEqual(assemblies[0].video.path, video_path)
            self.assertEqual(assemblies[0].audio_files, ())
            self.assertIsNone(assemblies[0].metadata)

    def test_p2_xml_with_missing_essences_does_not_invent_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            clip_xml = root / "CONTENTS" / "CLIP" / "B001AA.XML"
            clip_xml.parent.mkdir(parents=True)
            clip_xml.write_text(
                """<ClipContent>
                  <GlobalClipID>urn:uuid:missing-essences</GlobalClipID>
                  <EssenceList><Video/><Audio/></EssenceList>
                </ClipContent>""",
                encoding="utf-8",
            )

            result = scan_p2_metadata(root, ())

            self.assertEqual(len(result.clips), 1)
            self.assertIsNone(result.clips[0].video_file)
            self.assertEqual(result.clips[0].audio_files, ())
            self.assertEqual(result.by_media_path, {})

    def test_p2_xml_dtd_is_rejected_with_a_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            clip_xml = root / "CONTENTS" / "CLIP" / "C001AA.XML"
            clip_xml.parent.mkdir(parents=True)
            clip_xml.write_text(
                """<!DOCTYPE ClipContent [<!ENTITY name "expanded">]>
                <ClipContent><ClipName>&name;</ClipName></ClipContent>""",
                encoding="utf-8",
            )

            result = scan_p2_metadata(root, ())

            self.assertEqual(result.clips, ())
            self.assertEqual(len(result.warnings), 1)
            self.assertIn("DTD o ENTITY", result.warnings[0])


if __name__ == "__main__":
    unittest.main()

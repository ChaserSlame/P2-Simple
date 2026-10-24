# P2ViewerPlus Python

A small, source-based reconstruction starter for the statically observed
P2Viewer Plus application. It provides a Qt interface for scanning folders
and browsing supported media files.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m p2viewer
```

On Windows, activate the environment with `.venv\Scripts\activate`.

## Current behavior

- Choose a folder and scan it recursively without following directory symlinks.
- Scan supported Panasonic P2, AVCHD, and video files to find clip compositions.
- Read P2 Clip XML identifiers, timecode, frame rate, and duration; associate
  video and audio MXF essence files using the clip XML and P2 directory layout.
- Show a logical composition row for each video with its P2 audio tracks. This
  measures their timecode/PTS offsets before rendering output.
- Play an in-app audio/video preview for the selected clip, including seeking,
  volume, and pause/stop controls. A temporary low-resolution MPEG-4/AAC proxy is
  rendered asynchronously; verified P2 audio tracks are mixed for playback
  using their measured offsets. This proxy is separate from the original media
  and from the no-re-encode MXF output.
- Keep scanning off the UI thread, display progress and partial results, and
  report filesystem errors or an empty scan in the interface.
- Build standalone MXF files by copying the video and declared audio streams
  without re-encoding, after showing the measured offsets and asking for
  confirmation.

P2 metadata is read from `CONTENTS/CLIP/<clip-id>.XML`; the corresponding video
and audio essence files are resolved from `CONTENTS/VIDEO` and `CONTENTS/AUDIO`
using the P2 clip identifier and audio-track order. Only files present in the
selected folder are associated. Ambiguous or absent essence files are not guessed.
The builder requires `ffmpeg` and `ffprobe` on `PATH`. It validates each source's
P2 timecode, media-package UMID, stream start and duration. Clips with missing or
inconsistent evidence are blocked rather than guessed. Output is a standalone
MXF, not a newly authored Panasonic P2 card structure. It is written to a
user-selected folder outside the scanned card; existing output names are never
overwritten. Clips whose audio synchronization cannot be verified can still be
previewed as video, but their audio is deliberately omitted. AVCHD playlists,
licenses, and the original application's complete behavior remain outside this
reconstruction.

## Tests

```bash
python -m unittest discover -s tests
```

The media scanner uses only the Python standard library, so its tests do not
require Qt.

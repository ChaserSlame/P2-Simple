# P2ViewerPlus Python

A small, source-based reconstruction starter for the statically observed
P2Viewer Plus application. It provides a Qt interface for scanning folders
and browsing supported media files.

## Installation

### Dependencies

- Python 3.10 or newer.
- FFmpeg, with both `ffmpeg` and `ffprobe` available on `PATH`. These tools
  are required for audio/video previews and synchronization checks.
- PySide6 6.8 or newer. It is installed automatically with the project below.

Check that Python and FFmpeg are available before installing:

```text
python --version
ffmpeg -version
ffprobe -version
```

### Windows

Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/)
and enable the option to add Python to `PATH`. Install FFmpeg and make sure its
`bin` directory (containing `ffmpeg.exe` and `ffprobe.exe`) is on `PATH`. For
example, with [winget](https://learn.microsoft.com/windows/package-manager/winget/):

```powershell
winget install --id Gyan.FFmpeg.Shared --exact
```

Open a new PowerShell window so it picks up any `PATH` changes, then run these
commands from the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m p2viewer
```

If PowerShell does not allow activating the virtual environment, run the
environment's Python directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m p2viewer
```

### Linux (Ubuntu/Debian)

Install Python, virtual-environment support, pip, and FFmpeg:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip ffmpeg
```

From the project directory, create and activate a virtual environment, install
the application and its Python dependencies, and start it:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m p2viewer
```

On other Linux distributions, install Python 3.10 or newer and FFmpeg using the
distribution's package manager. Ensure both `ffmpeg` and `ffprobe` are on `PATH`.

To launch the app again later, activate the virtual environment and run
`python -m p2viewer` (on Windows, use `.\.venv\Scripts\Activate.ps1`).

## Current behavior

- Choose a folder and scan it recursively without following directory symlinks.
- Scan supported Panasonic P2, AVCHD, and video files to find clip compositions.
- Read P2 Clip XML identifiers, timecode, frame rate, and duration; associate
  video and audio MXF essence files using the clip XML and P2 directory layout.
- Show a logical composition row for each video with its P2 audio tracks. This
  measures their timecode/PTS offsets before rendering output.
- Play an in-app audio/video preview for the selected clip, including seeking,
  volume, and pause/stop controls. The app asynchronously creates a temporary
  MOV proxy by copying the source video stream and mixing verified P2 audio
  tracks to AAC using their measured offsets. If FFmpeg cannot mux the copied
  video into MOV, it falls back to a scaled MPEG-4/AAC MP4 proxy. The original
  media is not modified.
- Keep scanning off the UI thread, display progress and partial results, and
  report filesystem errors or an empty scan in the interface.

P2 metadata is read from `CONTENTS/CLIP/<clip-id>.XML`; the corresponding video
and audio essence files are resolved from `CONTENTS/VIDEO` and `CONTENTS/AUDIO`
using the P2 clip identifier and audio-track order. Only files present in the
selected folder are associated. Ambiguous or absent essence files are not guessed.
The application validates P2 timecode, media-package UMID, stream start and
duration before including audio in the preview. Clips with missing or
inconsistent synchronization evidence can still be previewed as video, but
their audio is deliberately omitted. AVCHD playlists, licenses, and the
original application's complete behavior remain outside this reconstruction.

## Tests

```bash
python -m unittest discover -s tests
```

The media scanner uses only the Python standard library, so its tests do not
require Qt.

## Performance diagnostics

Run the application from a terminal to see timing logs:

```bash
python -m p2viewer
```

The log reports directory scanning, P2 metadata scanning, each clip's
synchronization analysis and individual `ffprobe` calls. Preview timings
separate proxy generation, Qt player loading, and total time from selecting a
clip until it becomes playable. Compare the same clips on the same storage
before and after an optimization; preview generation copies the source video
stream when MOV supports it and mixes audio into a temporary proxy. Unsupported
combinations fall back to the scaled MPEG-4/AAC transcode.

P2 synchronization checks run concurrently for up to four clips at a time;
the worker limit is bounded to avoid creating an unrestrained number of
`ffprobe` processes.
Generated preview proxies are cached for the current app session (up to four
clips and 512 MiB total) and reused when selected again. The cache key includes
source file size, modification time, and audio offsets, so changed media
produces a fresh proxy.

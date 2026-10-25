from __future__ import annotations

import sys
from pathlib import Path


def resolve_media_tool(name: str) -> str:
    if name not in {"ffmpeg", "ffprobe"}:
        return name
    if not getattr(sys, "frozen", False):
        return name

    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(sys.executable).parent)
    )
    suffix = ".exe" if sys.platform == "win32" else ""
    executable = bundle_root / "ffmpeg" / f"{name}{suffix}"
    if not executable.is_file():
        raise FileNotFoundError(
            f"No se encontró {executable.name} dentro del paquete portable."
        )
    return str(executable)

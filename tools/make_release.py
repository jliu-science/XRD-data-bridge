"""Build the Windows release zip: launcher + app + bundled portable Python.

    python tools/make_release.py 1.0.0      ->  dist/XRD-data-bridge-1.0.0-win64.zip

Local settings/state and generated files are never included.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_NAMES = {"config.json", ".bridge_state.json", "__pycache__", "converted", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc"}


def _wanted(p: Path) -> bool:
    return not any(part in EXCLUDE_NAMES for part in p.parts) and p.suffix not in EXCLUDE_SUFFIXES


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
    if not (ROOT / "python" / "python.exe").is_file():
        sys.exit("python/ (portable Python with numpy + h5py) is missing next to the launcher")
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    name = f"XRD-data-bridge-{version}-win64"
    target = out_dir / f"{name}.zip"
    files = [ROOT / "start_xrd_bridge.bat", ROOT / "LICENSE", ROOT / "README.md"]
    for folder in ("xrd_sdl_bridge", "python"):
        files += [p for p in sorted((ROOT / folder).rglob("*")) if p.is_file() and _wanted(p.relative_to(ROOT))]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            zf.write(f, f"{name}/{f.relative_to(ROOT).as_posix()}")
    print(f"{target}  ({target.stat().st_size / 1e6:.1f} MB, {len(files)} files)")


if __name__ == "__main__":
    main()

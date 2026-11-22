"""Browser glue for the XRD Data Bridge online converter (runs inside Pyodide).

It uses exactly the same converter.py as the desktop bridge, so every conversion is identical and
lossless. Files are only ever in the browser's in-memory file system; nothing is uploaded.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

from converter import (
    FORMAT_EXTENSION,
    OUTPUT_FORMATS,
    convert_wavelength,
    normalize_anode,
    read_scan_file,
    write_asc_file,
    write_xrdml_file,
    write_xy_file,
)

IN_DIR = Path("/work/in")
OUT_DIR = Path("/work/out")


def reset_dirs() -> None:
    for d in (IN_DIR, OUT_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def convert(names_json: str, formats_json: str, source_anode: str, target_anode: str, asc_flavor: str) -> str:
    """Convert every uploaded file; return a JSON report."""
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    names = json.loads(names_json)
    wanted = set(json.loads(formats_json))
    formats = [f for f in OUTPUT_FORMATS if f in wanted]
    report = []

    for name in names:
        src = IN_DIR / name
        entry = {"file": name, "outputs": [], "skipped": [], "error": None, "scans": []}
        try:
            fixed = normalize_anode(source_anode) if source_anode not in ("", "auto") else None
            scans = read_scan_file(src, default_anode=fixed or "Cu")
            for scan in scans:
                src_key = fixed or normalize_anode(scan.anode)
                tgt = normalize_anode(target_anode) if target_anode else ""
                if tgt == src_key:
                    tgt = ""
                out_scan = convert_wavelength(scan, src_key, tgt) if tgt else scan
                override = tgt or fixed  # None = keep the wavelength stated in the file
                stem = f"{scan.name}_{tgt}" if tgt else scan.name
                entry["scans"].append(
                    {
                        "name": scan.name,
                        "points_in": int(scan.two_theta.size),
                        "points_out": int(out_scan.two_theta.size),
                        "anode_in": src_key,
                        "anode_out": tgt or src_key,
                        "dropped": int(out_scan.metadata.get("WavelengthDroppedPoints", 0)),
                    }
                )
                for fmt in formats:
                    ext = FORMAT_EXTENSION[fmt]
                    if ext == src.suffix.lower() and not tgt:
                        entry["skipped"].append({"format": fmt, "reason": "same format as the input file"})
                        continue
                    target = _unique(OUT_DIR / f"{stem}{ext}")
                    try:
                        if fmt == "xrdml":
                            write_xrdml_file(out_scan, target, anode_override=override)
                        elif fmt == "asc":
                            write_asc_file(out_scan, target, asc_flavor=asc_flavor, anode_override=override)
                        else:
                            write_xy_file(out_scan, target, anode_override=override)
                    except ValueError as exc:
                        entry["skipped"].append({"format": fmt, "reason": str(exc)})
                        continue
                    entry["outputs"].append(target.name)
        except Exception as exc:  # noqa: BLE001 - report any unreadable file to the page
            entry["error"] = str(exc)
        report.append(entry)
    return json.dumps(report)


def preview(name: str) -> str:
    """Return the first scan of an uploaded file for plotting (display only)."""
    scan = read_scan_file(IN_DIR / name)[0]
    x, y = scan.two_theta, scan.intensity
    step = max(1, x.size // 3000)
    return json.dumps({"name": scan.name, "x": x[::step].tolist(), "y": y[::step].tolist(), "anode": scan.anode,
                       "points": int(x.size)})


def zip_outputs() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(OUT_DIR.iterdir()):
            zf.write(p, p.name)
    return buf.getvalue()

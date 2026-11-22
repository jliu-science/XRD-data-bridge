"""Malvern Panalytical HighScore Plus integration, QC checks, and ICDD phase report parsing."""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from converter import ScanData

_COMMON_EXE_CANDIDATES = [
    r"C:\Program Files\PANalytical\HighScore Plus\HighScorePlus.exe",
    r"C:\Program Files (x86)\PANalytical\HighScore Plus\HighScorePlus.exe",
    r"C:\Program Files\Malvern Panalytical\HighScore Plus\HighScorePlus.exe",
    r"C:\Program Files (x86)\Malvern Panalytical\HighScore Plus\HighScorePlus.exe",
    r"C:\Program Files\PANalytical\HighScore\HighScore.exe",
    r"C:\Program Files (x86)\PANalytical\HighScore\HighScore.exe",
    r"C:\Program Files\PANalytical\Data Viewer\DataViewer.exe",
    r"C:\Program Files (x86)\PANalytical\Data Viewer\DataViewer.exe",
    r"C:\Program Files\Malvern Panalytical\Data Viewer\DataViewer.exe",
]


ALLOWED_VIEWER_EXE_NAMES = {"highscoreplus.exe", "highscore.exe", "dataviewer.exe"}


def is_allowed_viewer_exe(path: str | Path) -> bool:
    """Only HighScore / Data Viewer may ever be launched by the bridge."""
    return Path(str(path)).name.lower() in ALLOWED_VIEWER_EXE_NAMES


def discover_highscore_executables() -> list[str]:
    """Scan standard Windows install directories for HighScore Plus / Data Viewer."""
    found: list[str] = []
    for cand in _COMMON_EXE_CANDIDATES:
        if Path(cand).is_file():
            found.append(cand)
    return found


def launch_in_highscore(xrdml_path: str | Path, exe_path: str | None = None) -> dict[str, Any]:
    """Open a converted .xrdml scan directly in HighScore Plus (or configured viewer)."""
    target = Path(xrdml_path)
    if target.suffix.lower() != ".xrdml":
        return {"ok": False, "message": f"Only .xrdml files can be opened: {target.name}"}
    if not target.is_file():
        return {"ok": False, "message": f"Target XRDML file does not exist: {target}"}

    chosen_exe = (exe_path or "").strip()
    if not chosen_exe:
        discovered = discover_highscore_executables()
        chosen_exe = discovered[0] if discovered else ""

    if not chosen_exe:
        return {
            "ok": False,
            "message": (
                "HighScorePlus.exe path is not configured (and was not found in standard Program Files directories). "
                "Please browse to HighScorePlus.exe in the UI, or monitor the output .xrdml folder from HighScore."
            ),
            "xrdml_path": str(target),
        }

    exe = Path(chosen_exe)
    if not is_allowed_viewer_exe(exe):
        return {
            "ok": False,
            "message": f"Refusing to launch {exe.name}: only HighScorePlus.exe, HighScore.exe or DataViewer.exe are allowed.",
            "xrdml_path": str(target),
        }
    if not exe.is_file():
        return {
            "ok": False,
            "message": f"Configured HighScore executable not found: {exe}",
            "xrdml_path": str(target),
        }

    try:
        proc = subprocess.Popen([str(exe), str(target)])
        return {
            "ok": True,
            "pid": proc.pid,
            "exe": str(exe),
            "xrdml_path": str(target),
            "message": f"Launched {exe.name} with {target.name} (if Automatic Processing is enabled, ICDD Search & Match will run automatically).",
        }
    except OSError as exc:
        return {"ok": False, "message": f"Failed to launch {exe.name}: {exc}", "xrdml_path": str(target)}


def _find_peaks_numpy(y: np.ndarray, prominence: float, distance: int) -> tuple[np.ndarray, np.ndarray]:
    """Lightweight pure-NumPy 1D local-maxima and prominence peak detector."""
    if y.size < 3:
        return np.array([], dtype=int), np.array([], dtype=np.float64)

    cand = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]))[0] + 1
    if cand.size == 0:
        return np.array([], dtype=int), np.array([], dtype=np.float64)

    half_win = max(12, distance * 2)
    kept_idx: list[int] = []
    kept_prom: list[float] = []
    for idx in cand:
        left_min = float(np.min(y[max(0, idx - half_win) : idx]))
        right_min = float(np.min(y[idx + 1 : min(y.size, idx + half_win + 1)]))
        prom = float(y[idx]) - max(left_min, right_min)
        if prom >= prominence:
            kept_idx.append(int(idx))
            kept_prom.append(prom)

    if not kept_idx:
        return np.array([], dtype=int), np.array([], dtype=np.float64)

    # Greedy distance suppression ordered by descending peak height.
    order = sorted(range(len(kept_idx)), key=lambda i: y[kept_idx[i]], reverse=True)
    selected: list[int] = []
    selected_prom: list[float] = []
    for pos in order:
        p = kept_idx[pos]
        if all(abs(p - s) >= distance for s in selected):
            selected.append(p)
            selected_prom.append(kept_prom[pos])

    sort_order = np.argsort(selected)
    return np.asarray(selected, dtype=int)[sort_order], np.asarray(selected_prom, dtype=np.float64)[sort_order]


def assess_scan_quality(scan: ScanData) -> dict[str, Any]:
    """Evaluate Rigaku Slide-15 style autonomous QC: peaks present & SNR fit for phase ID."""
    y = np.asarray(scan.intensity, dtype=np.float64)
    x = np.asarray(scan.two_theta, dtype=np.float64)
    if y.size < 10:
        return {
            "peaks_present": False,
            "fit_for_analysis": False,
            "peak_count": 0,
            "max_counts": float(np.max(y)) if y.size else 0.0,
            "baseline_est": 0.0,
            "snr_estimate": 0.0,
            "recommendation": "Too few data points -> Recommend re-scan",
        }

    # Robust background & noise estimate using lower quantiles.
    baseline = float(np.percentile(y, 20))
    low_region = y[y <= np.percentile(y, 40)]
    noise_std = float(np.std(low_region)) if low_region.size > 3 else max(1.0, float(np.sqrt(max(baseline, 1.0))))
    noise_std = max(noise_std, 1.0)

    max_counts = float(np.max(y))
    net_max = max(0.0, max_counts - baseline)
    snr = net_max / noise_std

    # Detect diffraction peaks with prominence >= max(5 * noise, 2.5% of dynamic range).
    prominence_thresh = max(5.0 * noise_std, 0.025 * net_max, 25.0)
    dist = max(3, int(0.15 / max(1e-4, float(np.median(np.diff(x))))))
    peaks, proms = _find_peaks_numpy(y, prominence=prominence_thresh, distance=dist)
    peak_count = int(len(peaks))

    peaks_present = peak_count >= 1
    fit_for_analysis = peaks_present and snr >= 8.0 and net_max >= 100.0

    if not peaks_present:
        recommendation = "No distinct diffraction peaks detected (amorphous or empty holder) -> Skip refinement, advance to next sample"
    elif not fit_for_analysis:
        recommendation = f"Low signal-to-noise (SNR≈{snr:.1f}) -> Recommend longer counting time re-scan (Slide 15 adaptive loop)"
    else:
        recommendation = f"Good scan quality (detected {peak_count} peaks, SNR≈{snr:.1f}) -> Ready for HighScore ICDD Search & Match"

    top_peaks = []
    if peak_count > 0:
        order = np.argsort(proms)[::-1][:8]
        _, wl = scan.resolved_wavelengths()
        ka1 = wl["kAlpha1"]
        for idx in order:
            p_idx = int(peaks[idx])
            tth = float(x[p_idx])
            sin_t = np.sin(np.deg2rad(tth / 2.0))
            d_val = ka1 / (2.0 * sin_t) if sin_t > 1e-6 else 0.0
            top_peaks.append({"two_theta": round(tth, 3), "counts": round(float(y[p_idx]), 1), "d_angstrom": round(float(d_val), 4)})

    return {
        "peaks_present": peaks_present,
        "fit_for_analysis": fit_for_analysis,
        "peak_count": peak_count,
        "max_counts": round(max_counts, 1),
        "baseline_est": round(baseline, 1),
        "snr_estimate": round(snr, 1),
        "recommendation": recommendation,
        "top_peaks": top_peaks,
    }


def parse_highscore_phase_reports(results_dir: str | Path) -> list[dict[str, Any]]:
    """Read CSV/TSV phase identification results exported by HighScore Plus / Pascal script."""
    folder = Path(results_dir)
    if not folder.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for file_path in sorted(folder.glob("*.csv")) + sorted(folder.glob("*.tsv")):
        delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
        try:
            with file_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                for row in reader:
                    clean_row = {str(k).strip(): str(v).strip() for k, v in row.items() if k is not None}
                    clean_row["_source_file"] = file_path.name
                    records.append(clean_row)
        except OSError:
            continue
    return records

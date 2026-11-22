"""Synthetic Anton Paar XRDynamic-style .hdf5 and .xy demo data generator."""

from __future__ import annotations

import csv
from pathlib import Path

import h5py
import numpy as np


def _pseudo_voigt(x: np.ndarray, center: float, height: float, fwhm: float, eta: float = 0.45) -> np.ndarray:
    """Compute a normalized-peak Pseudo-Voigt profile."""
    sigma = fwhm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    gamma = fwhm / 2.0
    gauss = np.exp(-0.5 * ((x - center) / sigma) ** 2)
    lorentz = 1.0 / (1.0 + ((x - center) / gamma) ** 2)
    return height * (eta * lorentz + (1.0 - eta) * gauss)


def _simulate_pattern(
    two_theta: np.ndarray,
    peaks: list[tuple[float, float, float]],
    bg_level: float = 140.0,
    seed: int = 42,
) -> np.ndarray:
    """Simulate powder XRD pattern with Cu Ka1/Ka2 doublet, background, and Poisson noise."""
    rng = np.random.default_rng(seed)
    # Realistic amorphous + air-scatter background decaying slightly with 2Theta.
    y = bg_level + 160.0 * np.exp(-two_theta / 24.0) + 18.0 * np.sin(two_theta * 0.06)

    ka1 = 1.540598
    ka2 = 1.544426
    for pos1, height, fwhm in peaks:
        y += _pseudo_voigt(two_theta, pos1, height, fwhm)
        # Bragg Ka2 splitting: sin(theta2) = (ka2/ka1) * sin(theta1)
        sin_th2 = min(0.9999, (ka2 / ka1) * np.sin(np.deg2rad(pos1 / 2.0)))
        pos2 = 2.0 * np.rad2deg(np.arcsin(sin_th2))
        y += _pseudo_voigt(two_theta, pos2, height * 0.5, fwhm)

    noisy = rng.poisson(np.clip(y, 1.0, None)).astype(np.float64)
    return noisy


def _write_anton_paar_hdf5(
    out_path: Path,
    two_theta: np.ndarray,
    intensity: np.ndarray,
    sample_name: str,
    step_deg: float,
    counting_time: float = 0.5,
) -> None:
    """Write a realistic Anton Paar XRDdrive-style hierarchical HDF5 scan file."""
    with h5py.File(out_path, "w") as h5f:
        h5f.attrs["Instrument"] = "Anton Paar XRDynamic 500"
        h5f.attrs["Software"] = "XRDdrive 1.4 (Simulated)"
        h5f.attrs["Anode"] = "Cu"
        h5f.attrs["Wavelength_Ka1"] = 1.540598
        h5f.attrs["Tension_kV"] = 40
        h5f.attrs["Current_mA"] = 40
        h5f.attrs["SampleName"] = sample_name
        h5f.attrs["StartTimeStamp"] = "2026-11-13T10:15:00"
        h5f.attrs["EndTimeStamp"] = "2026-11-13T10:25:00"

        meas = h5f.create_group("Measurement")
        scan = meas.create_group("Scan_01")
        scan.attrs["ScanAxis"] = "2Theta"
        scan.attrs["Start_2Theta"] = float(two_theta[0])
        scan.attrs["Step_Size"] = float(step_deg)
        scan.attrs["Counting_Time_s"] = float(counting_time)

        ds_ang = scan.create_dataset("Position_2Theta", data=two_theta)
        ds_ang.attrs["Unit"] = "deg"
        ds_cnt = scan.create_dataset("Intensity_Counts", data=intensity)
        ds_cnt.attrs["Unit"] = "counts"


def _write_xy(out_path: Path, two_theta: np.ndarray, intensity: np.ndarray, header: str) -> None:
    lines = [f"# {header}", "# Wavelength: 1.540598 A (Cu Ka1)", "# 2Theta(deg)\tIntensity(counts)"]
    for x_val, y_val in zip(two_theta, intensity):
        lines.append(f"{x_val:.4f}\t{y_val:.1f}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_demo_dataset(target_dir: str | Path, include_sample_report: bool = True) -> list[Path]:
    """Generate synthetic Anton Paar .hdf5 and .xy files plus an optional sample ICDD CSV report."""
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    two_theta = np.arange(10.0, 90.0001, 0.02, dtype=np.float64)

    # 1. Silicon NIST 640f standard (.hdf5 + .xy pair)
    si_peaks = [
        (28.443, 18500.0, 0.11),
        (47.304, 10200.0, 0.12),
        (56.122, 5600.0, 0.13),
        (69.131, 1600.0, 0.14),
        (76.377, 2200.0, 0.15),
        (88.032, 2600.0, 0.16),
    ]
    si_y = _simulate_pattern(two_theta, si_peaks, bg_level=120.0, seed=101)
    si_h5 = out_dir / "AP_XRDynamic_Si_NIST640f.hdf5"
    si_xy = out_dir / "AP_XRDynamic_Si_NIST640f.xy"
    # Never overwrite an existing file (the demo may be generated inside a real data folder).
    if not si_h5.exists():
        _write_anton_paar_hdf5(si_h5, two_theta, si_y, "Si_NIST_640f", step_deg=0.02, counting_time=0.5)
        created.append(si_h5)
    if not si_xy.exists():
        _write_xy(si_xy, two_theta, si_y, "Anton Paar XRDynamic 500 - Si NIST 640f")
        created.append(si_xy)

    # 2. TiO2 Anatase + Rutile mixture (.hdf5)
    tio2_peaks = [
        (25.28, 14000.0, 0.22),  # Anatase (101)
        (27.45, 7200.0, 0.18),   # Rutile (110)
        (36.09, 3400.0, 0.19),   # Rutile (101)
        (37.80, 3100.0, 0.24),   # Anatase (004)
        (41.23, 1900.0, 0.20),   # Rutile (111)
        (48.05, 4800.0, 0.25),   # Anatase (200)
        (53.89, 2800.0, 0.26),   # Anatase (105)
        (54.32, 4200.0, 0.21),   # Rutile (211)
        (55.06, 2600.0, 0.26),   # Anatase (211)
        (62.69, 2100.0, 0.27),   # Anatase (204)
    ]
    tio2_y = _simulate_pattern(two_theta, tio2_peaks, bg_level=160.0, seed=202)
    tio2_h5 = out_dir / "AP_XRDynamic_TiO2_Anatase_Rutile.hdf5"
    if not tio2_h5.exists():
        _write_anton_paar_hdf5(tio2_h5, two_theta, tio2_y, "TiO2_Anatase_Rutile_Mix", step_deg=0.02, counting_time=0.8)
        created.append(tio2_h5)

    # 3. Calcite CaCO3 (.xy)
    calcite_peaks = [
        (23.02, 1800.0, 0.16),
        (29.41, 16500.0, 0.15),
        (35.97, 2600.0, 0.17),
        (39.40, 3200.0, 0.17),
        (43.15, 2900.0, 0.18),
        (47.49, 3100.0, 0.19),
        (48.51, 3300.0, 0.19),
        (57.40, 1700.0, 0.20),
    ]
    calcite_y = _simulate_pattern(two_theta, calcite_peaks, bg_level=135.0, seed=303)
    calcite_xy = out_dir / "AP_Scan_CaCO3_Calcite.xy"
    if not calcite_xy.exists():
        _write_xy(calcite_xy, two_theta, calcite_y, "Anton Paar XRDynamic 500 - CaCO3 Calcite")
        created.append(calcite_xy)

    if include_sample_report:
        rep_dir = out_dir / "highscore_results"
        rep_dir.mkdir(parents=True, exist_ok=True)
        rep_csv = rep_dir / "AP_XRDynamic_Si_NIST640f_phases.csv"
        if not rep_csv.exists():
            with rep_csv.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Sample", "Rank", "RefCode", "CompoundName", "Formula", "Score", "ScaleFactor", "Timestamp"])
                writer.writerow(["AP_XRDynamic_Si_NIST640f", "1", "00-027-1402", "Silicon (syn)", "Si", "94", "0.982", "2026-11-13T10:26:10"])
            created.append(rep_csv)

    return created

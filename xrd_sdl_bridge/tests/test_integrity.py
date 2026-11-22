"""Data-integrity audit: raw files are never modified and every data point survives conversion.

Covers awkward real-world data (decimal intensities, negatives, high-precision / non-uniform 2Theta,
large counts) and runs the full manager pipeline in every output mode, including the live watcher.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from converter import ScanData, read_scan_file, write_xrdml_file  # noqa: E402
from sample_data_gen import generate_demo_dataset  # noqa: E402
from watcher import FolderBridgeManager  # noqa: E402

FORMATS = ["xrdml", "asc", "xy", "dat"]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _snapshot(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): _sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def _tricky_patterns() -> dict[str, tuple[np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(7)
    n = 3001
    # XRDynamic-style step 0.0131 deg with float noise from accumulation
    tt_fine = 5.0 + np.cumsum(np.full(n, 0.0131)) - 0.0131
    # deliberately non-uniform grid (variable counting strategy)
    tt_nonuni = np.sort(10 + 70 * rng.random(n))
    y_decimal = rng.random(n) * 12345.678901 + 0.000123  # non-integer, many digits
    y_neg = rng.normal(0, 50, n)  # background-subtracted data with negatives
    y_big = rng.integers(0, 50_000_000, n).astype(float)  # large integer counts
    return {
        "fine_decimal": (tt_fine, y_decimal),
        "nonuniform_negative": (tt_nonuni, y_neg),
        "fine_bigcounts": (tt_fine, y_big),
    }


class TestIntegrity(unittest.TestCase):
    def _assert_identical(self, ref_x, ref_y, scan, label) -> None:
        self.assertEqual(ref_x.size, scan.two_theta.size, f"{label}: point count")
        self.assertLessEqual(float(np.max(np.abs(ref_x - scan.two_theta))), 1e-9, f"{label}: 2theta")
        self.assertTrue(np.array_equal(ref_y, scan.intensity), f"{label}: intensity values")

    def test_tricky_data_lossless_through_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw, out = tmp / "raw", tmp / "out"
            raw.mkdir()
            refs = {}
            for name, (x, y) in _tricky_patterns().items():
                # store as a foreign .xrdml raw file (exact values, listPositions when non-uniform)
                scan = ScanData(name=name, source_path=str(raw / f"{name}.src"), source_format="xy", two_theta=x, intensity=y)
                p = write_xrdml_file(scan, tmp / f"{name}.xrdml")
                (raw / f"{name}.xrdml").write_text(p.read_text(encoding="utf-8").replace("SDL Bridge", "Vendor"), encoding="utf-8")
                refs[name] = (x, y)
                self._assert_identical(x, y, read_scan_file(raw / f"{name}.xrdml")[0], f"{name} raw")

            before = _snapshot(raw)
            mgr = FolderBridgeManager(config_path=tmp / "c.json", state_path=tmp / "s.json")
            mgr.update_config(
                {"input_dir": str(raw), "output_mode": "custom_dir", "custom_output_dir": str(out), "output_formats": FORMATS}
            )
            res = mgr.convert_all()
            self.assertEqual(res["errors"], [])
            for name, (x, y) in refs.items():
                for fmt in ("asc", "xy", "dat"):  # xrdml is the source format here, so it is skipped
                    self._assert_identical(x, y, read_scan_file(out / f"{name}.{fmt}")[0], f"{name}.xrdml -> .{fmt}")
                # second hop: .xy output -> every other format again
                hop = FolderBridgeManager(config_path=tmp / "c2.json", state_path=tmp / "s2.json")
                hop_out = tmp / "hop" / name
                hop.update_config({"input_dir": str(out), "output_mode": "custom_dir", "custom_output_dir": str(hop_out),
                                   "output_formats": FORMATS, "include_subfolders": False})
                hop.convert_file(out / f"{name}.xy")
                for fmt in ("xrdml", "asc", "dat"):
                    self._assert_identical(x, y, read_scan_file(hop_out / f"{name}.{fmt}")[0], f"{name} -> .xy -> .{fmt}")
            self.assertEqual(before, _snapshot(raw), "raw folder changed")

    def test_raw_files_never_modified_any_mode(self) -> None:
        for mode in ("same_dir", "custom_dir"):
            for target in ("", "Co", "Mo"):
                with self.subTest(mode=mode, target=target), tempfile.TemporaryDirectory() as tmp:
                    tmp = Path(tmp)
                    raw = tmp / "raw"
                    generate_demo_dataset(raw / "Alice", include_sample_report=False)
                    generate_demo_dataset(raw / "Bob", include_sample_report=False)
                    # a raw .asc and .dat that share a stem with nothing else
                    (raw / "Bob" / "Rigaku_Sample.asc").write_text(
                        "*TYPE_RAW\n*START\t10\n*STOP\t10.08\n*STEP\t0.02\n*COUNT\t5\n1.5\n2.25\n3\n4\n5\n*END\n", encoding="utf-8")
                    (raw / "Alice" / "Lab_Sample.dat").write_text(
                        "\n".join(f"{10 + i * 0.01:.2f} {100 + i}.5" for i in range(50)) + "\n", encoding="utf-8")
                    before = _snapshot(raw)
                    raw_files = set(before)

                    mgr = FolderBridgeManager(config_path=tmp / "c.json", state_path=tmp / "s.json")
                    mgr.update_config({"input_dir": str(raw), "output_mode": mode, "custom_output_dir": str(tmp / "out"),
                                       "output_formats": FORMATS, "target_anode": target, "anode": "Cu"})
                    res = mgr.convert_all()
                    self.assertEqual(res["errors"], [], res["errors"])
                    # run it again plus a few live-watch cycles: still nothing raw may change
                    mgr.convert_all()
                    mgr.update_config({"watch_enabled": True, "poll_interval_sec": 0.5})
                    mgr.start_background_watcher()
                    time.sleep(2.0)
                    mgr.stop_background_watcher()

                    after = _snapshot(raw)
                    written = sum(len(o["files"]) for c in res["converted"] for o in c["outputs"])
                    self.assertGreaterEqual(written, 10, "conversion produced too few files")
                    for rel, digest in before.items():
                        self.assertEqual(after.get(rel), digest, f"raw file changed: {rel}")
                    sources = {Path(s["path"]).relative_to(raw).as_posix() for s in mgr.list_source_files()}
                    self.assertEqual(sources, {Path(r).as_posix() for r in raw_files}, "generated file listed as a source")
                    if mode == "custom_dir":
                        self.assertEqual(set(after), raw_files, "files were added to the raw folder in custom_dir mode")


if __name__ == "__main__":
    unittest.main(verbosity=2)

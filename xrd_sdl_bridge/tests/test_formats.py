"""Tests for format interconversion (.xrdml/.xy/.asc/.dat) and anode wavelength re-mapping."""

from __future__ import annotations

import hashlib
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from converter import (  # noqa: E402
    ANODE_WAVELENGTHS,
    convert_wavelength,
    read_scan_file,
    write_asc_file,
    write_xrdml_file,
    write_xy_file,
)
from sample_data_gen import generate_demo_dataset  # noqa: E402
from watcher import FolderBridgeManager  # noqa: E402


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestFormats(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        generate_demo_dataset(self.tmp / "raw", include_sample_report=False)
        self.xy = self.tmp / "raw" / "AP_Scan_CaCO3_Calcite.xy"
        self.scan = read_scan_file(self.xy)[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_same(self, a, b, tol_y=0.6) -> None:
        self.assertEqual(a.two_theta.size, b.two_theta.size)
        self.assertTrue(np.allclose(a.two_theta, b.two_theta, atol=2e-3))
        self.assertTrue(np.allclose(a.intensity, b.intensity, atol=tol_y))

    def test_round_trip_every_text_format(self) -> None:
        out = self.tmp / "out"
        files = {
            "xrdml": write_xrdml_file(self.scan, out / "s.xrdml"),
            "asc": write_asc_file(self.scan, out / "s.asc"),
            "asc_rigaku": write_asc_file(self.scan, out / "s_rig.asc", asc_flavor="rigaku_asc"),
            "xy": write_xy_file(self.scan, out / "s.xy"),
            "dat": write_xy_file(self.scan, out / "s.dat"),
        }
        for kind, path in files.items():
            back = read_scan_file(path)[0]
            self._assert_same(self.scan, back)
            self.assertEqual(back.anode, "Cu", kind)
        # chain: xrdml -> xy -> asc -> xrdml keeps the data
        s1 = read_scan_file(files["xrdml"])[0]
        p2 = write_xy_file(s1, out / "chain.xy")
        p3 = write_asc_file(read_scan_file(p2)[0], out / "chain.asc")
        p4 = write_xrdml_file(read_scan_file(p3)[0], out / "chain.xrdml")
        self._assert_same(self.scan, read_scan_file(p4)[0])

    def test_single_column_dat(self) -> None:
        p = self.tmp / "single.dat"
        vals = "\n".join(str(v) for v in range(100, 160))
        p.write_text(f"10.0 0.02 11.18\n{vals}\n", encoding="utf-8")
        s = read_scan_file(p)[0]
        self.assertEqual(s.two_theta.size, 60)
        self.assertAlmostEqual(s.two_theta[1] - s.two_theta[0], 0.02, places=6)

    def test_wavelength_cu_to_co_and_back(self) -> None:
        si = read_scan_file(self.tmp / "raw" / "AP_XRDynamic_Si_NIST640f.hdf5")[0]
        peak_cu = float(si.two_theta[np.argmax(si.intensity)])
        co = convert_wavelength(si, "Cu", "Co")
        peak_co = float(co.two_theta[np.argmax(co.intensity)])
        d = ANODE_WAVELENGTHS["Cu"]["kAlpha1"] / (2 * math.sin(math.radians(peak_cu / 2)))
        expected = 2 * math.degrees(math.asin(ANODE_WAVELENGTHS["Co"]["kAlpha1"] / (2 * d)))
        self.assertAlmostEqual(peak_co, expected, delta=0.05)
        self.assertEqual(co.anode, "Co")
        back = convert_wavelength(co, "Co", "Cu")
        self.assertAlmostEqual(float(back.two_theta[np.argmax(back.intensity)]), peak_cu, delta=0.05)
        mo = convert_wavelength(si, "Cu", "Mo")  # shorter wavelength compresses the pattern
        self.assertLess(mo.two_theta[-1], si.two_theta[-1])

    def test_manager_xrdml_input_targets_and_safety(self) -> None:
        raw = self.tmp / "raw"
        # a raw .xrdml from "another instrument" (no bridge marker)
        foreign = raw / "Foreign_Sample.xrdml"
        text = write_xrdml_file(self.scan, self.tmp / "tmp.xrdml").read_text(encoding="utf-8")
        foreign.write_text(text.replace("SDL Bridge", "Vendor X"), encoding="utf-8")
        si_xy = raw / "AP_XRDynamic_Si_NIST640f.xy"
        si_xy_hash = _sha(si_xy)
        foreign_hash = _sha(foreign)

        mgr = FolderBridgeManager(config_path=self.tmp / "c.json", state_path=self.tmp / "s.json")
        mgr.update_config({"input_dir": str(raw), "output_mode": "same_dir", "output_formats": ["xy", "dat", "asc"]})
        names = {s["name"] for s in mgr.list_source_files()}
        self.assertIn("Foreign_Sample.xrdml", names)

        res = mgr.convert_all()
        self.assertEqual(res["errors"], [])
        self.assertTrue((raw / "Foreign_Sample.xy").is_file())
        self.assertTrue((raw / "Foreign_Sample.dat").is_file())
        # raw Si.xy next to Si.hdf5 must NOT be overwritten by the hdf5 -> xy output
        self.assertEqual(_sha(si_xy), si_xy_hash)
        self.assertEqual(_sha(foreign), foreign_hash)

        # generated files never re-appear as sources (no conversion loops)
        names_after = {s["name"] for s in mgr.list_source_files()}
        self.assertEqual(names, names_after)
        again = mgr.convert_all()
        self.assertEqual(len(again["converted"]), len(res["converted"]))

        # wavelength re-mapping writes *_Co files, including a same-format copy
        mgr.update_config({"anode": "Cu", "target_anode": "Co", "output_formats": ["xrdml", "xy"]})
        mgr.convert_file(foreign)
        self.assertTrue((raw / "Foreign_Sample_Co.xrdml").is_file())
        self.assertTrue((raw / "Foreign_Sample_Co.xy").is_file())
        co = read_scan_file(raw / "Foreign_Sample_Co.xrdml")[0]
        self.assertEqual(co.anode, "Co")
        self.assertEqual(_sha(foreign), foreign_hash)
        item = next(s for s in mgr.list_source_files() if s["name"] == "Foreign_Sample.xrdml")
        self.assertTrue(item["ready"])


class TestLosslessness(unittest.TestCase):
    """Every data point must survive every conversion unchanged (no correction applied)."""

    SOURCES = [
        "AP_XRDynamic_Si_NIST640f.hdf5",
        "AP_XRDynamic_TiO2_Anatase_Rutile.hdf5",
        "AP_XRDynamic_Si_NIST640f.xy",
        "AP_Scan_CaCO3_Calcite.xy",
        "AP_Scan_CaCO3_Calcite.xrdml",
        "AP_Scan_CaCO3_Calcite.asc",
    ]

    def _check(self, ref, back, label: str) -> None:
        self.assertEqual(ref.two_theta.size, back.two_theta.size, f"{label}: point count")
        self.assertLessEqual(float(np.max(np.abs(ref.two_theta - back.two_theta))), 1e-9, f"{label}: 2theta")
        self.assertTrue(np.array_equal(ref.intensity, back.intensity), f"{label}: intensity")

    def test_every_point_identical_through_every_format(self) -> None:
        data_dir = BRIDGE_ROOT / "test_data"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in self.SOURCES:
                ref = read_scan_file(data_dir / name)[0]
                writers = {
                    "xrdml": lambda s, p: write_xrdml_file(s, p),
                    "asc": lambda s, p: write_asc_file(s, p),
                    "xy": lambda s, p: write_xy_file(s, p),
                    "dat": lambda s, p: write_xy_file(s, p),
                }
                for fmt, writer in writers.items():
                    path = writer(ref, out / f"{Path(name).stem}_{Path(name).suffix[1:]}.{fmt}")
                    back = read_scan_file(path)[0]
                    self._check(ref, back, f"{name} -> .{fmt}")
                    # second generation (A -> B -> A) must also be identical
                    path2 = write_xy_file(back, out / f"{path.stem}_gen2_{fmt}.xy")
                    self._check(ref, read_scan_file(path2)[0], f"{name} -> .{fmt} -> .xy")

    def test_wavelength_keeps_every_intensity(self) -> None:
        ref = read_scan_file(BRIDGE_ROOT / "test_data" / "AP_XRDynamic_Si_NIST640f.hdf5")[0]
        for tgt in ("Co", "Mo"):
            conv = convert_wavelength(ref, "Cu", tgt)
            dropped = conv.metadata["WavelengthDroppedPoints"]
            self.assertEqual(conv.two_theta.size + dropped, ref.two_theta.size)
            self.assertTrue(np.array_equal(conv.intensity, ref.intensity[: conv.intensity.size]))
            with tempfile.TemporaryDirectory() as tmp:
                p = write_xrdml_file(conv, Path(tmp) / f"si_{tgt}.xrdml")
                self._check(conv, read_scan_file(p)[0], f"Cu->{tgt} xrdml")
        mo = convert_wavelength(ref, "Cu", "Mo")
        self.assertEqual(mo.metadata["WavelengthDroppedPoints"], 0)  # shorter λ never loses points


if __name__ == "__main__":
    unittest.main(verbosity=2)

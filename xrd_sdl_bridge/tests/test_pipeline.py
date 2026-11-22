"""End-to-end automated tests for the Anton Paar -> HighScore Plus XRD SDL Bridge."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app import create_server  # noqa: E402
from converter import read_hdf5_file, read_xy_file, write_asc_file, write_xrdml_file  # noqa: E402
from highscore_bridge import assess_scan_quality  # noqa: E402
from sample_data_gen import generate_demo_dataset  # noqa: E402
from watcher import FolderBridgeManager  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestXrdSdlBridge(unittest.TestCase):
    def test_non_destructive_conversion_and_xrdml_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            created = generate_demo_dataset(tmp_dir, include_sample_report=True)
            self.assertGreaterEqual(len(created), 4)

            h5_file = tmp_dir / "AP_XRDynamic_Si_NIST640f.hdf5"
            xy_file = tmp_dir / "AP_Scan_CaCO3_Calcite.xy"

            h5_hash_before = _sha256(h5_file)
            xy_hash_before = _sha256(xy_file)

            # Convert HDF5
            h5_scans = read_hdf5_file(h5_file)
            self.assertEqual(len(h5_scans), 1)
            si_scan = h5_scans[0]
            self.assertEqual(si_scan.two_theta.size, 4001)
            self.assertEqual(si_scan.anode, "Cu")

            asc_out = write_asc_file(si_scan, tmp_dir / "AP_XRDynamic_Si_NIST640f.asc")
            xrdml_out = write_xrdml_file(si_scan, tmp_dir / "AP_XRDynamic_Si_NIST640f.xrdml")
            self.assertTrue(asc_out.is_file())
            self.assertTrue(xrdml_out.is_file())

            # Convert XY
            xy_scans = read_xy_file(xy_file)
            self.assertEqual(len(xy_scans), 1)
            cal_scan = xy_scans[0]
            cal_asc = write_asc_file(cal_scan, tmp_dir / "AP_Scan_CaCO3_Calcite.asc")
            cal_xrdml = write_xrdml_file(cal_scan, tmp_dir / "AP_Scan_CaCO3_Calcite.xrdml")
            self.assertTrue(cal_asc.is_file())
            self.assertTrue(cal_xrdml.is_file())

            # Raw source files must remain byte-for-byte identical
            self.assertEqual(_sha256(h5_file), h5_hash_before)
            self.assertEqual(_sha256(xy_file), xy_hash_before)

            # Safety guard must forbid overwriting .hdf5/.h5/.xy
            with self.assertRaises(ValueError):
                write_asc_file(si_scan, h5_file)
            with self.assertRaises(ValueError):
                write_xrdml_file(cal_scan, xy_file)

            # Verify .asc numeric round-trip
            loaded_asc = np.loadtxt(asc_out)
            self.assertEqual(loaded_asc.shape, (4001, 2))
            self.assertTrue(np.allclose(loaded_asc[:, 0], si_scan.two_theta, atol=1e-4))
            self.assertTrue(np.allclose(loaded_asc[:, 1], si_scan.intensity, atol=0.5))

            # Verify strict XRDML v1.3 XML structure and elements
            ns = {"x": "http://www.xrdml.com/XRDMeasurement/1.3"}
            tree = ET.parse(xrdml_out)
            root = tree.getroot()
            self.assertEqual(root.tag, "{http://www.xrdml.com/XRDMeasurement/1.3}xrdMeasurements")
            self.assertIsNotNone(root.find("x:sample", ns))
            self.assertIsNotNone(root.find(".//x:usedWavelength", ns))
            self.assertIsNotNone(root.find(".//x:incidentBeamPath/x:xRayTube/x:anodeMaterial", ns))
            self.assertIsNotNone(root.find(".//x:diffractedBeamPath", ns))

            pos_2th = root.find('.//x:positions[@axis="2Theta"]', ns)
            self.assertIsNotNone(pos_2th)
            self.assertAlmostEqual(float(pos_2th.find("x:startPosition", ns).text), 10.0, places=4)
            self.assertAlmostEqual(float(pos_2th.find("x:endPosition", ns).text), 90.0, places=4)

            cct = root.find(".//x:commonCountingTime", ns)
            self.assertIsNotNone(cct)
            intens_el = root.find(".//x:intensities", ns)
            self.assertIsNotNone(intens_el)
            intens_vals = [float(v) for v in intens_el.text.split()]
            self.assertEqual(len(intens_vals), 4001)
            self.assertTrue(np.allclose(intens_vals, si_scan.intensity, atol=1.0))

            # Verify the automatic QC assessment
            qc = assess_scan_quality(si_scan)
            self.assertTrue(qc["peaks_present"])
            self.assertTrue(qc["fit_for_analysis"])
            self.assertGreaterEqual(qc["peak_count"], 5)

    def test_watcher_and_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cfg_file = tmp_dir / "config.json"
            state_file = tmp_dir / ".state.json"
            data_dir = tmp_dir / "scans"
            generate_demo_dataset(data_dir, include_sample_report=True)

            mgr = FolderBridgeManager(config_path=cfg_file, state_path=state_file)
            mgr.update_config({"input_dir": str(data_dir), "watch_enabled": False})

            server, _ = create_server(manager=mgr, host="127.0.0.1", port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            token = server.bridge_token
            hdr = {"X-Bridge-Token": token}
            try:
                base = f"http://127.0.0.1:{port}"
                with urllib.request.urlopen(urllib.request.Request(f"{base}/api/state", headers=hdr), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    state = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(state["sources"]), 4)
                    self.assertEqual(len(state["phase_reports"]), 1)

                req = urllib.request.Request(
                    f"{base}/api/convert-all",
                    data=b"{}",
                    headers={"Content-Type": "application/json", **hdr},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    conv = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(conv["ok"])
                    self.assertEqual(len(conv["result"]["errors"]), 0)
                    self.assertEqual(len(conv["result"]["converted"]), 3)

                h5_path = urllib.parse.quote(str(data_dir / "AP_XRDynamic_Si_NIST640f.hdf5"))
                with urllib.request.urlopen(urllib.request.Request(f"{base}/api/preview?path={h5_path}", headers=hdr), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    prev = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(prev["total_points"], 4001)
                    self.assertIn("<xrdMeasurements", prev["xrdml_preview"])

                with urllib.request.urlopen(urllib.request.Request(f"{base}/api/hdf5-tree?path={h5_path}", headers=hdr), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    tree = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(tree["tree"]["kind"], "group")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_recursive_subfolder_mirroring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            raw_dir = tmp_dir / "raw"
            inbox_dir = tmp_dir / "HighScore_Inbox"
            alice_dir = raw_dir / "Alice"
            bob_dir = raw_dir / "Bob" / "ProjectX"

            generate_demo_dataset(alice_dir, include_sample_report=False)
            generate_demo_dataset(bob_dir, include_sample_report=False)

            # Rename one file in both folders to identical stem "Scan1" to verify no collision
            (alice_dir / "AP_XRDynamic_Si_NIST640f.hdf5").rename(alice_dir / "Scan1.hdf5")
            (bob_dir / "AP_Scan_CaCO3_Calcite.xy").rename(bob_dir / "Scan1.xy")

            mgr = FolderBridgeManager(config_path=tmp_dir / "config.json", state_path=tmp_dir / ".state.json")
            mgr.update_config(
                {
                    "input_dir": str(raw_dir),
                    "output_mode": "custom_dir",
                    "custom_output_dir": str(inbox_dir),
                    "include_subfolders": True,
                    "watch_enabled": False,
                }
            )

            # Filtered conversion for Alice only
            res_alice = mgr.convert_all(subfolder_filter="Alice")
            self.assertEqual(len(res_alice["errors"]), 0)
            self.assertTrue((inbox_dir / "Alice" / "Scan1.xrdml").is_file())
            self.assertTrue((inbox_dir / "Alice" / "Scan1.asc").is_file())
            self.assertFalse((inbox_dir / "Bob" / "ProjectX" / "Scan1.xrdml").exists())

            # Full recursive conversion converts Bob/ProjectX too without collision
            res_all = mgr.convert_all()
            self.assertEqual(len(res_all["errors"]), 0)
            self.assertTrue((inbox_dir / "Bob" / "ProjectX" / "Scan1.xrdml").is_file())
            self.assertTrue((inbox_dir / "Bob" / "ProjectX" / "Scan1.asc").is_file())

            state = mgr.get_dashboard_state()
            self.assertIn("Alice", state["subfolders"])
            self.assertIn("Bob", state["subfolders"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

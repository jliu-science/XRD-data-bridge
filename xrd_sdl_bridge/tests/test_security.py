"""Security tests for the local web server (must pass before any public release)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app import create_server  # noqa: E402
from highscore_bridge import launch_in_highscore  # noqa: E402
from sample_data_gen import generate_demo_dataset  # noqa: E402
from watcher import FolderBridgeManager  # noqa: E402


class TestServerSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.data = cls.tmp / "scans"
        cls.outside = cls.tmp / "private"
        generate_demo_dataset(cls.data, include_sample_report=False)
        generate_demo_dataset(cls.outside, include_sample_report=False)  # a folder the bridge must not expose
        cls.mgr = FolderBridgeManager(config_path=cls.tmp / "c.json", state_path=cls.tmp / "s.json")
        cls.mgr.update_config({"input_dir": str(cls.data), "output_mode": "same_dir", "highscore_exe": ""})
        cls.server, _ = create_server(manager=cls.mgr, host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.token = cls.server.bridge_token
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmp.cleanup()

    def _call(self, path: str, body: dict | None = None, headers: dict | None = None, raw: bytes | None = None):
        hdr = {"X-Bridge-Token": self.token, "Content-Type": "application/json"}
        hdr.update(headers or {})
        hdr = {k: v for k, v in hdr.items() if v is not None}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, headers=hdr, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    # --- token / origin / host ------------------------------------------------------------
    def test_token_required(self) -> None:
        self.assertEqual(self._call("/api/state")[0], 200)
        self.assertEqual(self._call("/api/state", headers={"X-Bridge-Token": None})[0], 401)
        self.assertEqual(self._call("/api/state", headers={"X-Bridge-Token": "guess"})[0], 401)
        self.assertEqual(self._call("/api/convert-all", body={}, headers={"X-Bridge-Token": None})[0], 401)

    def test_token_is_embedded_in_page_and_changes_per_launch(self) -> None:
        status, page = self._call("/")
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), page)
        self.assertNotIn(b"__BRIDGE_TOKEN__", page)
        other, _ = create_server(manager=self.mgr, host="127.0.0.1", port=0)
        try:
            self.assertNotEqual(other.bridge_token, self.token)
        finally:
            other.server_close()

    def test_dns_rebinding_host_rejected(self) -> None:
        self.assertEqual(self._call("/", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self._call("/api/state", headers={"Host": f"evil.example.com:{self.port}"})[0], 403)
        self.assertEqual(self._call("/api/state", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_cross_site_post_rejected(self) -> None:
        self.assertEqual(self._call("/api/config", body={"anode": "Mo"}, headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(self._call("/api/config", body={"anode": "Mo"}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(
            self._call("/api/config", body={"anode": "Cu"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200
        )

    def test_non_json_post_rejected(self) -> None:
        # a "simple" cross-site form/text POST must not be accepted even with a token
        status, _ = self._call("/api/convert-all", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_security_headers(self) -> None:
        req = urllib.request.Request(self.base + "/", headers={})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

    # --- file-path confinement ------------------------------------------------------------
    def test_paths_outside_data_folders_rejected(self) -> None:
        inside = self.data / "AP_XRDynamic_Si_NIST640f.hdf5"
        outside = self.outside / "AP_XRDynamic_Si_NIST640f.hdf5"
        q = urllib.parse.quote
        self.assertEqual(self._call(f"/api/preview?path={q(str(inside))}")[0], 200)
        self.assertEqual(self._call(f"/api/preview?path={q(str(outside))}")[0], 403)
        self.assertEqual(self._call(f"/api/hdf5-tree?path={q(str(outside))}")[0], 403)
        traversal = str(self.data / ".." / "private" / "AP_XRDynamic_Si_NIST640f.hdf5")
        self.assertEqual(self._call(f"/api/preview?path={q(traversal)}")[0], 403)
        self.assertEqual(self._call("/api/convert-one", body={"path": str(outside)})[0], 403)
        self.assertEqual(self._call("/api/open-highscore", body={"xrdml_path": str(outside)})[0], 403)
        self.assertEqual(self._call("/api/open-highscore", body={"xrdml_path": str(inside)})[0], 403)  # not .xrdml

    def test_demo_generation_ignores_client_folder(self) -> None:
        target = self.tmp / "attacker_dir"
        self.assertEqual(self._call("/api/generate-demo", body={"target_dir": str(target)})[0], 200)
        self.assertFalse(target.exists())

    # --- program launch -------------------------------------------------------------------
    def test_only_highscore_can_be_launched(self) -> None:
        status, body = self._call("/api/config", body={"highscore_exe": r"C:\Windows\System32\cmd.exe"})
        self.assertNotEqual(status, 200)
        self.assertIn(b"HighScore", body)
        self.assertEqual(self.mgr.config.highscore_exe, "")
        fake_xrdml = self.tmp / "x.xrdml"
        fake_xrdml.write_text("<xrdMeasurements/>", encoding="utf-8")
        res = launch_in_highscore(fake_xrdml, r"C:\Windows\System32\calc.exe")
        self.assertFalse(res["ok"])
        self.assertIn("Refusing", res["message"])
        res2 = launch_in_highscore(self.tmp / "x.txt", r"C:\HighScore\HighScorePlus.exe")
        self.assertFalse(res2["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

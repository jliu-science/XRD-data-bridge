"""Tests for the read-only Bruker .raw (RAW1.01) reader."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from converter import read_scan_file, write_xy_file  # noqa: E402

# Optional vendor samples with matching vendor .xy exports (not part of the repository).
SAMPLES = BRIDGE_ROOT.parent / "data testing"


def _make_raw(ranges: list[tuple[float, float, np.ndarray]]) -> bytes:
    head = bytearray(712)
    head[0:7] = b"RAW1.01"
    struct.pack_into("<I", head, 12, len(ranges))
    head[326:330] = b"TEST"
    head[608:610] = b"Cu"
    struct.pack_into("<5d", head, 616, 1.54184, 1.5406, 1.54439, 1.39222, 0.5)
    out = bytes(head)
    for start, step, y in ranges:
        rh = bytearray(304)
        struct.pack_into("<II", rh, 0, 304, y.size)
        struct.pack_into("<2d", rh, 8, start / 2, start)
        struct.pack_into("<d", rh, 176, step)
        struct.pack_into("<f", rh, 192, 1.5)
        struct.pack_into("<d", rh, 240, 1.5406)
        struct.pack_into("<II", rh, 252, 4, 0)
        out += bytes(rh) + y.astype("<f4").tobytes()
    return out


class TestBinaryReaders(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_raw_ranges_read_exactly(self) -> None:
        y1 = np.array([97, 84, 75, 0, 16777216, 1.25], dtype=np.float64)
        y2 = np.array([5, 6, 7], dtype=np.float64)
        p = self.tmp / "s.raw"
        p.write_bytes(_make_raw([(2.0, 0.01, y1), (30.0, 0.02, y2)]))
        scans = read_scan_file(p)
        self.assertEqual(len(scans), 2)
        np.testing.assert_array_equal(scans[0].intensity, y1)
        self.assertEqual(list(scans[0].two_theta), [2.0, 2.01, 2.02, 2.03, 2.04, 2.05])
        np.testing.assert_array_equal(scans[1].intensity, y2)
        self.assertEqual(list(scans[1].two_theta), [30.0, 30.02, 30.04])
        self.assertEqual(scans[0].counting_time, 1.5)

    def test_raw_truncated_file_is_refused(self) -> None:
        p = self.tmp / "s.raw"
        p.write_bytes(_make_raw([(2.0, 0.01, np.ones(50))])[:-8])
        with self.assertRaises(ValueError):
            read_scan_file(p)

    def test_unsupported_raw_version_is_refused(self) -> None:
        p = self.tmp / "s.raw"
        p.write_bytes(b"RAW4.00" + bytes(800))
        with self.assertRaises(ValueError):
            read_scan_file(p)

    def test_xy_round_trip_is_lossless(self) -> None:
        p = self.tmp / "s.raw"
        p.write_bytes(_make_raw([(2.0, 0.01, np.array([97, 84, 75, 1.25, 0, 16777216], dtype=np.float64))]))
        scan = read_scan_file(p)[0]
        out = write_xy_file(scan, self.tmp / "s_out.xy")
        back = read_scan_file(out)[0]
        np.testing.assert_array_equal(back.two_theta, scan.two_theta)
        np.testing.assert_array_equal(back.intensity, scan.intensity)

    def test_rd_is_not_an_input_format(self) -> None:
        p = self.tmp / "s.rd"
        p.write_bytes(b"V3RD" + bytes(300))
        with self.assertRaises(ValueError):
            read_scan_file(p)


@unittest.skipUnless(SAMPLES.is_dir(), "vendor sample files not present")
class TestVendorSamples(unittest.TestCase):
    def _pair(self, src: str, ref: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not (SAMPLES / src).is_file() or not (SAMPLES / ref).is_file():
            self.skipTest(f"{src} / {ref} not present")
        scan = read_scan_file(SAMPLES / src)[0]
        r = np.loadtxt(SAMPLES / ref)
        self.assertEqual(scan.two_theta.size, r.shape[0])
        return scan.two_theta, scan.intensity, r

    def test_bruker_raw_matches_vendor_xy_exactly(self) -> None:
        x, y, r = self._pair("AP1.raw", "AP1.xy")
        np.testing.assert_array_equal(np.round(x, 5), r[:, 0])
        np.testing.assert_array_equal(y, r[:, 1])


if __name__ == "__main__":
    unittest.main()

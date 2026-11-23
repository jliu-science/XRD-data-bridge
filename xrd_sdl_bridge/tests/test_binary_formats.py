"""Tests for the Bruker .raw (RAW1.01) reader and writer."""

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

from converter import is_bridge_generated, read_scan_file, write_bruker_raw_file, write_xy_file  # noqa: E402

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

    def test_raw_writer_round_trip_is_exact(self) -> None:
        src = self.tmp / "s.raw"
        y = np.array([97, 84, 75, 0, 16777216, 1.25, 3], dtype=np.float64)
        src.write_bytes(_make_raw([(2.0, 0.01, y)]))
        scan = read_scan_file(src)[0]
        out = write_bruker_raw_file(scan, self.tmp / "out" / "s.raw")
        back = read_scan_file(out)[0]
        np.testing.assert_array_equal(back.intensity, scan.intensity)
        self.assertLessEqual(float(np.max(np.abs(back.two_theta - scan.two_theta))), 1e-9)
        self.assertTrue(is_bridge_generated(out))
        self.assertEqual(back.wavelength_ka1, scan.wavelength_ka1)

    def test_raw_writer_refuses_lossy_data(self) -> None:
        src = self.tmp / "s.raw"
        src.write_bytes(_make_raw([(2.0, 0.01, np.arange(6, dtype=np.float64))]))
        scan = read_scan_file(src)[0]
        scan.intensity = scan.intensity + 0.1  # 0.1 is not exact in float32
        with self.assertRaises(ValueError):
            write_bruker_raw_file(scan, self.tmp / "a.raw")
        scan = read_scan_file(src)[0]
        scan.two_theta = scan.two_theta.copy()
        scan.two_theta[3] += 0.001  # non-uniform grid
        with self.assertRaises(ValueError):
            write_bruker_raw_file(scan, self.tmp / "b.raw")
        self.assertFalse((self.tmp / "a.raw").exists() or (self.tmp / "b.raw").exists())

    def test_raw_writer_never_overwrites_instrument_file(self) -> None:
        src = self.tmp / "s.raw"
        original = _make_raw([(2.0, 0.01, np.arange(6, dtype=np.float64))])
        src.write_bytes(original)
        scan = read_scan_file(src)[0]
        with self.assertRaises(ValueError):
            write_bruker_raw_file(scan, src)
        self.assertEqual(src.read_bytes(), original)


class TestNexusHdf5(unittest.TestCase):
    """Anton Paar style NeXus files hold the pattern in /entry/data and /entry/instrument/detector."""

    def _write(self, path: Path, detector_counts: np.ndarray) -> np.ndarray:
        import h5py

        tt = 4.0 + np.arange(200) * 0.01
        counts = np.arange(200, dtype=np.float64) * 3 + 7
        with h5py.File(path, "w") as f:
            entry = f.create_group("entry")
            entry.attrs["NX_class"] = "NXentry"
            data = entry.create_group("data")
            data.attrs["NX_class"] = "NXdata"
            data["two_theta"] = tt
            data["counts"] = counts
            det = entry.create_group("instrument").create_group("detector")
            det.attrs["NX_class"] = "NXdetector"
            det["two_theta"] = tt
            det["counts"] = detector_counts
        return counts

    def test_one_scan_per_file_named_after_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "KDP 4-60.hdf5"
            counts = self._write(p, np.arange(200, dtype=np.float64) * 3 + 7)
            scans = read_scan_file(p)
            self.assertEqual(len(scans), 1)
            self.assertEqual(scans[0].name, "KDP 4-60")
            np.testing.assert_array_equal(scans[0].intensity, counts)

    def test_nxdata_wins_when_detector_copy_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.hdf5"
            counts = self._write(p, np.ones(200))
            scans = read_scan_file(p)
            self.assertEqual(len(scans), 1)
            np.testing.assert_array_equal(scans[0].intensity, counts)
            self.assertIn("instrument/detector", scans[0].metadata["HDF5SkippedGroups"])


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

    def test_bruker_raw_rewrite_keeps_identical_data_block(self) -> None:
        src = SAMPLES / "AP1.raw"
        if not src.is_file():
            self.skipTest("AP1.raw not present")
        with tempfile.TemporaryDirectory() as tmp:
            out = write_bruker_raw_file(read_scan_file(src)[0], Path(tmp) / "AP1_copy.raw")
            n = struct.unpack_from("<I", src.read_bytes(), 716)[0]
            self.assertEqual(out.read_bytes()[-4 * n :], src.read_bytes()[-4 * n :])


if __name__ == "__main__":
    unittest.main()

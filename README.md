# XRD Data Bridge

**Lossless powder-XRD format converter for self-driving labs.**

[**Online converter →**](https://jliu-science.github.io/XRD-data-bridge/)

---

## What it does

- **Reads**: `.hdf5` / `.h5` (e.g. Anton Paar XRDynamic), `.xrdml`, Bruker `.raw` (RAW1.01), `.xy` / `.xye`, `.asc` (PANalytical 2-column and Rigaku), `.dat`
- **Writes**: `.xrdml`, `.asc`, `.xy`, `.dat` — choose any combination
- **Wavelength re-mapping**: Cu ↔ Co ↔ Mo ↔ Cr ↔ Fe ↔ Ag via Bragg's law (d-spacing preserved, intensities unchanged)
- **Folder watching** (desktop): new scans are converted automatically, user sub-folders are mirrored
- **Quality check and preview**, optional hand-off to HighScore Plus / ICDD Search & Match

Bruker `.raw` is a read-only input. PANalytical `.rd` is intentionally not supported: it stores each point as round(10·√counts), so it cannot reproduce the exact counts; use the `.xrdml` instead (see [notes](xrd_sdl_bridge/docs/BINARY_FORMATS_NOTES.md)).

## Data integrity

- **Every data point is preserved exactly**: no rounding, truncation, interpolation, resampling, smoothing or correction. Numbers are written in the shortest form that reads back to the identical value.
- **Raw files are never modified or overwritten**. Files the bridge writes carry a marker so they are never mistaken for raw data.
- Wavelength re-mapping moves 2θ by design (physics, not error); points beyond 180° cannot exist and are dropped and reported.
- Verified by automated tests: exact round-trips through every format, SHA-256 checks of the raw folder in every mode.

## Online converter (students, phones)

The online converter uses the same dashboard as the desktop app. Choose files or a folder, select your output formats, and click **Convert All** to download a ZIP. Conversion runs on your device via [Pyodide](https://pyodide.org). Live folder watching and launching HighScore require the desktop app.

## Desktop bridge (lab PC, Windows)

1. Download `XRD-data-bridge-<version>-win64.zip` from [Releases](https://github.com/jliu-science/XRD-data-bridge/releases) — it includes a portable Python, nothing to install.
2. Unzip the **whole folder** anywhere (local disk or network share).
3. Double-click `start_xrd_bridge.bat`; the page opens at `http://127.0.0.1:8765`.
4. Pick the raw data folder and output folder, choose output formats, press **Convert All** or turn on **Live watch**.

Updating: replace the folder but keep your `xrd_sdl_bridge/config.json` (your settings).

### Running from source

```bash
pip install -r requirements.txt
python xrd_sdl_bridge/app.py --open-browser
```

### Security

The desktop bridge is a **local-only** service: it serves only `localhost`, every API call needs a random per-launch token embedded in its own page, cross-site requests are rejected, file access is confined to the configured folders, and only HighScore / Data Viewer can be launched. Do not expose it to a network.

## Development

```bash
cd xrd_sdl_bridge
python -m unittest discover -s tests -v     # security, data-integrity, formats, pipeline
python ../tools/build_web.py --serve         # online converter at http://127.0.0.1:8800
python ../tools/make_release.py 1.0.0        # Windows zip (needs ./python portable runtime)
```

## License

[MIT](LICENSE) © 2026 Jie.
Bundled third-party components keep their own licenses (Python: PSF; NumPy and h5py: BSD-3-Clause); their license files are included in the release zip.

Anton Paar, XRDynamic, Malvern Panalytical, HighScore, Bruker and ICDD are trademarks of their respective owners. This project is independent and not affiliated with or endorsed by them.

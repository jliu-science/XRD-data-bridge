"""Thread-safe folder watcher, config persistence, and non-destructive conversion manager."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from converter import (
    ANODE_WAVELENGTHS,
    FORMAT_EXTENSION,
    INPUT_EXTENSIONS,
    OUTPUT_FORMATS,
    PROTECTED_EXTENSIONS,
    convert_wavelength,
    is_bridge_generated,
    normalize_anode,
    read_scan_file,
    write_asc_file,
    write_bruker_raw_file,
    write_xrdml_file,
    write_xy_file,
)
from highscore_bridge import (
    assess_scan_quality,
    discover_highscore_executables,
    is_allowed_viewer_exe,
    launch_in_highscore,
    parse_highscore_phase_reports,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEST_DIR = BASE_DIR / "test_data"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / ".bridge_state.json"

SUPPORTED_INPUT_EXTS = set(INPUT_EXTENSIONS)
# Lower number = more "raw". When several files share a stem in one folder, the most raw wins.
# Lower = closer to the instrument's own counts (Bruker .raw holds exact float32 counts).
SOURCE_PRIORITY = {".hdf5": 0, ".h5": 0, ".raw": 0, ".xrdml": 1, ".xy": 2, ".xye": 2, ".dat": 3, ".asc": 4}
_MARKER_EXTS = {".xrdml", ".xy", ".xye", ".dat", ".raw"}
_GENERATED_KEY = "__generated__"


@dataclass
class BridgeConfig:
    """User-configurable settings for the XRD SDL Bridge."""

    input_dir: str = ""
    output_mode: str = "same_dir"  # "same_dir" or "custom_dir"
    custom_output_dir: str = ""
    include_subfolders: bool = True
    anode: str = "Cu"
    asc_flavor: str = "panalytical_2col"  # "panalytical_2col" or "rigaku_asc"
    angle_dataset_override: str = ""
    intensity_dataset_override: str = ""
    highscore_exe: str = ""
    auto_launch_highscore: bool = False
    watch_enabled: bool = False
    poll_interval_sec: float = 2.0
    output_formats: list[str] = field(default_factory=lambda: ["xrdml", "asc"])
    target_anode: str = ""  # "" = keep the source wavelength; else re-map 2Theta to this anode


class FolderBridgeManager:
    """Manages folder monitoring, non-destructive conversions, and conversion history."""

    def __init__(self, config_path: Path = CONFIG_PATH, state_path: Path = STATE_PATH) -> None:
        self._lock = threading.RLock()
        self.config_path = config_path
        self.state_path = state_path
        self.config = self._load_config()
        self.processed_cache: dict[str, dict[str, int]] = self._load_state()
        self.generated: set[str] = set(self.processed_cache.pop(_GENERATED_KEY, []) or [])
        self._marker_cache: dict[str, tuple[int, bool]] = {}
        self.logs: list[dict[str, str]] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        if not self.config.input_dir:
            self.config.input_dir = str(DEFAULT_TEST_DIR)
        if not self.config.custom_output_dir:
            self.config.custom_output_dir = str(DEFAULT_TEST_DIR / "converted")

        if not self.config.highscore_exe:
            discovered = discover_highscore_executables()
            if discovered:
                self.config.highscore_exe = discovered[0]
                self._save_config()

        try:
            Path(self.config.input_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            self.config.input_dir = str(DEFAULT_TEST_DIR)
            self.config.custom_output_dir = str(DEFAULT_TEST_DIR / "converted")
            Path(self.config.input_dir).mkdir(parents=True, exist_ok=True)
            self._save_config()
        self._append_log("INFO", f"Bridge initialized. Linked folder: {self.config.input_dir}")

    def _load_config(self) -> BridgeConfig:
        if self.config_path.is_file():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                valid_keys = {f for f in BridgeConfig.__dataclass_fields__}
                filtered = {k: v for k, v in raw.items() if k in valid_keys}
                cfg = BridgeConfig(**filtered)
                cfg.output_formats = self._clean_formats(cfg.output_formats) or ["xrdml", "asc"]
                cfg.target_anode = self._clean_anode(cfg.target_anode)
                return cfg
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        cfg = BridgeConfig()
        self._save_config(cfg)
        return cfg

    def _save_config(self, cfg: BridgeConfig | None = None) -> None:
        target = cfg or self.config
        data = asdict(target)
        if data["input_dir"] == str(DEFAULT_TEST_DIR):
            data["input_dir"] = ""
        if data["custom_output_dir"] == str(DEFAULT_TEST_DIR / "converted"):
            data["custom_output_dir"] = ""
        self.config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_state(self) -> dict[str, dict[str, int]]:
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save_state(self) -> None:
        data: dict[str, Any] = dict(self.processed_cache)
        data[_GENERATED_KEY] = sorted(self.generated)
        self.state_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _clean_formats(value: Any) -> list[str]:
        items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        wanted = {str(x).strip().lower().lstrip(".") for x in items}
        return [f for f in OUTPUT_FORMATS if f in wanted]

    @staticmethod
    def _clean_anode(value: Any) -> str:
        text = str(value or "").strip()
        if not text or text.lower() in ("keep", "none", "same", "original"):
            return ""
        return normalize_anode(text)

    def _target_anode(self) -> str:
        """Anode to re-map to, or '' when no wavelength conversion is requested."""
        tgt = self.config.target_anode
        if not tgt or tgt == normalize_anode(self.config.anode):
            return ""
        return tgt

    def _is_generated(self, path: Path) -> bool:
        """True for files this bridge wrote (registry or header marker)."""
        key = str(path)
        if key in self.generated:
            return True
        if path.suffix.lower() not in _MARKER_EXTS:
            return False
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return False
        cached = self._marker_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        flag = is_bridge_generated(path)
        self._marker_cache[key] = (mtime, flag)
        return flag

    def _append_log(self, level: str, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self.logs.insert(0, {"time": ts, "level": level, "message": message})
            if len(self.logs) > 80:
                self.logs = self.logs[:80]

    def update_config(self, patch: dict[str, Any]) -> BridgeConfig:
        with self._lock:
            for k, v in patch.items():
                if hasattr(self.config, k):
                    if k == "poll_interval_sec":
                        setattr(self.config, k, max(0.5, float(v)))
                    elif k == "output_formats":
                        fmts = self._clean_formats(v)
                        if fmts:
                            self.config.output_formats = fmts
                    elif k == "target_anode":
                        self.config.target_anode = self._clean_anode(v)
                    elif k == "highscore_exe":
                        exe = str(v or "").strip()
                        if exe and not is_allowed_viewer_exe(exe):
                            raise ValueError("HighScore path must point to HighScorePlus.exe, HighScore.exe or DataViewer.exe")
                        self.config.highscore_exe = exe
                    elif k in ("auto_launch_highscore", "watch_enabled", "include_subfolders"):
                        setattr(self.config, k, bool(v))
                    else:
                        setattr(self.config, k, str(v).strip())
            if not self.config.input_dir:
                self.config.input_dir = str(DEFAULT_TEST_DIR)
            if not self.config.custom_output_dir:
                self.config.custom_output_dir = str(DEFAULT_TEST_DIR / "converted")
            Path(self.config.input_dir).mkdir(parents=True, exist_ok=True)
            if self.config.output_mode == "custom_dir" and self.config.custom_output_dir:
                Path(self.config.custom_output_dir).mkdir(parents=True, exist_ok=True)
            self._save_config()
            self._append_log("CONFIG", f"Settings updated (watch={self.config.watch_enabled}, dir={self.config.input_dir})")
            return self.config

    def _relative_subfolder(self, source_file: Path) -> Path:
        """Compute source_file's folder path relative to input_dir for mirroring user subfolders."""
        input_root = Path(self.config.input_dir)
        try:
            rel = os.path.relpath(str(source_file.parent), str(input_root))
        except ValueError:
            return Path(".")
        if not rel or rel == "." or rel.startswith(".."):
            return Path(".")
        rel_path = Path(rel)
        return Path(".") if rel_path.is_absolute() else rel_path

    def _resolve_output_dir(self, source_file: Path, create: bool = True) -> Path:
        if self.config.output_mode == "custom_dir" and self.config.custom_output_dir:
            base_out = Path(self.config.custom_output_dir)
            if self.config.include_subfolders:
                rel_sub = self._relative_subfolder(source_file)
                out_dir = base_out / rel_sub if rel_sub != Path(".") else base_out
            else:
                out_dir = base_out
        else:
            out_dir = source_file.parent
        if create:
            out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def is_inside_data_folders(self, path: Path) -> bool:
        """True if path lies inside the raw-data folder or the configured output folder."""
        roots = [self.config.input_dir]
        if self.config.custom_output_dir:
            roots.append(self.config.custom_output_dir)
        try:
            target = Path(path).resolve()
        except OSError:
            return False
        for root in roots:
            if not root:
                continue
            try:
                target.relative_to(Path(root).resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    def _output_stem(self, scan_name: str) -> str:
        tgt = self._target_anode()
        return f"{scan_name}_{tgt}" if tgt else scan_name

    def _output_targets(self, source_file: Path, scan_name: str, create: bool = True) -> dict[str, Path]:
        """Map each selected output format to its target path (skips the source's own format
        unless a wavelength conversion makes a same-format copy meaningful)."""
        out_dir = self._resolve_output_dir(source_file, create=create)
        stem = self._output_stem(scan_name)
        converting = bool(self._target_anode())
        src_ext = source_file.suffix.lower()
        targets: dict[str, Path] = {}
        for fmt in self.config.output_formats:
            ext = FORMAT_EXTENSION[fmt]
            if ext == src_ext and not converting:
                continue
            targets[fmt] = out_dir / f"{stem}{ext}"
        return targets

    def _may_write(self, target: Path, source: Path) -> tuple[bool, str]:
        """Decide whether target may be (over)written. Never touches raw files."""
        try:
            if target.resolve() == source.resolve():
                return False, "same file as the source"
        except OSError:
            pass
        ext = target.suffix.lower()
        if ext in PROTECTED_EXTENSIONS:
            return False, "protected raw format"
        if not target.exists():
            return True, ""
        if self._is_generated(target):
            return True, ""
        if self.config.output_mode == "custom_dir" and self.config.custom_output_dir:
            try:
                target.resolve().relative_to(Path(self.config.custom_output_dir).resolve())
                if ext not in _MARKER_EXTS:
                    return True, ""
            except (ValueError, OSError):
                pass
        if ext in (".asc",) and SOURCE_PRIORITY.get(source.suffix.lower(), 9) < SOURCE_PRIORITY[ext]:
            # Legacy .asc output written next to a more-raw sibling of the same sample.
            return True, ""
        return False, f"{target.name} already exists and looks like a raw file"

    def _filter_sources(self, candidates: list[Path]) -> list[Path]:
        """Drop files the bridge generated, and .asc/.dat that sit next to a more-raw sibling."""
        kept = [p for p in candidates if not self._is_generated(p)]
        best: dict[tuple[str, str], int] = {}
        for p in kept:
            key = (str(p.parent).lower(), p.stem.lower())
            best[key] = min(best.get(key, 9), SOURCE_PRIORITY.get(p.suffix.lower(), 9))
        result: list[Path] = []
        for p in kept:
            ext = p.suffix.lower()
            if ext in (".asc", ".dat"):
                if best[(str(p.parent).lower(), p.stem.lower())] < SOURCE_PRIORITY[ext]:
                    continue
            result.append(p)
        return result

    def _iter_scan_files(self, input_dir: Path, recursive: bool, subfolder_filter: str = "") -> list[Path]:
        """Collect supported scan files in input_dir (optionally scanning user subfolders)."""
        if not input_dir.is_dir():
            return []

        skip_names = {"highscore_results", "converted"}
        custom_resolved: Path | None = None
        if self.config.output_mode == "custom_dir" and self.config.custom_output_dir:
            try:
                custom_resolved = Path(self.config.custom_output_dir).resolve()
            except OSError:
                custom_resolved = None

        candidates: list[Path] = []
        if recursive:
            for root_str, dirnames, filenames in os.walk(input_dir):
                root_path = Path(root_str)
                kept_dirs: list[str] = []
                for d in dirnames:
                    if d.startswith(".") or d.lower() in skip_names:
                        continue
                    if custom_resolved is not None:
                        try:
                            if (root_path / d).resolve() == custom_resolved:
                                continue
                        except OSError:
                            pass
                    kept_dirs.append(d)
                dirnames[:] = kept_dirs
                for fname in filenames:
                    p = root_path / fname
                    if p.suffix.lower() in SUPPORTED_INPUT_EXTS:
                        candidates.append(p)
        else:
            for p in input_dir.iterdir():
                if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS:
                    candidates.append(p)

        candidates = self._filter_sources(candidates)
        filt = subfolder_filter.strip().strip("/\\")
        if not filt:
            return candidates

        filtered: list[Path] = []
        for p in candidates:
            rel_sub = self._relative_subfolder(p)
            rel_posix = "(root)" if rel_sub == Path(".") else rel_sub.as_posix()
            if filt == "(root)":
                if rel_posix == "(root)":
                    filtered.append(p)
            elif rel_posix == filt or rel_posix.startswith(filt + "/"):
                filtered.append(p)
        return filtered

    def _is_file_stable(self, file_path: Path) -> bool:
        """Ensure instrument software has finished flushing bytes before reading."""
        try:
            st1 = file_path.stat()
            if st1.st_size <= 0:
                return False
            time.sleep(0.35)
            st2 = file_path.stat()
            return st1.st_size == st2.st_size and st1.st_mtime_ns == st2.st_mtime_ns
        except OSError:
            return False

    def convert_file(
        self,
        filepath: str | Path,
        trigger_highscore: bool | None = None,
        check_stability: bool = False,
    ) -> dict[str, Any]:
        """Convert one source file to every selected output format; the original is never modified."""
        src = Path(filepath)
        if not src.is_file():
            raise FileNotFoundError(f"Source file not found: {src}")
        if src.suffix.lower() not in SUPPORTED_INPUT_EXTS:
            raise ValueError(f"Unsupported extension: {src.suffix}")

        if check_stability and not self._is_file_stable(src):
            return {"skipped": True, "reason": "file still being written", "source": str(src)}

        with self._lock:
            cfg = self.config

        scans = read_scan_file(
            src,
            angle_dataset=cfg.angle_dataset_override or None,
            intensity_dataset=cfg.intensity_dataset_override or None,
            default_anode=cfg.anode,
        )

        outputs: list[dict[str, Any]] = []
        should_launch = cfg.auto_launch_highscore if trigger_highscore is None else trigger_highscore
        target_anode = self._target_anode()
        written_fmts: set[str] = set()

        for scan in scans:
            out_scan = convert_wavelength(scan, cfg.anode, target_anode) if target_anode else scan
            out_anode = target_anode or cfg.anode
            files: dict[str, str] = {}
            skipped: list[dict[str, str]] = []
            for fmt, path in self._output_targets(src, scan.name, create=True).items():
                ok, why = self._may_write(path, src)
                if not ok:
                    skipped.append({"format": fmt, "path": str(path), "reason": why})
                    self._append_log("WARN", f"Skipped {path.name}: {why}")
                    continue
                try:
                    if fmt == "xrdml":
                        write_xrdml_file(out_scan, path, anode_override=out_anode)
                    elif fmt == "raw":
                        write_bruker_raw_file(out_scan, path, anode_override=out_anode)
                    elif fmt == "asc":
                        write_asc_file(out_scan, path, asc_flavor=cfg.asc_flavor, anode_override=out_anode)
                    else:
                        write_xy_file(out_scan, path, anode_override=out_anode)
                except ValueError as exc:  # format cannot hold the data losslessly / safety guard
                    skipped.append({"format": fmt, "path": str(path), "reason": str(exc)})
                    self._append_log("WARN", f"Skipped {path.name}: {exc}")
                    continue
                files[fmt] = str(path)
                written_fmts.add(fmt)
                with self._lock:
                    self.generated.add(str(path))
            qc = assess_scan_quality(scan)

            launch_res = None
            if should_launch and files.get("xrdml"):
                launch_res = launch_in_highscore(files["xrdml"], cfg.highscore_exe)
                self._append_log("HIGHSCORE", launch_res.get("message", ""))

            outputs.append(
                {
                    "scan_name": scan.name,
                    "points": int(out_scan.two_theta.size),
                    "asc_path": files.get("asc", ""),
                    "xrdml_path": files.get("xrdml", ""),
                    "files": files,
                    "skipped": skipped,
                    "anode": out_anode,
                    "qc": qc,
                    "highscore_launch": launch_res,
                }
            )

        st = src.stat()
        with self._lock:
            self.processed_cache[str(src)] = {
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "profile": self._profile(),
                "written": len(written_fmts),
            }
            self._save_state()

        fmt_txt = " + ".join(f".{f}" for f in OUTPUT_FORMATS if f in written_fmts) or "nothing new"
        wl_txt = f", re-mapped {normalize_anode(cfg.anode)} -> {target_anode}" if target_anode else ""
        self._append_log("OK", f"Converted {src.name} -> {len(outputs)} scan(s) ({fmt_txt}{wl_txt}); raw file preserved.")
        return {"skipped": False, "source": str(src), "outputs": outputs}

    def _profile(self) -> str:
        """Signature of settings that change the produced files (forces re-conversion when edited)."""
        c = self.config
        return f"{','.join(c.output_formats)}|{normalize_anode(c.anode)}|{self._target_anode()}|{c.asc_flavor}"

    def list_source_files(self) -> list[dict[str, Any]]:
        """List all supported raw scan files in the linked directory (including subfolders) and their output status."""
        with self._lock:
            input_dir = Path(self.config.input_dir)
            recursive = self.config.include_subfolders

        if not input_dir.is_dir():
            return []

        items: list[dict[str, Any]] = []
        files = self._iter_scan_files(input_dir, recursive=recursive)
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            rel_sub = self._relative_subfolder(p)
            sub_posix = "." if rel_sub == Path(".") else rel_sub.as_posix()
            top_folder = "(root)" if rel_sub == Path(".") else rel_sub.parts[0]
            targets = self._output_targets(p, p.stem, create=False)
            outputs = {fmt: {"exists": path.is_file(), "path": str(path)} for fmt, path in targets.items()}
            asc_info = outputs.get("asc") or {"exists": False, "path": ""}
            if "xrdml" in outputs:
                xrdml_info = outputs["xrdml"]
            elif p.suffix.lower() == ".xrdml":
                xrdml_info = {"exists": True, "path": str(p)}  # the source itself opens in HighScore
            else:
                xrdml_info = {"exists": False, "path": ""}
            items.append(
                {
                    "name": p.name,
                    "stem": p.stem,
                    "ext": p.suffix.lower(),
                    "path": str(p),
                    "subfolder": sub_posix,
                    "top_folder": top_folder,
                    "mtime_ns": int(st.st_mtime_ns),
                    "size_kb": round(st.st_size / 1024.0, 2),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "protected_original": p.suffix.lower() in PROTECTED_EXTENSIONS,
                    "outputs": outputs,
                    "ready": bool(outputs) and all(o["exists"] for o in outputs.values()),
                    "asc_exists": asc_info["exists"],
                    "asc_path": asc_info["path"],
                    "xrdml_exists": xrdml_info["exists"],
                    "xrdml_path": xrdml_info["path"],
                }
            )
        items.sort(key=lambda d: (-d["mtime_ns"], d["subfolder"].lower(), d["name"].lower()))
        return items

    def _select_prioritized_sources(self, files: list[Path]) -> list[Path]:
        """When both foo.hdf5 and foo.xy exist in the same folder, prioritize .hdf5 for batch auto-conversion."""
        by_folder_stem: dict[tuple[str, str], list[Path]] = {}
        for f in files:
            by_folder_stem.setdefault((str(f.parent).lower(), f.stem.lower()), []).append(f)

        selected: list[Path] = []
        for stem_files in by_folder_stem.values():
            selected.append(min(stem_files, key=lambda p: SOURCE_PRIORITY.get(p.suffix.lower(), 9)))
        return sorted(selected, key=lambda p: str(p).lower())

    def convert_all(self, only_new: bool = False, subfolder_filter: str = "") -> dict[str, Any]:
        """Batch-convert source files in the linked directory (and subfolders) without touching originals."""
        with self._lock:
            input_dir = Path(self.config.input_dir)
            recursive = self.config.include_subfolders
        if not input_dir.is_dir():
            return {"converted": [], "errors": [f"Directory does not exist: {input_dir}"]}

        all_files = self._iter_scan_files(input_dir, recursive=recursive, subfolder_filter=subfolder_filter)
        targets = self._select_prioritized_sources(all_files)

        converted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for src in targets:
            if only_new:
                st = src.stat()
                cached = self.processed_cache.get(str(src))
                tgts = self._output_targets(src, src.stem, create=False)
                if not tgts:
                    continue
                out_dir = self._resolve_output_dir(src, create=False)
                has_outputs = any(t.is_file() for t in tgts.values()) or any(
                    any(out_dir.glob(f"{src.stem}_*{t.suffix}")) for t in tgts.values()
                )
                if (
                    cached
                    and cached.get("size") == st.st_size
                    and cached.get("mtime_ns") == st.st_mtime_ns
                    and cached.get("profile", self._profile()) == self._profile()
                    and (has_outputs or cached.get("written") == 0)
                ):
                    continue
            try:
                res = self.convert_file(src, check_stability=only_new)
                if not res.get("skipped"):
                    converted.append(res)
            except Exception as exc:  # noqa: BLE001
                err_msg = f"Failed converting {src.name}: {exc}"
                self._append_log("ERROR", err_msg)
                errors.append({"file": src.name, "error": str(exc)})

        return {"converted": converted, "errors": errors}

    def preview_scan(self, file_path: Path) -> dict[str, Any]:
        """Read the scan, quality metrics and display data for either dashboard transport."""
        cfg = self.config
        scans = read_scan_file(
            file_path,
            angle_dataset=cfg.angle_dataset_override or None,
            intensity_dataset=cfg.intensity_dataset_override or None,
            default_anode=cfg.anode,
        )
        scan = scans[0]
        qc = assess_scan_quality(scan)

        # Downsample only if extremely dense (>4500 pts) for smooth UI rendering.
        x = scan.two_theta
        y = scan.intensity
        if x.size > 4500:
            step = max(1, x.size // 4500)
            x = x[::step]
            y = y[::step]

        xrdml_cand = self._output_targets(file_path, scan.name, create=False).get("xrdml")
        if (xrdml_cand is None or not xrdml_cand.is_file()) and file_path.suffix.lower() == ".xrdml":
            xrdml_cand = file_path
        xrdml_head = ""
        if xrdml_cand is not None and xrdml_cand.is_file():
            xrdml_head = "\n".join(xrdml_cand.read_text(encoding="utf-8", errors="replace").splitlines()[:28])

        anode, wl = scan.resolved_wavelengths(cfg.anode)
        return {
            "name": scan.name,
            "source_format": scan.source_format,
            "anode": anode,
            "ka1": wl["kAlpha1"],
            "total_points": int(scan.two_theta.size),
            "two_theta": [round(float(v), 4) for v in x],
            "intensity": [round(float(v), 2) for v in y],
            "metadata": scan.metadata,
            "qc": qc,
            "xrdml_preview": xrdml_head,
        }

    def get_dashboard_state(self) -> dict[str, Any]:
        """Collect complete state snapshot for the web UI."""
        with self._lock:
            cfg_dict = asdict(self.config)
            logs_copy = list(self.logs)
        sources = self.list_source_files()
        subfolders = sorted({s["top_folder"] for s in sources if s.get("top_folder") and s["top_folder"] != "(root)"})
        if any(s.get("top_folder") == "(root)" for s in sources):
            subfolders.insert(0, "(root)")

        phase_reports: list[dict[str, Any]] = []
        seen_result_dirs: set[str] = set()
        for base_dir_str in (cfg_dict["input_dir"], cfg_dict.get("custom_output_dir", "")):
            if not base_dir_str:
                continue
            rdir = Path(base_dir_str) / "highscore_results"
            rkey = str(rdir).lower()
            if rkey not in seen_result_dirs:
                seen_result_dirs.add(rkey)
                phase_reports.extend(parse_highscore_phase_reports(rdir))

        discovered_exes = discover_highscore_executables()
        return {
            "config": cfg_dict,
            "sources": sources,
            "subfolders": subfolders,
            "phase_reports": phase_reports,
            "discovered_exes": discovered_exes,
            "logs": logs_copy,
        }

    def start_background_watcher(self) -> None:
        """Start daemon polling thread for live folder watching."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                with self._lock:
                    enabled = self.config.watch_enabled
                    interval = self.config.poll_interval_sec
                if enabled:
                    self.convert_all(only_new=True)
                self._stop_event.wait(timeout=max(0.5, interval))

        self._thread = threading.Thread(target=_loop, name="xrd-folder-watcher", daemon=True)
        self._thread.start()

    def stop_background_watcher(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

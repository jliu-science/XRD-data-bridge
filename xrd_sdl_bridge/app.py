"""Local HTTP server and REST API for XRD Data Bridge.

Security model (the server is meant for the local machine only):
  * binds to 127.0.0.1 by default and rejects any request whose Host header is not a loopback name
    (blocks DNS-rebinding attacks);
  * every /api/ call must carry the per-launch random token that is embedded in the page served by
    this process (a web page from another site cannot read it), and POSTs must be JSON with a
    matching Origin (blocks cross-site request forgery from other pages in the same browser);
  * file paths sent by the page are confined to the configured raw-data / output folders;
  * only HighScore / Data Viewer executables can be launched.
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Ensure sibling imports and portable Tcl/Tk work regardless of working directory.
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

_PORTABLE_TCL = Path(sys.executable).resolve().parent / "tcl"
if (_PORTABLE_TCL / "tcl8.6").is_dir() and "TCL_LIBRARY" not in os.environ:
    os.environ["TCL_LIBRARY"] = str(_PORTABLE_TCL / "tcl8.6")
if (_PORTABLE_TCL / "tk8.6").is_dir() and "TK_LIBRARY" not in os.environ:
    os.environ["TK_LIBRARY"] = str(_PORTABLE_TCL / "tk8.6")

from converter import inspect_hdf5_tree, read_scan_file  # noqa: E402
from highscore_bridge import assess_scan_quality, is_allowed_viewer_exe, launch_in_highscore  # noqa: E402
from sample_data_gen import generate_demo_dataset  # noqa: E402
from watcher import FolderBridgeManager  # noqa: E402

STATIC_DIR = APP_DIR / "static"
SCRIPTS_DIR = APP_DIR / "highscore_scripts"
TOKEN_PLACEHOLDER = b"__BRIDGE_TOKEN__"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _split_host(value: str) -> tuple[str, str]:
    """Split 'host:port' / '[v6]:port' into (host, port) — lower-cased host, '' port if absent."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        return host + "]", rest.lstrip(":")
    if value.count(":") == 1:
        host, port = value.split(":")
        return host, port
    return value, ""


def _native_browse_folder(initial_dir: str = "") -> str:
    """Open native Windows folder selection dialog via tkinter."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(initialdir=initial_dir or str(APP_DIR), title="Select the raw XRD data folder")
    finally:
        root.destroy()
    return chosen or ""


def _native_browse_exe(initial_dir: str = "") -> str:
    """Open native Windows file dialog to pick HighScorePlus.exe or DataViewer.exe."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askopenfilename(
            initialdir=initial_dir or r"C:\Program Files",
            title="Select HighScorePlus.exe or DataViewer.exe",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    return chosen or ""


def build_handler(manager: FolderBridgeManager, token: str) -> type[BaseHTTPRequestHandler]:
    """Create HTTP request handler bound to a FolderBridgeManager instance and a session token."""

    class BridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        bridge_token = token

        # ------------------------------------------------------------ request guards
        def _host_ok(self) -> bool:
            host, _ = _split_host(self.headers.get("Host", ""))
            return host in LOOPBACK_HOSTS

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # non-browser client (e.g. tests); browsers always send Origin on POST
            parsed = urllib.parse.urlparse(origin)
            host, port = _split_host(parsed.netloc)
            own_port = str(self.server.server_address[1])
            return parsed.scheme == "http" and host in LOOPBACK_HOSTS and port == own_port

        def _token_ok(self) -> bool:
            sent = self.headers.get("X-Bridge-Token", "")
            return bool(sent) and hmac.compare_digest(sent, self.bridge_token)

        def _reject(self, why: str, status: int = HTTPStatus.FORBIDDEN) -> None:
            self._send_json({"error": f"Request blocked: {why}"}, status=status)

        def _guard_api(self, is_post: bool) -> bool:
            if not self._host_ok():
                self._reject("unexpected Host header (only localhost is served)")
                return False
            if not self._origin_ok():
                self._reject("cross-origin request")
                return False
            if not self._token_ok():
                self._reject("missing or invalid session token - reload the page", HTTPStatus.UNAUTHORIZED)
                return False
            if is_post:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    self._reject("POST body must be application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                    return False
            return True

        def _allowed_path(self, raw: str, *, must_exist: bool = True, suffix: str | None = None) -> Path:
            """Resolve a client-supplied path and confine it to the raw-data / output folders."""
            p = Path(str(raw)).expanduser()
            try:
                resolved = p.resolve()
            except OSError as exc:
                raise PermissionError(f"Invalid path: {raw}") from exc
            if suffix and resolved.suffix.lower() != suffix:
                raise PermissionError(f"Only {suffix} files are allowed here")
            if not manager.is_inside_data_folders(resolved):
                raise PermissionError("Path is outside the configured raw-data / output folders")
            if must_exist and not resolved.is_file():
                raise FileNotFoundError(f"File not found: {resolved}")
            return resolved

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Keep terminal clean unless debugging.
            return

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, html_path: Path) -> None:
            if not html_path.is_file():
                self._send_json({"error": "index.html not found"}, status=HTTPStatus.NOT_FOUND)
                return
            raw = html_path.read_bytes().replace(TOKEN_PLACEHOLDER, self.bridge_token.encode("ascii"))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._security_headers()
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            return json.loads(body.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if parsed.path in ("/", "/index.html"):
                    if not self._host_ok():
                        self._reject("unexpected Host header (only localhost is served)")
                        return
                    self._send_html(STATIC_DIR / "index.html")
                    return

                if not self._guard_api(is_post=False):
                    return

                if parsed.path == "/api/state":
                    self._send_json(manager.get_dashboard_state())
                    return

                if parsed.path == "/api/preview":
                    file_path = qs.get("path", [""])[0]
                    if not file_path:
                        self._send_json({"error": "Missing 'path' query parameter"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    file_path = str(self._allowed_path(file_path))
                    cfg = manager.config
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

                    src_path = Path(file_path)
                    xrdml_cand = manager._output_targets(src_path, scan.name, create=False).get("xrdml")
                    if (xrdml_cand is None or not xrdml_cand.is_file()) and src_path.suffix.lower() == ".xrdml":
                        xrdml_cand = src_path
                    xrdml_head = ""
                    if xrdml_cand is not None and xrdml_cand.is_file():
                        xrdml_head = "\n".join(xrdml_cand.read_text(encoding="utf-8", errors="replace").splitlines()[:28])

                    anode, wl = scan.resolved_wavelengths(cfg.anode)
                    self._send_json(
                        {
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
                    )
                    return

                if parsed.path == "/api/hdf5-tree":
                    file_path = qs.get("path", [""])[0]
                    if not file_path:
                        self._send_json({"error": "Missing 'path' query parameter"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    self._send_json(inspect_hdf5_tree(self._allowed_path(file_path)))
                    return

                if parsed.path == "/api/setup-guide":
                    pas_file = SCRIPTS_DIR / "auto_search_match_icdd.pas"
                    md_file = SCRIPTS_DIR / "HIGHSCORE_SETUP_GUIDE.md"
                    self._send_json(
                        {
                            "pascal_script": pas_file.read_text(encoding="utf-8") if pas_file.is_file() else "",
                            "guide_md": md_file.read_text(encoding="utf-8") if md_file.is_file() else "",
                        }
                    )
                    return

                self._send_json({"error": f"Unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            try:
                if not self._guard_api(is_post=True):
                    return
                body = self._read_json_body()

                if parsed.path == "/api/config":
                    manager.update_config(body)
                    self._send_json({"ok": True, "state": manager.get_dashboard_state()})
                    return

                if parsed.path == "/api/browse-folder":
                    target_field = str(body.get("target") or "input_dir").strip()
                    if target_field == "custom_output_dir":
                        start_dir = manager.config.custom_output_dir or manager.config.input_dir
                        chosen = _native_browse_folder(start_dir)
                        if chosen:
                            manager.update_config({"output_mode": "custom_dir", "custom_output_dir": chosen})
                    else:
                        chosen = _native_browse_folder(manager.config.input_dir)
                        if chosen:
                            manager.update_config({"input_dir": chosen})
                    self._send_json({"ok": bool(chosen), "path": chosen, "state": manager.get_dashboard_state()})
                    return

                if parsed.path == "/api/browse-exe":
                    chosen = _native_browse_exe()
                    if chosen and not is_allowed_viewer_exe(chosen):
                        self._send_json({"error": "Please choose HighScorePlus.exe, HighScore.exe or DataViewer.exe"},
                                        status=HTTPStatus.BAD_REQUEST)
                        return
                    if chosen:
                        manager.update_config({"highscore_exe": chosen})
                    self._send_json({"ok": bool(chosen), "path": chosen, "state": manager.get_dashboard_state()})
                    return

                if parsed.path == "/api/generate-demo":
                    target_dir = manager.config.input_dir  # never a client-chosen folder
                    created = generate_demo_dataset(target_dir, include_sample_report=True)
                    manager._append_log("DEMO", f"Generated {len(created)} demo files in {target_dir}")
                    self._send_json(
                        {
                            "ok": True,
                            "created": [str(p) for p in created],
                            "state": manager.get_dashboard_state(),
                        }
                    )
                    return

                if parsed.path == "/api/convert-all":
                    subfolder_filter = str(body.get("subfolder") or "").strip()
                    res = manager.convert_all(only_new=False, subfolder_filter=subfolder_filter)
                    self._send_json({"ok": True, "result": res, "state": manager.get_dashboard_state()})
                    return

                if parsed.path == "/api/convert-one":
                    file_path = body.get("path")
                    if not file_path:
                        self._send_json({"error": "Missing 'path'"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    res = manager.convert_file(self._allowed_path(file_path), trigger_highscore=body.get("trigger_highscore"))
                    self._send_json({"ok": True, "result": res, "state": manager.get_dashboard_state()})
                    return

                if parsed.path == "/api/open-highscore":
                    xrdml_path = body.get("xrdml_path")
                    if not xrdml_path:
                        self._send_json({"error": "Missing 'xrdml_path'"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    res = launch_in_highscore(self._allowed_path(xrdml_path, suffix=".xrdml"), manager.config.highscore_exe)
                    manager._append_log("HIGHSCORE", res.get("message", ""))
                    self._send_json({**res, "state": manager.get_dashboard_state()})
                    return

                self._send_json({"error": f"Unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    return BridgeHandler


def create_server(
    manager: FolderBridgeManager | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> tuple[ThreadingHTTPServer, FolderBridgeManager]:
    """Instantiate ThreadingHTTPServer and FolderBridgeManager."""
    mgr = manager or FolderBridgeManager()
    token = secrets.token_urlsafe(32)  # new random token every launch
    handler_cls = build_handler(mgr, token)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.bridge_token = token  # type: ignore[attr-defined]
    return server, mgr


def _open_url(url: str) -> None:
    try:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
            return
    except OSError:
        pass
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="XRD Data Bridge - local XRD data format converter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    print("Starting XRD Data Bridge...", flush=True)
    url = f"http://{args.host}:{args.port}/"
    browser_url = f"{url}?v={int(time.time())}"
    try:
        server, mgr = create_server(host=args.host, port=args.port)
    except OSError as exc:
        print(f"Port {args.port} is already active ({exc}) — opening browser to {browser_url} ...", flush=True)
        if args.open_browser:
            _open_url(browser_url)
        return

    mgr.start_background_watcher()

    url = f"http://{args.host}:{server.server_address[1]}/"
    browser_url = f"{url}?v={int(time.time())}"
    print(f"XRD Data Bridge running at {url}", flush=True)
    print(f"Linked folder: {mgr.config.input_dir}", flush=True)
    print("Keep this window minimized while measuring. Press Ctrl+C to stop.", flush=True)

    if args.open_browser:
        threading.Timer(0.4, lambda: _open_url(browser_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down XRD Data Bridge...")
    finally:
        mgr.stop_background_watcher()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

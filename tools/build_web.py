"""Assemble the GitHub Pages site into ./_site (used by CI and for local testing).

The site serves the desktop dashboard with a browser transport built on the same conversion modules.

    python tools/build_web.py            # build ./_site
    python tools/build_web.py --serve    # build and serve on http://127.0.0.1:8800
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"


def build() -> Path:
    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(ROOT / "web", SITE)
    bridge = ROOT / "xrd_sdl_bridge"
    for name in ("converter.py", "watcher.py", "highscore_bridge.py", "sample_data_gen.py"):
        shutil.copy2(bridge / name, SITE / name)
    for path in (bridge / "highscore_scripts").iterdir():
        if path.suffix in {".md", ".pas"}:
            shutil.copy2(path, SITE / path.name)
    html = (bridge / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        '  <meta name="bridge-token" content="__BRIDGE_TOKEN__" />',
        f'  <script src="browser.js?v={int(time.time())}"></script>',  # new URL per build: no stale cache
    )
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    return SITE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    site = build()
    print(f"Built {site}")
    if args.serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
        with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
            print(f"Serving on http://127.0.0.1:{args.port}/")
            srv.serve_forever()


if __name__ == "__main__":
    main()

"""Assemble the GitHub Pages site into ./_site (used by CI and for local testing).

The online converter runs the *same* converter.py as the desktop bridge, copied in at build time,
so there is only one copy of the conversion code in the repository.

    python tools/build_web.py            # build ./_site
    python tools/build_web.py --serve    # build and serve on http://127.0.0.1:8800
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"


def build() -> Path:
    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(ROOT / "web", SITE)
    shutil.copy2(ROOT / "xrd_sdl_bridge" / "converter.py", SITE / "converter.py")
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

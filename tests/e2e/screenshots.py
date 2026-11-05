"""Screenshots of the running stack's UIs, for reviewing the look (CI uploads them).

    pip install playwright && python -m playwright install chromium
    python tests/e2e/screenshots.py <out dir> [--log-previews]

--log-previews also prints small JPEG previews (base64, in a collapsed CI
log group) for reviewers who can read job logs but not download artifacts.

Not part of the regression suites: it checks nothing, it only captures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

UI = os.environ.get("DATAHUB_FRONTEND", "http://localhost:9002")
API = os.environ.get("VEROMESH_API", "http://localhost:8700")
PAGES = [
    ("datahub-home", f"{UI}/"),
    ("datahub-search", f"{UI}/search?query=customer"),
    ("datahub-glossary", f"{UI}/glossary"),
    ("api-docs", f"{API}/docs"),
]


def log_previews(out: Path) -> None:
    import base64
    import io

    from PIL import Image

    print("::group::screenshot previews (base64 JPEG)")
    for f in sorted(out.glob("*.png")):
        im = Image.open(f).convert("RGB")
        im.thumbnail((1100, 700))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        print(f"PREVIEW-BEGIN {f.stem}")
        data = base64.b64encode(buf.getvalue()).decode()
        for i in range(0, len(data), 3000):
            print(data[i : i + 3000])
        print(f"PREVIEW-END {f.stem}")
    print("::endgroup::")


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(f"{UI}/login", wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=out / "datahub-login.png")
        r = page.request.post(f"{UI}/logIn", data={"username": "datahub", "password": "datahub"})
        print("login:", r.status)
        for name, url in PAGES:
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception as exc:  # keep going; a missing page is visible in the output
                print(f"{name}: {exc}")
            page.wait_for_timeout(3000)
            page.screenshot(path=out / f"{name}.png")
            print("captured", name)
        browser.close()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = Path(args[0] if args else ".build/test-results/screenshots")
    code = main(target)
    if "--log-previews" in sys.argv:
        log_previews(target)
    sys.exit(code)

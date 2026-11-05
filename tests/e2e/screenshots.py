"""Screenshots of the running stack's UIs, for reviewing the look (CI uploads them).

    pip install playwright && python -m playwright install chromium
    python tests/e2e/screenshots.py <out dir>

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
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".build/test-results/screenshots")))

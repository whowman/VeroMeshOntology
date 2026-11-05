"""License gate for the Python components (see docs/licensing.md).

Fails on GPL, AGPL, SSPL and Elastic License dependencies; reports LGPL and
unknown licenses for review. Writes a Markdown report to the given path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OWN = {"veromesh", "veromesh-connectors", "pip-licenses"}
FORBIDDEN = re.compile(
    r"(?<!Lesser )(?<!L)\bGPL|(?<!Lesser )General Public License|AGPL|Affero|SSPL|Server Side Public|Elastic License|ELv2",
    re.I,
)
REVIEW = re.compile(r"LGPL|Lesser General|UNKNOWN", re.I)
# Dual-licensed packages where we use the permissive option.
PERMISSIVE_ALTERNATIVE = re.compile(r"\b(MIT|BSD|Apache|ISC|PSF|MPL)\b", re.I)


def main(report: str) -> int:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls", "--from=mixed"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = sorted(
        (r for r in json.loads(out) if r["Name"].lower() not in OWN), key=lambda r: r["Name"].lower()
    )
    bad, review = [], []
    for r in rows:
        lic = r["License"]
        if FORBIDDEN.search(lic) and not (" OR " in lic.upper() and PERMISSIVE_ALTERNATIVE.search(lic)):
            bad.append(r)
        elif REVIEW.search(lic):
            review.append(r)
    lines = ["| Package | Version | License | URL |", "| --- | --- | --- | --- |"]
    lines += [f"| {r['Name']} | {r['Version']} | {r['License']} | {r.get('URL', '')} |" for r in rows]
    Path(report).parent.mkdir(parents=True, exist_ok=True)
    Path(report).write_text("\n".join(lines) + "\n")
    print(f"Report: {report}")
    for r in review:
        print(f"REVIEW {r['Name']} {r['Version']}: {r['License']}")
    for r in bad:
        print(f"FAIL   {r['Name']} {r['Version']}: {r['License']} (not allowed; see docs/licensing.md)")
    print(f"{len(rows)} packages checked: {len(bad)} not allowed, {len(review)} to review.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "python-licenses.md"))

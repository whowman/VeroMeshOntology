"""Print the third-party requirements of the Python components, one per line.

Used by the test runner image and the --local venv, so both install exactly
what the components declare (dependencies + dev extras). The components'
own code is never installed: tests run against the working tree.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

COMPONENTS = ("service", "connectors")


def requirements(root: Path) -> list[str]:
    out: list[str] = []
    for c in COMPONENTS:
        project = tomllib.loads((root / c / "pyproject.toml").read_text())["project"]
        for req in project["dependencies"] + project.get("optional-dependencies", {}).get("dev", []):
            if req not in out:
                out.append(req)
    return out


if __name__ == "__main__":
    print(
        "\n".join(
            requirements(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2])
        )
    )

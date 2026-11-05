"""Add VeroMesh files to the DataHub frontend's static assets.

The frontend serves /assets/* from public/assets/ on its classpath, inside
one of the jars in /datahub-frontend/lib. This finds that jar and writes a
copy with our files added under public/assets/:

    python add_assets.py <lib dir> <files dir> <out dir>

<files dir> mirrors the assets tree, e.g. conf/theme/veromesh.theme.json and
veromesh/logo.svg. Only the changed jar is written to <out dir>.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PREFIX = "public/assets/"


def find_assets_jar(lib: Path) -> Path:
    found = []
    for jar in sorted(lib.glob("*.jar")):
        with zipfile.ZipFile(jar) as z:
            if any(n.startswith(PREFIX) for n in z.namelist()):
                found.append(jar)
    if len(found) != 1:
        raise SystemExit(f"expected exactly one jar with {PREFIX}, found {[j.name for j in found]}")
    return found[0]


def add(jar: Path, files: Path, out: Path) -> list[str]:
    new = {PREFIX + p.relative_to(files).as_posix(): p for p in sorted(files.rglob("*")) if p.is_file()}
    out.mkdir(parents=True, exist_ok=True)
    target = out / jar.name
    with zipfile.ZipFile(jar) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename not in new:
                dst.writestr(info, src.read(info.filename))
        for name, path in new.items():
            dst.write(path, name)
    return sorted(new)


def main(argv: list[str]) -> int:
    lib, files, out = (Path(a) for a in argv)
    jar = find_assets_jar(lib)
    for name in add(jar, files, out):
        print(f"{jar.name}: + {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

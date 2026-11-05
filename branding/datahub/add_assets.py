"""Add VeroMesh files to the DataHub frontend's static assets.

The frontend serves /assets/* from public/assets/ on its classpath. Several
jars in /datahub-frontend/lib can hold public/assets/ entries (the React
build, and the frontend's own jar with the ingestion connector registry);
ours go into the React build's jar, the one that also has public/index.html.
This writes a copy of that jar with our files added under public/assets/:

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
    candidates = []
    for jar in sorted(lib.glob("*.jar")):
        with zipfile.ZipFile(jar) as z:
            names = z.namelist()
        assets = sum(n.startswith(PREFIX) for n in names)
        if assets:
            candidates.append(("public/index.html" in names, assets, jar))
    if not candidates:
        raise SystemExit(f"no jar in {lib} holds {PREFIX}")
    has_index = [c for c in candidates if c[0]]
    if len(has_index) > 1:
        raise SystemExit(f"several jars hold public/index.html: {[c[2].name for c in has_index]}")
    # The React build (has index.html); otherwise the jar with the most assets.
    best = has_index[0] if has_index else max(candidates, key=lambda c: c[1])
    print(f"assets jars: {', '.join(f'{c[2].name} ({c[1]})' for c in candidates)}; using {best[2].name}")
    return best[2]


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

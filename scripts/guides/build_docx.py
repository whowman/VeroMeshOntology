"""Build the Word copies of the guides from their Markdown sources.

    python scripts/guides/build_docx.py [--out .build/guides] [--version 0.4.0]

The Markdown in docs/guides/ is the source; the .docx files are generated, never
edited. Each is laid out on a VeroMesh reference document made here with
python-docx (VeroMesh colours from branding/palette.json, the mark in the header),
then converted by pandoc with a table of contents.

Needs pandoc (3.x) and python-docx.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDES = ROOT / "docs" / "guides"
PALETTE = ROOT / "branding" / "palette.json"
MARK = ROOT / "branding" / "logos" / "veromesh-mark-180.png"

# Used when the repository has no branding/ folder of its own (the test harness).
DEFAULT_PALETTE = {"ink": "#0D0F14", "tealDeep": "#2A9E75", "textMuted": "#555A6A", "text": "#E8EAF0"}
FONT = "Calibri"


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} is not installed")
    return path


def palette() -> dict[str, str]:
    if PALETTE.exists():
        return {k: v["hex"] for k, v in json.loads(PALETTE.read_text()).items()}
    return DEFAULT_PALETTE


def reference_docx(path: Path) -> None:
    """A pandoc reference document in VeroMesh's colours."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [tool("pandoc"), "-o", str(path), "--print-default-data-file", "reference.docx"]
    subprocess.run(cmd, check=True)  # noqa: S603
    doc = Document(str(path))
    pal = palette()

    def rgb(name: str) -> RGBColor:
        return RGBColor.from_string(pal[name].lstrip("#"))

    def style(name: str, size: float | None = None, colour: str | None = None, bold: bool | None = None):
        try:
            s = doc.styles[name]
        except KeyError:
            return
        s.font.name = FONT
        rpr = s.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            fonts.set(qn(attr), FONT)
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            fonts.attrib.pop(qn(attr), None)
        if size:
            s.font.size = Pt(size)
        if colour:
            s.font.color.rgb = rgb(colour)
        if bold is not None:
            s.font.bold = bold

    for body in ("Normal", "Body Text", "First Paragraph", "Compact", "Table"):
        style(body, 10.5, "ink")
    style("Title", 28, "ink", True)
    style("Subtitle", 14, "tealDeep", False)
    style("Author", 10.5, "textMuted")
    style("Date", 10.5, "textMuted")
    style("Heading 1", 18, "tealDeep", True)
    style("Heading 2", 14, "ink", True)
    style("Heading 3", 12, "tealDeep", True)
    style("Heading 4", 11, "ink", True)
    style("TOC Heading", 18, "tealDeep", True)
    style("Hyperlink", None, "tealDeep")
    style("Block Text", 10.5, "textMuted")
    style("Source Code", 9, "ink")
    style("Verbatim Char", 9, "ink")

    section = doc.sections[0]
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if MARK.exists():
        header.add_run().add_picture(str(MARK), height=Pt(18))
        header.add_run("  ")
    run = header.add_run("VeroMesh")
    run.font.name, run.font.size, run.font.bold = FONT, Pt(9), True
    run.font.color.rgb = rgb("tealDeep")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    # Page number field.
    r = footer.add_run()
    r.font.size, r.font.name = Pt(8), FONT
    r.font.color.rgb = rgb("textMuted")
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        r._r.append(el)
    doc.save(str(path))


def version_of() -> str:
    try:
        return subprocess.run(  # noqa: S603
            [tool("git"), "describe", "--tags", "--always", "--dirty"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, SystemExit):
        return "unreleased"


def build(out: Path, version: str) -> list[Path]:
    ref = out / "reference.docx"
    reference_docx(ref)
    built = []
    for md in sorted(GUIDES.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        target = out / (md.stem + ".docx")
        subprocess.run(  # noqa: S603 — fixed arguments; the paths are this repository's
            [
                tool("pandoc"),
                str(md),
                "--from",
                "gfm+yaml_metadata_block",
                "--to",
                "docx",
                "--reference-doc",
                str(ref),
                "--toc",
                "--toc-depth",
                "2",
                "--resource-path",
                str(GUIDES),
                "--metadata",
                f"subtitle=Version {version}",
                "--metadata",
                f"date={date.today().isoformat()}",
                "-o",
                str(target),
            ],
            check=True,
        )
        built.append(target)
    return built


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / ".build" / "guides"))
    ap.add_argument("--version", default=None, help="shown under the title (default: git describe)")
    args = ap.parse_args(argv)
    for path in build(Path(args.out), args.version or version_of()):
        print(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

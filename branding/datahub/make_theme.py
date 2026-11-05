"""Generate the VeroMesh theme for the DataHub UI.

DataHub (v1.8+) loads a JSON theme at runtime when GMS has
REACT_APP_CUSTOM_THEME_ID=<name>.json and the frontend serves
assets/conf/theme/<name>.json. A JSON theme replaces the colour tokens as a
whole, so this script starts from DataHub's own light theme at the pinned
version (src/conf/theme/colorThemes/{color,light}.ts and themeV2.ts) and
swaps in the VeroMesh palette. Every token DataHub defines stays defined,
and upgrading DataHub regenerates the theme from the new token set.

    python3 branding/datahub/make_theme.py <datahub source checkout> <out.json>

Standard library only (runs with the python3 that ships with macOS).
"""

from __future__ import annotations

import colorsys
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
THEME_ID = "veromesh.theme.json"
ASSETS = "assets/veromesh"

PALETTE_LINE = re.compile(r"^\s+(\w+): '(#[0-9A-Fa-f]{6})',?\s*$")
TOKEN_LINE = re.compile(r"^\s+(\w+): (?:colors\.(\w+)|'([^']*)'),\s*$")
STYLE_LINE = re.compile(r"^\s+'?([\w-]+)'?: '([^']*)',\s*$")
KEY_LIKE = re.compile(r"^\s+'?[\w-]+'?:")


class ThemeSourceError(Exception):
    """DataHub's theme files changed shape; the generator needs updating."""


# ---------------------------------------------------------------------------- colour maths


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """t=0 -> a, t=1 -> b."""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))  # noqa: B905 (Python 3.9)


def luminance(h: str) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def retint(h: str, toward: str) -> str:
    """Keep h's lightness; take hue and saturation from `toward`."""
    _, light, _ = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(h)))
    hue, _, sat = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(toward)))
    # Very light and very dark neutrals keep less tint, as DataHub's own ladder does.
    sat *= min(1.0, 4 * light * (1 - light) + 0.2)
    return rgb_to_hex(c * 255 for c in colorsys.hls_to_rgb(hue, light, sat))


# ---------------------------------------------------------------------------- parsing DataHub sources


def parse_palette(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = PALETTE_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).upper()
        elif KEY_LIKE.match(line):
            raise ThemeSourceError(f"color.ts: unexpected line {line.strip()!r}")
    if "violet600" not in out or "gray0" not in out:
        raise ThemeSourceError("color.ts: expected violet and gray scales")
    return out


def parse_tokens(text: str) -> dict[str, tuple[str, str]]:
    """token -> ('ref', paletteName) | ('lit', value)."""
    out = {}
    for line in text.splitlines():
        m = TOKEN_LINE.match(line)
        if m:
            out[m.group(1)] = ("ref", m.group(2)) if m.group(2) else ("lit", m.group(3))
        elif KEY_LIKE.match(line) and "ColorTheme" not in line:
            raise ThemeSourceError(f"light.ts: unexpected line {line.strip()!r}")
    if len(out) < 50:
        raise ThemeSourceError(f"light.ts: only {len(out)} tokens found")
    return out


def parse_styles(text: str) -> dict[str, str]:
    body = text.split("styles: {", 1)[1].split("},", 1)[0]
    out = {}
    for line in body.splitlines():
        m = STYLE_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
        elif KEY_LIKE.match(line):
            raise ThemeSourceError(f"themeV2.ts styles: unexpected line {line.strip()!r}")
    return out


# ---------------------------------------------------------------------------- the VeroMesh mapping


def load_palette(path: Path = HERE.parent / "palette.json") -> dict[str, str]:
    return {k: v["hex"].upper() for k, v in json.loads(path.read_text()).items()}


def brand_palette(base: dict[str, str], p: dict[str, str]) -> dict[str, str]:
    """DataHub's palette with the brand (violet) scale and the neutrals replaced."""
    out = dict(base)
    white = "#FFFFFF"
    brand = {
        "violet0": mix(white, p["deepBlue"], 0.05),  # selected rows, brand surfaces
        "violet100": mix(white, p["deepBlue"], 0.10),  # active background, avatar borders
        "violet200": mix(white, p["deepBlue"], 0.22),  # brand hover surfaces, tag borders
        "violet300": mix(white, p["purple"], 0.45),  # hover borders
        "violet400": p["purple"],  # focus, selected and input borders
        "violet500": p["purple"],  # brand, hover and selected icons
        "violet600": p["deepBlue"],  # buttons, links, brand text
        "violet700": p["navy"],  # active text
        "violet800": p["navy"],  # button hover
        "violet900": mix(p["navy"], "#000000", 0.15),
        "violet1000": mix(p["navy"], "#000000", 0.30),
        "violet1100": mix(p["navy"], "#000000", 0.45),
        "violet1200": mix(p["navy"], "#000000", 0.60),
    }
    for k, v in brand.items():
        if k in out:
            out[k] = v
    for k in [k for k in out if re.fullmatch(r"gray\d+", k)]:
        if k != "gray0":
            out[k] = retint(out[k], p["midGray"])
    out["gray200"] = p["lightGray"]
    out["gray600"] = p["midGray"]
    return out


RGBA_SWAPS = {  # DataHub's violet-tinted shadows and selections -> palette
    "83, 63, 209": "deepBlue",
    "108, 71, 255": "purple",
    "33, 23, 95": "navy",
    "9, 1, 61": "navy",
}


def swap_rgba(value: str, p: dict[str, str]) -> str:
    for rgb, name in RGBA_SWAPS.items():
        r, g, b = hex_to_rgb(p[name])
        value = value.replace(rgb, f"{r}, {g}, {b}")
    return value


def build_theme(src: Path, p: dict[str, str], *, api_docs_url: str = "http://localhost:8700/docs") -> dict:
    conf = src / "datahub-web-react/src/conf/theme"
    base = parse_palette((conf / "colorThemes/color.ts").read_text())
    tokens = parse_tokens((conf / "colorThemes/light.ts").read_text())
    styles = parse_styles((conf / "themeV2.ts").read_text())
    pal = brand_palette(base, p)

    colors = {}
    for token, (kind, value) in tokens.items():
        if kind == "ref":
            if value not in pal:
                raise ThemeSourceError(f"light.ts: {token} uses unknown palette colour {value}")
            colors[token] = pal[value]
        else:
            colors[token] = swap_rgba(value, p)

    navy_rgb = ", ".join(str(c) for c in hex_to_rgb(p["navy"]))
    styles.update(
        {
            "primary-color": p["deepBlue"],
            "primary-color-dark": p["navy"],
            "primary-color-light": mix("#FFFFFF", p["deepBlue"], 0.08),
            "layout-header-color": p["navy"],
            "heading-color": p["navy"],
            "border-color-base": p["lightGray"],
            "homepage-background-upper-fade": mix("#FFFFFF", p["lightGray"], 0.6),
            "homepage-background-lower-fade": "#FFFFFF",
            "homepage-text-color": p["navy"],
            "box-shadow-navbar-redesign": f"0 0 6px 0px rgba({navy_rgb}, 0.20)",
            "highlight-color": mix("#FFFFFF", p["purple"], 0.12),
            "highlight-border-color": p["teal"],
        }
    )
    return {
        "id": THEME_ID,
        "colors": colors,
        "styles": styles,
        "assets": {"logoUrl": f"{ASSETS}/veromesh-stacked.svg"},
        "content": {
            "title": "VeroMesh",
            "subtitle": "Organizational ontology on your data catalog",
            "search": {"searchbarMessage": "Find data, ontology terms, people and more"},
            "menu": {
                "items": [
                    {
                        "label": "VeroMesh API",
                        "path": api_docs_url,
                        "shouldOpenInNewTab": True,
                        "description": "Model API, changesets and connectors",
                    },
                    {
                        "label": "DataHub documentation",
                        "path": "https://docs.datahub.com",
                        "shouldOpenInNewTab": True,
                        "description": "The open-source catalog VeroMesh builds on",
                    },
                ]
            },
        },
    }


def check_theme(theme: dict) -> list[str]:
    """Readability checks (WCAG AA) on the tokens that carry text and controls."""
    c, problems = theme["colors"], []
    white = "#FFFFFF"
    rules = [
        ("text", c.get("bg", white), 7.0),
        ("textSecondary", c.get("bg", white), 4.5),
        ("textBrand", c.get("bg", white), 4.5),
        ("hyperlinks", c.get("bg", white), 4.5),
        ("textOnFillBrand", c.get("buttonFillBrand", "#000000"), 4.5),
        ("iconBrand", c.get("bg", white), 3.0),
    ]
    for token, against, minimum in rules:
        value = c.get(token)
        if value and value.startswith("#") and contrast(value, against) < minimum:
            problems.append(
                f"{token} {value} on {against}: contrast {contrast(value, against):.2f} < {minimum}"
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    theme = build_theme(Path(argv[0]), load_palette())
    problems = check_theme(theme)
    for p in problems:
        print(f"contrast: {p}", file=sys.stderr)
    if problems:
        return 1
    Path(argv[1]).parent.mkdir(parents=True, exist_ok=True)
    Path(argv[1]).write_text(json.dumps(theme, indent=2) + "\n")
    print(f"Wrote {argv[1]}: {len(theme['colors'])} colour tokens, {len(theme['styles'])} styles.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

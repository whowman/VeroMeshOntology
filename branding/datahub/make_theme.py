"""Generate the VeroMesh theme for the DataHub UI: dark, in the assessment's colours.

DataHub (v1.8+) loads a JSON theme at runtime when GMS has
REACT_APP_CUSTOM_THEME_ID=<name>.json and the frontend serves
assets/conf/theme/<name>.json. A JSON theme replaces the colour tokens as a
whole, so this script starts from DataHub's own dark theme at the pinned
version (src/conf/theme/colorThemes/{color,dark}.ts, themeV2.ts and
themeV2Dark.ts) and swaps in the VeroMesh palette: near-black surfaces, a
teal primary accent and a purple secondary one, as in the governance
assessment. Every token DataHub defines stays defined, and upgrading DataHub
regenerates the theme from the new token set. A custom theme takes precedence
over DataHub's light/dark switch, so the UI is always dark.

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
            raise ThemeSourceError(f"dark.ts: unexpected line {line.strip()!r}")
    if len(out) < 50:
        raise ThemeSourceError(f"dark.ts: only {len(out)} tokens found")
    return out


def parse_styles(text: str) -> dict[str, str]:
    body = text.split("styles: {", 1)[1].split("},", 1)[0]
    out = {}
    for line in body.splitlines():
        m = STYLE_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
        elif KEY_LIKE.match(line):
            raise ThemeSourceError(f"theme styles: unexpected line {line.strip()!r}")
    return out


# ---------------------------------------------------------------------------- the VeroMesh mapping


def load_palette(path: Path = HERE.parent / "palette.json") -> dict[str, str]:
    return {k: v["hex"].upper() for k, v in json.loads(path.read_text()).items()}


def brand_scale(p: dict[str, str]) -> dict[str, str]:
    """DataHub's violet brand scale, re-made in teal."""
    white, teal, deep = "#FFFFFF", p["teal"], p["tealDeep"]
    return {
        "violet0": mix(white, teal, 0.12),
        "violet100": mix(white, teal, 0.25),
        "violet200": mix(white, teal, 0.45),
        "violet300": mix(teal, white, 0.30),  # brand text, links, active text
        "violet400": teal,  # brand icons, hover
        "violet500": teal,  # primary buttons, active borders
        "violet600": deep,  # brand borders, hover borders
        "violet700": mix(deep, p["ink"], 0.20),
        "violet800": mix(deep, p["ink"], 0.35),
        "violet900": mix(deep, p["ink"], 0.50),
        "violet1000": mix(p["raised"], teal, 0.18),  # active, secondary hover
        "violet1100": mix(p["surface"], teal, 0.12),  # selected rows
        "violet1200": mix(p["ink"], teal, 0.08),  # brand surfaces
    }


def brand_palette(base: dict[str, str], p: dict[str, str]) -> dict[str, str]:
    """DataHub's palette with the brand (violet) scale made teal and the neutrals made the assessment's."""
    out = dict(base)
    for k, v in brand_scale(p).items():
        if k in out:
            out[k] = v
    for k in [k for k in out if re.fullmatch(r"gray\d+", k)]:
        if k != "gray0":
            out[k] = retint(out[k], p["textSecondary"])
    # The dark ladder, darkest first: page, cards, raised, borders; then muted, secondary, primary text.
    neutrals = {
        "gray1200": p["ink"],
        "gray1100": p["surface"],
        "gray1000": p["raised"],
        "gray900": p["line"],
        "gray700": p["textMuted"],  # disabled fills, scrollbars
        "gray600": mix(p["textSecondary"], p["textMuted"], 0.6),  # icons, disabled text
        "gray500": mix(p["textSecondary"], p["textMuted"], 0.3),  # tertiary text, placeholders
        "gray400": p["textSecondary"],  # secondary text
        "gray300": p["text"],  # body text
    }
    for k, v in neutrals.items():
        if k in out:
            out[k] = v
    return out


RGBA_SWAPS = {  # DataHub's violet-tinted shadows and selections -> palette
    "83, 63, 209": "teal",
    "108, 71, 255": "teal",
    "33, 23, 95": "ink",
    "9, 1, 61": "ink",
}


def swap_rgba(value: str, p: dict[str, str]) -> str:
    for rgb, name in RGBA_SWAPS.items():
        r, g, b = hex_to_rgb(p[name])
        value = value.replace(rgb, f"{r}, {g}, {b}")
    return value


HEX = re.compile(r"#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?\b")


def is_violet(h: str) -> bool:
    """DataHub's brand hue: the violet family (hue 230-275 degrees, clearly saturated)."""
    hue, _, sat = colorsys.rgb_to_hls(*(c / 255 for c in hex_to_rgb(h)))
    return 230 <= hue * 360 <= 275 and sat >= 0.35


def swap_hex(value: str, base: dict[str, str], brand: dict[str, str]) -> str:
    """Replace violet hex colours inside literal tokens (gradients, chart and nav colours).

    An exact DataHub palette colour becomes its brand counterpart; any other
    violet becomes the brand shade closest in luminance. Alpha suffixes stay.
    """
    exact = {base[k]: brand[k] for k in base if k.startswith("violet") and k in brand}
    scale = sorted({brand[k] for k in brand if k.startswith("violet")}, key=luminance)

    def sub(m: re.Match) -> str:
        rgb = "#" + m.group(1).upper()
        if rgb in exact:
            new = exact[rgb]
        elif is_violet(rgb):
            new = min(scale, key=lambda c: abs(luminance(c) - luminance(rgb)))
        else:
            return m.group(0)
        return new + (m.group(2) or "")

    return HEX.sub(sub, value)


def build_theme(src: Path, p: dict[str, str], *, api_docs_url: str = "http://localhost:8700/docs") -> dict:
    conf = src / "datahub-web-react/src/conf/theme"
    base = parse_palette((conf / "colorThemes/color.ts").read_text())
    tokens = parse_tokens((conf / "colorThemes/dark.ts").read_text())
    styles = parse_styles((conf / "themeV2.ts").read_text())
    styles.update(parse_styles((conf / "themeV2Dark.ts").read_text()))
    pal = brand_palette(base, p)
    shade = brand_scale(p)

    colors = {}
    for token, (kind, value) in tokens.items():
        if kind == "ref":
            if value not in pal:
                raise ThemeSourceError(f"dark.ts: {token} uses unknown palette colour {value}")
            colors[token] = pal[value]
        else:
            colors[token] = swap_hex(swap_rgba(value, p), base, pal)

    # No colour DataHub uses for its own violet brand may survive anywhere.
    datahub_violets = {v for k, v in base.items() if k.startswith("violet")} | {
        "#" + m.group(1).upper()
        for kind, value in tokens.values()
        if kind == "lit"
        for m in HEX.finditer(value)
        if is_violet("#" + m.group(1))
    }
    leftover = sorted(
        t for t, v in colors.items() if {"#" + m.group(1).upper() for m in HEX.finditer(v)} & datahub_violets
    )
    if leftover:
        raise ThemeSourceError(f"DataHub violet left in: {', '.join(leftover)}")

    # Roles the scale alone cannot express, as the assessment draws them.
    overrides = {
        "textOnFillBrand": p["onTeal"],  # dark text on teal buttons; white on teal is unreadable
        "iconOnFillBrand": p["onTeal"],
        "buttonSurfaceBrandHover": p["tealHover"],
        "borderBrandFocused": p["purple"],  # focus rings in the secondary accent
        "borderInputFocus": p["purple"],
        "tagsBrandBg": shade["violet1000"],
        "tagsBrandBorder": p["tealDeep"],
        "tagsBrandText": shade["violet300"],
    }
    for token, value in overrides.items():
        if token in colors:
            colors[token] = value

    styles.update(
        {
            "primary-color": p["teal"],
            "primary-color-dark": p["tealDeep"],
            "primary-color-light": shade["violet1100"],
            "layout-header-color": p["text"],
            "layout-header-background": p["surface"],
            "layout-body-background": p["ink"],
            "component-background": p["surface"],
            "body-background": p["ink"],
            "text-color": p["text"],
            "text-color-secondary": p["textSecondary"],
            "heading-color": p["text"],
            "border-color-base": p["line"],
            "divider-color": p["line"],
            "background-color-light": p["raised"],
            "disabled-color": p["textMuted"],
            "steps-nav-arrow-color": p["textSecondary"],
            "homepage-background-upper-fade": p["surface"],
            "homepage-background-lower-fade": p["ink"],
            "homepage-text-color": p["text"],
            "highlight-color": mix(p["ink"], p["purple"], 0.25),
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
    rules = []
    for surface in ("bg", "bgSurface"):  # content cards and the page around them
        ground = c.get(surface, "#000000")
        rules += [
            ("text", ground, 7.0),
            ("textSecondary", ground, 4.5),
            ("textBrand", ground, 4.5),
            ("hyperlinks", ground, 4.5),
            ("iconBrand", ground, 3.0),
        ]
    rules += [
        ("textOnFillBrand", c.get("buttonFillBrand", "#FFFFFF"), 4.5),
        ("textOnFillBrand", c.get("buttonSurfaceBrandHover", "#FFFFFF"), 4.5),
        ("tagsBrandText", c.get("tagsBrandBg", "#000000"), 4.5),
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

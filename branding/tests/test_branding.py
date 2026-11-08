"""The DataHub theme generator and the asset injector."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "branding" / "datahub"))

import add_assets  # noqa: E402
import make_theme as mt  # noqa: E402

COLOR_TS = """
// palette
export default {
    gray0: '#FFFFFF',
    gray300: '#D1D1D9',
    gray400: '#BBBBC5',
    gray600: '#888893',
    gray800: '#42414B',
    gray900: '#3C3B44',
    gray1100: '#24232B',
    gray1200: '#1C1B23',
    violet0: '#F1F3FD',
    violet300: '#B0A7EA',
    violet400: '#8C7EE0',
    violet500: '#705EE4',
    violet600: '#533FD1',
    violet1000: '#32267D',
};
"""
DARK_TS = (
    "const darkTheme: ColorTheme = {\n"
    + "\n".join(
        [
            "    bg: colors.gray1100,",
            "    bgSurface: colors.gray1200,",
            "    text: colors.gray300,",
            "    textSecondary: colors.gray400,",
            "    icon: colors.gray600,",
            "    // a comment line is ignored",
            "    textBrand: colors.violet300,",
            "    hyperlinks: colors.violet300,",
            "    buttonFillBrand: colors.violet500,",
            "    buttonSurfaceBrandHover: colors.violet600,",
            "    textOnFillBrand: colors.gray0,",
            "    iconBrand: colors.violet400,",
            "    borderInputFocus: colors.violet400,",
            "    border: colors.gray900,",
            "    tagsBrandBg: colors.violet600,",
            "    tagsBrandText: colors.violet0,",
            "    bgSelectedSubtle: 'rgba(108, 71, 255, 0.08)',",
            "    brandGradient: 'linear-gradient(180deg, #8C7EE0 20%, #5340cc 80%)',",
            "    bgHighlight: '#4a3d00',",
        ]
        + [f"    filler{i}: colors.gray900," for i in range(50)]
    )
    + "\n};\n"
)
THEME_V2_TS = """
const themeV2: Theme = {
    id: 'themeV2',
    colors: light,
    styles: {
        'primary-color': '#533FD1',
        'body-background': 'white',
        'highlight-border-color': '#07878180',
        'border-radius-navbar-redesign': '12px',
    },
    assets: {
        logoUrl: 'assets/logo.png',
    },
};
"""
THEME_V2_DARK_TS = """
const themeV2Dark: Theme = {
    ...themeV2,
    id: 'themeV2Dark',
    colors: dark,
    styles: {
        ...themeV2.styles,
        'body-background': '#171B2B',
        'box-shadow': '0px 0px 30px 0px rgba(0, 0, 0, 0.3)',
    },
};
"""


@pytest.fixture
def datahub_src(tmp_path) -> Path:
    conf = tmp_path / "datahub-web-react/src/conf/theme"
    (conf / "colorThemes").mkdir(parents=True)
    (conf / "colorThemes/color.ts").write_text(COLOR_TS)
    (conf / "colorThemes/dark.ts").write_text(DARK_TS)
    (conf / "themeV2.ts").write_text(THEME_V2_TS)
    (conf / "themeV2Dark.ts").write_text(THEME_V2_DARK_TS)
    return tmp_path


def test_palette_file_has_the_assessment_colours():
    """The default palette is the governance assessment's (VeroMeshAssessment static/css/app.css)."""
    p = mt.load_palette()
    assert {k: p[k] for k in ("ink", "surface", "raised", "text", "textSecondary", "textMuted")} == {
        "ink": "#0D0F14",
        "surface": "#13161E",
        "raised": "#1A1E28",
        "text": "#E8EAF0",
        "textSecondary": "#8B90A0",
        "textMuted": "#555A6A",
    }
    assert (p["teal"], p["tealHover"], p["tealDeep"], p["onTeal"]) == (
        "#4ECBA0",
        "#5DDCB0",
        "#2A9E75",
        "#061A10",
    )
    assert (p["purple"], p["warn"], p["danger"]) == ("#7B72E9", "#E8A84A", "#E8604A")


def test_theme_is_dark_in_the_assessment_colours(datahub_src):
    p = mt.load_palette()
    theme = mt.build_theme(datahub_src, p)
    c, st = theme["colors"], theme["styles"]
    assert theme["id"] == "veromesh.theme.json"
    assert c["bgSurface"] == p["ink"] and c["bg"] == p["surface"]
    assert c["text"] == p["text"] and c["textSecondary"] == p["textSecondary"]
    assert c["border"] == p["line"]
    assert c["buttonFillBrand"] == c["iconBrand"] == p["teal"]
    assert c["textOnFillBrand"] == p["onTeal"], "teal buttons carry dark text, as in the assessment"
    assert c["buttonSurfaceBrandHover"] == p["tealHover"]
    assert c["borderInputFocus"] == p["purple"]
    assert c["hyperlinks"] == c["textBrand"] != p["purple"]
    assert c["bgSelectedSubtle"] == "rgba(78, 203, 160, 0.08)"
    # violet inside literal tokens: exact palette colour -> its brand shade; other violets -> nearest shade
    assert c["brandGradient"].startswith(f"linear-gradient(180deg, {p['teal']} 20%, #")
    assert c["bgHighlight"] == "#4a3d00"  # not violet: untouched
    assert st["primary-color"] == p["teal"]
    assert st["body-background"] == p["ink"] and st["component-background"] == p["surface"]
    assert st["box-shadow"] == "0px 0px 30px 0px rgba(0, 0, 0, 0.3)"  # DataHub's dark style kept
    assert st["border-radius-navbar-redesign"] == "12px"  # light style inherited by dark kept
    assert theme["assets"]["logoUrl"] == "assets/veromesh/veromesh-stacked.svg"
    assert theme["content"]["title"] == "VeroMesh"
    assert set(c) == set(mt.parse_tokens(DARK_TS)), "every DataHub token must be defined"
    assert mt.check_theme(theme) == []


def test_neutrals_take_the_brand_tint_and_keep_their_lightness():
    tinted = mt.retint("#42414B", "#8B90A0")
    assert tinted != "#42414B"
    assert abs(mt.luminance(tinted) - mt.luminance("#42414B")) < 0.01


def test_contrast_check_catches_unreadable_text(datahub_src):
    theme = mt.build_theme(datahub_src, mt.load_palette())
    assert mt.check_theme(theme) == []
    theme["colors"]["hyperlinks"] = "#2A2F3B"
    assert any("hyperlinks" in p for p in mt.check_theme(theme))


def test_unexpected_source_shapes_fail_loudly():
    with pytest.raises(mt.ThemeSourceError):
        mt.parse_tokens(DARK_TS.replace("colors.gray1100,", "colors.gray1100 ?? 'x',", 1))
    with pytest.raises(mt.ThemeSourceError):
        mt.parse_palette(COLOR_TS.replace("'#FFFFFF'", "white", 1))


def test_main_writes_theme(datahub_src, tmp_path):
    out = tmp_path / "out" / "veromesh.theme.json"
    assert mt.main([str(datahub_src), str(out)]) == 0
    assert json.loads(out.read_text())["id"] == "veromesh.theme.json"


def test_against_the_pinned_datahub_source_if_present():
    """When the DataHub checkout exists (dev-up, build tier), the real token files must parse cleanly."""
    versions = dict(
        line.split("=", 1) for line in (ROOT / "deploy/versions.env").read_text().splitlines() if "=" in line
    )
    src = ROOT / ".build" / f"datahub-{versions['DATAHUB_VERSION']}"
    if not (src / "datahub-web-react").is_dir():
        pytest.skip("no DataHub checkout in .build/")
    theme = mt.build_theme(src, mt.load_palette())
    assert len(theme["colors"]) > 100 and mt.check_theme(theme) == []


def test_assets_are_added_to_the_jar_that_serves_them(tmp_path):
    lib, files, out = tmp_path / "lib", tmp_path / "files", tmp_path / "out"
    lib.mkdir()
    (files / "conf/theme").mkdir(parents=True)
    (files / "conf/theme/veromesh.theme.json").write_text("{}")
    with zipfile.ZipFile(lib / "other.jar", "w") as z:
        z.writestr("com/Example.class", b"x")
    with zipfile.ZipFile(lib / "datahub-frontend.jar", "w") as z:  # the frontend's own jar
        z.writestr("public/assets/ingestion/connector_registry/a.json", b"{}")
        z.writestr("public/assets/ingestion/connector_registry/b.json", b"{}")
        z.writestr("public/assets/ingestion/connector_registry/c.json", b"{}")
    with zipfile.ZipFile(lib / "frontend-assets.jar", "w") as z:  # the React build
        z.writestr("public/index.html", b"<html>")
        z.writestr("public/assets/logo.png", b"png")
        z.writestr("public/assets/conf/theme/veromesh.theme.json", b"old")
    assert add_assets.main([str(lib), str(files), str(out)]) == 0
    assert [p.name for p in out.iterdir()] == ["frontend-assets.jar"]
    z = zipfile.ZipFile(out / "frontend-assets.jar")
    assert z.read("public/assets/conf/theme/veromesh.theme.json") == b"{}"
    assert z.read("public/assets/logo.png") == b"png"
    assert len(z.namelist()) == 3


def test_datahub_settings_point_at_shipped_files():
    env = dict(
        line.split("=", 1)
        for line in (ROOT / "branding/datahub/datahub.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert env["REACT_APP_CUSTOM_THEME_ID"] == mt.THEME_ID
    for key in ("REACT_APP_LOGO_URL", "REACT_APP_FAVICON_URL"):
        name = env[key].removeprefix(mt.ASSETS + "/")
        assert (ROOT / "branding/logos" / name).is_file(), env[key]

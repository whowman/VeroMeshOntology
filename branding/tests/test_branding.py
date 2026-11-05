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
    gray200: '#ECECF0',
    gray600: '#888893',
    gray700: '#666672',
    gray800: '#42414B',
    violet0: '#F1F3FD',
    violet400: '#8C7EE0',
    violet500: '#705EE4',
    violet600: '#533FD1',
    violet800: '#4232A7',
};
"""
LIGHT_TS = (
    "const lightTheme: ColorTheme = {\n"
    + "\n".join(
        [
            "    bg: colors.gray0,",
            "    text: colors.gray800,",
            "    textSecondary: colors.gray700,",
            "    icon: colors.gray600,",
            "    textBrand: colors.violet600,",
            "    hyperlinks: colors.violet600,",
            "    buttonFillBrand: colors.violet600,",
            "    textOnFillBrand: colors.gray0,",
            "    iconBrand: colors.violet500,",
            "    border: colors.gray200,",
            "    bgSelectedSubtle: 'rgba(83, 63, 209, 0.04)',",
        ]
        + [f"    filler{i}: colors.gray200," for i in range(50)]
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
    },
    assets: {
        logoUrl: 'assets/logo.png',
    },
};
"""


@pytest.fixture
def datahub_src(tmp_path) -> Path:
    conf = tmp_path / "datahub-web-react/src/conf/theme"
    (conf / "colorThemes").mkdir(parents=True)
    (conf / "colorThemes/color.ts").write_text(COLOR_TS)
    (conf / "colorThemes/light.ts").write_text(LIGHT_TS)
    (conf / "themeV2.ts").write_text(THEME_V2_TS)
    return tmp_path


def test_palette_file_has_the_brand_colours():
    p = mt.load_palette()
    assert p == {
        "navy": "#1A2A44",
        "deepBlue": "#23395D",
        "purple": "#7B72E9",
        "teal": "#4ECBA0",
        "lightGray": "#E8EAF0",
        "midGray": "#8B90A0",
    }


def test_theme_maps_brand_roles(datahub_src):
    p = mt.load_palette()
    theme = mt.build_theme(datahub_src, p)
    c = theme["colors"]
    assert theme["id"] == "veromesh.theme.json"
    assert c["buttonFillBrand"] == c["hyperlinks"] == p["deepBlue"]
    assert c["iconBrand"] == p["purple"]
    assert c["border"] == p["lightGray"]
    assert c["icon"] == p["midGray"]
    assert c["bgSelectedSubtle"] == "rgba(35, 57, 93, 0.04)"
    assert theme["styles"]["primary-color"] == p["deepBlue"]
    assert theme["styles"]["highlight-border-color"] == p["teal"]
    assert theme["styles"]["body-background"] == "white"  # untouched DataHub style kept
    assert theme["assets"]["logoUrl"] == "assets/veromesh/veromesh-stacked.svg"
    assert theme["content"]["title"] == "VeroMesh"
    assert set(c) == set(mt.parse_tokens(LIGHT_TS)), "every DataHub token must be defined"


def test_neutrals_take_the_brand_tint_and_keep_their_lightness():
    tinted = mt.retint("#42414B", "#8B90A0")
    assert tinted != "#42414B"
    assert abs(mt.luminance(tinted) - mt.luminance("#42414B")) < 0.01


def test_contrast_check_catches_unreadable_text(datahub_src):
    theme = mt.build_theme(datahub_src, mt.load_palette())
    assert mt.check_theme(theme) == []
    theme["colors"]["hyperlinks"] = "#C8CCD8"
    assert any("hyperlinks" in p for p in mt.check_theme(theme))


def test_unexpected_source_shapes_fail_loudly():
    with pytest.raises(mt.ThemeSourceError):
        mt.parse_tokens(LIGHT_TS.replace("colors.gray0,", "colors.gray0 ?? 'x',", 1))
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

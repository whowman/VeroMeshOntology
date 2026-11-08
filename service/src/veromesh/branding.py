"""VeroMesh look for the API's own pages: interactive docs, ReDoc, favicon.

Logos and the palette come from the repository's branding/ folder (the
Docker image copies it to /app/branding). Without it the docs still work,
with FastAPI's default look.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

DEFAULT_PALETTE = {  # mirrors branding/palette.json (the assessment's colours)
    "ink": "#0D0F14",
    "raised": "#1A1E28",
    "text": "#E8EAF0",
    "textSecondary": "#8B90A0",
    "teal": "#4ECBA0",
    "tealDeep": "#2A9E75",
    "purple": "#7B72E9",
}


def palette(brand_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((brand_dir / "palette.json").read_text())
        return {**DEFAULT_PALETTE, **{k: v["hex"] for k, v in data.items()}}
    except (OSError, ValueError, KeyError, TypeError):
        return dict(DEFAULT_PALETTE)


def docs_css(p: dict[str, str]) -> str:
    return f"""
:root {{
  --vm-ink: {p["ink"]}; --vm-raised: {p["raised"]}; --vm-purple: {p["purple"]};
  --vm-teal: {p["teal"]}; --vm-teal-deep: {p["tealDeep"]}; --vm-light: {p["text"]}; --vm-mid: {p["textSecondary"]};
}}
body {{ margin: 0; background: #fff; color: var(--vm-ink); }}
.vm-header {{
  display: flex; align-items: center; gap: 16px; padding: 14px 24px;
  border-bottom: 1px solid var(--vm-light); background: #fff;
  box-shadow: inset 0 -3px 0 0 var(--vm-ink);
}}
.vm-header img {{ height: 34px; width: auto; display: block; }}
.vm-header .vm-sub {{ font: 500 14px/1.3 system-ui, sans-serif; color: var(--vm-mid);
  border-left: 1px solid var(--vm-light); padding-left: 16px; }}
.vm-header nav {{ margin-left: auto; display: flex; gap: 18px; }}
.vm-header nav a {{ font: 500 14px system-ui, sans-serif; color: var(--vm-raised); text-decoration: none; }}
.vm-header nav a:hover {{ color: var(--vm-purple); }}
.swagger-ui, .swagger-ui .info .title, .swagger-ui .opblock-tag,
.swagger-ui .opblock .opblock-summary-path, .swagger-ui table thead tr th,
.swagger-ui .parameter__name, .swagger-ui .model-title {{ color: var(--vm-ink); }}
.swagger-ui .info .title small.version-stamp {{ background: var(--vm-teal); }}
.swagger-ui .info a, .swagger-ui a.nostyle {{ color: var(--vm-raised); }}
.swagger-ui .opblock-tag {{ border-bottom-color: var(--vm-light); }}
.swagger-ui .opblock {{ border-radius: 6px; box-shadow: none; }}
.swagger-ui .opblock.opblock-get {{ border-color: var(--vm-raised); background: rgba(26, 30, 40, .04); }}
.swagger-ui .opblock.opblock-get .opblock-summary-method {{ background: var(--vm-raised); }}
.swagger-ui .opblock.opblock-get .opblock-summary {{ border-color: var(--vm-raised); }}
.swagger-ui .opblock.opblock-post {{ border-color: var(--vm-teal); background: rgba(78, 203, 160, .06); }}
.swagger-ui .opblock.opblock-post .opblock-summary-method {{ background: var(--vm-teal-deep); }}
.swagger-ui .opblock.opblock-post .opblock-summary {{ border-color: var(--vm-teal); }}
.swagger-ui .opblock.opblock-put {{ border-color: var(--vm-purple); background: rgba(123, 114, 233, .06); }}
.swagger-ui .opblock.opblock-put .opblock-summary-method {{ background: #5C53C9; }}
.swagger-ui .opblock.opblock-put .opblock-summary {{ border-color: var(--vm-purple); }}
.swagger-ui .btn.execute {{ background: var(--vm-raised); border-color: var(--vm-raised); }}
.swagger-ui .btn.execute:hover {{ background: var(--vm-ink); }}
.swagger-ui .btn.authorize {{ color: var(--vm-raised); border-color: var(--vm-teal); }}
.swagger-ui .btn.authorize svg {{ fill: var(--vm-raised); }}
.swagger-ui input[type=text]:focus, .swagger-ui textarea:focus, .swagger-ui select:focus {{
  outline: 2px solid var(--vm-purple); outline-offset: 1px; }}
.swagger-ui .scheme-container {{ box-shadow: none; border-bottom: 1px solid var(--vm-light); }}
"""


def header_html(p: dict[str, str], has_logo: bool) -> str:
    logo = (
        '<img src="/brand/logos/veromesh-landscape.svg" alt="VeroMesh">'
        if has_logo
        else f'<strong style="font:700 22px system-ui;color:{p["ink"]}">VeroMesh</strong>'
    )
    return (
        f'<header class="vm-header">{logo}<span class="vm-sub">Ontology API</span>'
        '<nav><a href="/docs">Interactive docs</a><a href="/redoc">Reference</a>'
        '<a href="/openapi.json">OpenAPI</a></nav></header>'
    )


def install(app: FastAPI, brand_dir: Path) -> None:
    """Serve /brand, /favicon.ico, and branded /docs and /redoc."""
    has_assets = (brand_dir / "logos").is_dir()
    p = palette(brand_dir)
    favicon = "/brand/logos/favicon.ico" if has_assets else None
    if has_assets:
        app.mount("/brand/logos", StaticFiles(directory=brand_dir / "logos"), name="brand")

        @app.get("/favicon.ico", include_in_schema=False)
        def _favicon() -> FileResponse:
            return FileResponse(brand_dir / "logos" / "favicon.ico", media_type="image/x-icon")

    head = f"<style>{docs_css(p)}</style>"
    header = header_html(p, has_assets)

    @app.get("/docs", include_in_schema=False)
    def _docs() -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - docs",
            swagger_favicon_url=favicon or "https://fastapi.tiangolo.com/img/favicon.png",
            swagger_ui_parameters={"defaultModelsExpandDepth": -1, "docExpansion": "list", "filter": True},
        )
        html = (
            page.body.decode().replace("</head>", head + "</head>", 1).replace("<body>", "<body>" + header, 1)
        )
        return HTMLResponse(html)

    @app.get("/redoc", include_in_schema=False)
    def _redoc() -> HTMLResponse:
        page = get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - reference",
            redoc_favicon_url=favicon or "https://fastapi.tiangolo.com/img/favicon.png",
        )
        html = (
            page.body.decode().replace("</head>", head + "</head>", 1).replace("<body>", "<body>" + header, 1)
        )
        return HTMLResponse(html)

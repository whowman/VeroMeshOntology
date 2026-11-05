# Branding

The VeroMesh look for the UIs in the stack.

| File | What |
| --- | --- |
| `palette.json` | The brand colours and their roles; the single source for both UIs |
| `logos/veromesh-landscape.svg` | Full logo, horizontal: API docs header |
| `logos/veromesh-stacked.svg` | Full logo, stacked: DataHub login card |
| `logos/veromesh-mark.svg` | The mark alone, square (derived from the stacked logo): DataHub navigation bar |
| `logos/favicon.ico`, `logos/veromesh-mark-180.png` | Favicon (16-64 px) and a 180 px mark, rendered from the mark |
| `datahub/` | DataHub theme generator and the branded frontend image |

## Palette

| Colour | Hex | Used for |
| --- | --- | --- |
| Primary dark navy | `#1A2A44` | hover and active states, headings, the API docs header rule |
| Deep blue | `#23395D` | primary buttons, links, brand text, GET operations |
| Purple accent | `#7B72E9` | brand icons, focus and selected borders, PUT operations |
| Soft teal accent | `#4ECBA0` | highlight edges, version badge, POST operations |
| Light gray | `#E8EAF0` | borders, subtle backgrounds |
| Mid gray | `#8B90A0` | secondary icons; every neutral gray is tinted toward it |

Text and controls are checked against WCAG AA when the theme is generated (body text 7:1, secondary text, links and button labels 4.5:1, brand icons 3:1). A palette change that breaks this fails the build.

## DataHub

DataHub itself is not modified. Three of its own extension points are used:

1. **Theme.** DataHub loads a JSON theme when GMS has `REACT_APP_CUSTOM_THEME_ID=<name>.json`. `datahub/make_theme.py` writes `veromesh.theme.json` from DataHub's own light theme at the pinned version (`colorThemes/color.ts`, `light.ts`, `themeV2.ts`) with the palette swapped in, so every colour token DataHub defines is present and an upgrade regenerates the theme from the new token set. If DataHub changes the shape of those files, generation stops with an error rather than producing a partial theme.
2. **Assets.** The frontend serves `/assets/*` from a jar. `datahub/build.sh` builds `veromesh/datahub-frontend-react:<version>`: the official image plus our theme and logos added to that jar (`datahub/add_assets.py`), nothing else.
3. **Settings.** `datahub/gms.env` selects the theme, logo, favicon and browser title. `scripts/dev-up.sh` passes it with DataHub's `DATAHUB_LOCAL_GMS_ENV` hook and selects the image with `DATAHUB_FRONTEND_IMAGE`.

`SKIP_BRANDING=1 scripts/dev-up.sh` starts the stock DataHub look instead.

The branded image is still DataHub under the Apache License 2.0; its license and notice files are unchanged inside it.

## VeroMesh API docs

`/docs` and `/redoc` on the VeroMesh API use the landscape logo, the palette and the favicon (`service/src/veromesh/branding.py`). The service image carries `palette.json` and `logos/`.

## Tests

`scripts/test.sh branding` (generator and injector), `scripts/test.sh datahub-frontend` (image builds with the assets in place), and the `e2e` suite checks that the running DataHub serves the theme, logos and favicon. CI also saves screenshots of the DataHub and API docs pages with each end-to-end run.

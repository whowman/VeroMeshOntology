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

The default VeroMesh palette is the governance assessment's (VeroMeshAssessment, `static/css/app.css`): near-black surfaces, a teal primary accent and a purple secondary one. Decks, the architecture pages and these UIs all use it.

| Colour | Hex | Used for |
| --- | --- | --- |
| Ink | `#0D0F14` | page background; text on the light API docs pages |
| Surface | `#13161E` | cards, panels, navigation and header |
| Raised | `#1A1E28` | hover and selected rows; buttons and GET operations on the API docs |
| Line | `#32343B` | borders and dividers (the assessment's white at 13% over the surface) |
| Text | `#E8EAF0` | primary text on dark surfaces; borders on the API docs |
| Secondary text | `#8B90A0` | secondary text and icons; every neutral gray is tinted toward it |
| Muted text | `#555A6A` | disabled text, scrollbars |
| Teal | `#4ECBA0` | primary buttons, links, brand icons, POST operations |
| Teal hover / deep | `#5DDCB0` / `#2A9E75` | button hover / brand borders, pressed states |
| On teal | `#061A10` | text on teal fills (white on teal is unreadable) |
| Purple | `#7B72E9` | focus rings, secondary links, PUT operations |
| Warning / danger | `#E8A84A` / `#E8604A` | warnings / errors |

Text and controls are checked against WCAG AA when the theme is generated, on both the content and the page surfaces (body text 7:1, secondary text, links, button labels and brand tags 4.5:1, brand icons 3:1). A palette change that breaks this fails the build.

## DataHub

DataHub itself is not modified. Three of its own extension points are used:

1. **Theme.** DataHub loads a JSON theme when GMS has `REACT_APP_CUSTOM_THEME_ID=<name>.json`. `datahub/make_theme.py` writes `veromesh.theme.json` from DataHub's own dark theme at the pinned version (`colorThemes/color.ts`, `dark.ts`, `themeV2.ts`, `themeV2Dark.ts`) with the palette swapped in, so every colour token DataHub defines is present and an upgrade regenerates the theme from the new token set. A custom theme takes precedence over DataHub's own light/dark switch, so the UI is always dark. If DataHub changes the shape of those files, generation stops with an error rather than producing a partial theme.
2. **Assets.** The frontend serves `/assets/*` from a jar. `datahub/build.sh` builds `veromesh/datahub-frontend-react:<version>`: the official image plus our theme and logos added to that jar (`datahub/add_assets.py`), nothing else.
3. **Settings.** `datahub/datahub.env` selects the theme, logo, favicon and browser title. `scripts/dev-up.sh` passes it to GMS and the frontend with DataHub's `DATAHUB_LOCAL_GMS_ENV` and `DATAHUB_LOCAL_FRONTEND_ENV` hooks (GMS hands the settings to the UI; the frontend serves the favicon itself), and selects the image with `DATAHUB_FRONTEND_IMAGE`.

`SKIP_BRANDING=1 scripts/dev-up.sh` starts the stock DataHub look instead.

The branded image is still DataHub under the Apache License 2.0; its license and notice files are unchanged inside it.

## VeroMesh API docs

`/docs` and `/redoc` on the VeroMesh API use the landscape logo, the palette and the favicon (`service/src/veromesh/branding.py`). The service image carries `palette.json` and `logos/`.

## Tests

`scripts/test.sh branding` (generator and injector), `scripts/test.sh datahub-frontend` (image builds with the assets in place), and the `e2e` suite checks that the running DataHub serves the theme, logos and favicon. CI also saves screenshots of the DataHub and API docs pages with each end-to-end run.

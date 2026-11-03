# Licensing checklist

Veromesh is proprietary. It builds on DataHub, which is Apache-2.0. Apache-2.0 allows commercial use, modification and closed-source distribution, with the conditions below. This is an engineering checklist, not legal advice; have counsel review before the first customer release.

## Rules we follow

1. **Don't fork DataHub.** Run upstream images unmodified and extend through the custom-model plugin (`datahub-model/`) and APIs. Upgrades stay cheap and we never have to mark modified DataHub files.
2. **If we ever patch DataHub source**, every modified file must carry a prominent notice that we changed it, and the change list goes in `third_party/datahub/CHANGES.md`.
3. **Ship notices.** Every self-hosted bundle and container image includes `LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.md` and `third_party/` (the service Dockerfile already copies them to `/app/licenses/`).
4. **Trademark.** Never use "DataHub" in the product name, logo or domain. Factual statements ("built on the open source DataHub project") are fine.
5. **Patent clause.** Apache-2.0's patent license terminates for anyone who sues claiming DataHub infringes a patent. Flag any patent action involving DataHub to counsel first.
6. **Approved-license policy.** Allowed without review: Apache-2.0, MIT, BSD, ISC, PostgreSQL, PSF, MPL-2.0 (file-level copyleft, unmodified use). Needs review: LGPL, EPL. Not allowed in shipped artifacts: GPL, AGPL, SSPL, Elastic License, BSL, Commons Clause.

## SaaS vs self-hosted

- **SaaS:** Apache-2.0 and permissive licenses impose no obligations when software isn't distributed. AGPL would; that's one reason it's blocked.
- **Self-hosted:** distributing DataHub images or charts triggers the notice requirements above.

## Before each release

- [ ] `deploy/versions.env` bumps reviewed, and DataHub's LICENSE/NOTICE refreshed in `third_party/datahub/` from the new tag
- [ ] CI license report reviewed for new or changed licenses
- [ ] Container images scanned (e.g. `syft` / `trivy --scanners license`) for OS-level packages
- [ ] `THIRD_PARTY_LICENSES.md` updated

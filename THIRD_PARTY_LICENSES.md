# Third-party components

Components Veromesh ships or runs, and their licenses. Keep this file current: CI regenerates the Python part (`service-licenses.md` build artifact) on every run. Versions are pinned in `deploy/versions.env` and `service/pyproject.toml`.

## Platform (containers in the self-hosted bundle)

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| DataHub (GMS, frontend, actions, upgrade) | v1.8.0 | Apache-2.0 | Unmodified upstream images. LICENSE + NOTICE in `third_party/datahub/` |
| OpenSearch | per DataHub profile (2.x) | Apache-2.0 | Search, graph and timeseries index for DataHub |
| PostgreSQL | per DataHub profile | PostgreSQL License | DataHub primary store (`quickstart-postgres` profile) |
| Oxigraph server | 0.5.13 | MIT OR Apache-2.0 | RDF / SPARQL store |

Deliberately **not** used, because of their licenses: MySQL (GPL-2.0), Elasticsearch (SSPL / Elastic License / AGPL), Neo4j Community (GPL-3.0). DataHub's `quickstart-postgres` profile uses pgQueue instead of Kafka. If Kafka is added later, it is Apache-2.0 and fine to ship.

## Veromesh service (Python)

| Package | License |
| --- | --- |
| acryl-datahub | Apache-2.0 |
| pyoxigraph | MIT OR Apache-2.0 |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| pydantic / pydantic-settings | MIT |

Transitive dependencies are listed in the CI artifact. Review any new GPL, AGPL, LGPL, SSPL or "source-available" license before release.

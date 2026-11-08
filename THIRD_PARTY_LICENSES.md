# Third-party components

Components Veromesh ships or runs, and their licenses. Keep this file current: CI regenerates the Python part (`service-licenses.md` build artifact) on every run. Versions are pinned in `deploy/versions.env` and `service/pyproject.toml`.

## Platform (containers in the self-hosted bundle)

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| DataHub (GMS, frontend, actions, upgrade) | v1.8.0 | Apache-2.0 | Unmodified upstream images. LICENSE + NOTICE in `third_party/datahub/` |
| OpenSearch | per DataHub profile (2.x) | Apache-2.0 | Search, graph and timeseries index for DataHub |
| PostgreSQL | per DataHub profile | PostgreSQL License | DataHub primary store (`quickstart-postgres` profile) |
| Apache Jena Fuseki | 6.2.0 | Apache-2.0 | RDF / SPARQL store; image built from the official jar (`deploy/fuseki/`). Jena LICENSE + NOTICE in `third_party/jena/` and in the image at `/opt/fuseki/licenses` |
| Eclipse Temurin JRE 21 (Fuseki base image) | 21 | GPL-2.0 with Classpath Exception | Unmodified runtime; the Classpath Exception permits running Apache-licensed code on it without affecting its license |
| Fuseki-Kafka (`io.telicent.jena:jena-fmod-kafka`, Telicent) | 3.4.3 | Apache-2.0 | Fuseki module in the Fuseki image; read replicas apply the writer's changes from Kafka. Bundles the Apache Kafka client (Apache-2.0) and Telicent Smart Caches Core (Apache-2.0). LICENSE + NOTICE in `third_party/fuseki-kafka/` and in the image at `/opt/fuseki/licenses/fuseki-kafka` |
| Apache Kafka | per DataHub prerequisites chart (Bitnami image) | Apache-2.0 | Message broker: DataHub's events on Kubernetes, and the knowledge graph's change log (writer to replicas) |

Deliberately **not** used, because of their licenses: MySQL (GPL-2.0), Elasticsearch (SSPL / Elastic License / AGPL), Neo4j Community (GPL-3.0). DataHub's `quickstart-postgres` profile uses pgQueue instead of Kafka locally; on Kubernetes, Kafka (Apache-2.0) is used, by DataHub and for Fuseki replication.

## Veromesh service (Python)

| Package | License |
| --- | --- |
| acryl-datahub | Apache-2.0 |
| pyoxigraph (embedded store for tests and the `memory` mode) | MIT OR Apache-2.0 |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| pydantic / pydantic-settings | MIT |
| rdflib | BSD-3-Clause |
| pyshacl | Apache-2.0 |

## Connectors (Python)

| Package | License |
| --- | --- |
| openpyxl | MIT |
| httpx | BSD-3-Clause |
| PyYAML | MIT |

Transitive dependencies are listed in the CI artifact. Review any new GPL, AGPL, LGPL, SSPL or "source-available" license before release.

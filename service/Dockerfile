# Build context: repository root (see deploy/docker-compose.veromesh.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VEROMESH_ONTOLOGY_DIR=/app/ontology VEROMESH_BRANDING_DIR=/app/branding
WORKDIR /app

COPY service/pyproject.toml service/README.md ./
COPY service/src ./src
RUN pip install --no-cache-dir .

# Ship license notices for everything bundled in this image.
COPY LICENSE NOTICE THIRD_PARTY_LICENSES.md /app/licenses/
COPY third_party /app/licenses/third_party
COPY ontology /app/ontology
COPY branding/palette.json /app/branding/palette.json
COPY branding/logos /app/branding/logos

RUN useradd --system --uid 10001 veromesh
USER veromesh

EXPOSE 8700
CMD ["uvicorn", "veromesh.api:app", "--host", "0.0.0.0", "--port", "8700"]

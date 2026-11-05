# DataHub's frontend, unmodified, plus VeroMesh static assets: the theme JSON
# and logos. Built by branding/datahub/build.sh; the build context is
# prepared by that script (assets/ mirrors the frontend's public/assets/).
ARG DATAHUB_VERSION
FROM acryldata/datahub-frontend-react:${DATAHUB_VERSION} AS upstream

FROM python:3.12-slim AS patch
COPY --from=upstream /datahub-frontend/lib /lib-in
COPY add_assets.py /add_assets.py
COPY assets /assets
RUN python /add_assets.py /lib-in /assets /out

FROM acryldata/datahub-frontend-react:${DATAHUB_VERSION}
COPY --from=patch --chown=datahub:datahub /out/ /datahub-frontend/lib/
LABEL org.opencontainers.image.title="VeroMesh-branded DataHub frontend" \
      org.opencontainers.image.description="acryldata/datahub-frontend-react with the VeroMesh theme and logos added as static assets" \
      org.opencontainers.image.licenses="Apache-2.0"

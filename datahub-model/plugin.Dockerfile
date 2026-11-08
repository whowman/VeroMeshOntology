# The VeroMesh custom DataHub model as an image, for Kubernetes: an init
# container copies /plugins/models into GMS's plugin folder
# (deploy/helm/datahub/datahub-values.yaml). Built by datahub-model/build_image.sh
# from the zip that datahub-model/build.sh produces.
FROM debian:12-slim
ARG REGISTRY_ID
ARG MODEL_VERSION
COPY plugin/ /plugins/models/${REGISTRY_ID}/${MODEL_VERSION}/
COPY licenses/ /licenses/
RUN chmod -R a+rX /plugins /licenses
USER 65534

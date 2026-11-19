# VeroMesh backups (deploy/backup/veromesh-backup.sh): the knowledge graph and
# the decision log, encrypted with the customer's key, to a volume or object storage.
#
# Alpine's packages, all under permissive licenses except BusyBox (GPL-2.0,
# the base image's shell and tools, run unmodified):
#   age (BSD-3-Clause)          encryption to the customer's public keys
#   rclone (MIT)                object storage: Azure Blob, S3, Google Cloud Storage and more
#   postgresql17-client         pg_dump, pg_restore, psql (PostgreSQL License); dumps
#                               servers up to PostgreSQL 17
#   curl (curl), jq (MIT)       Fuseki's Graph Store Protocol; the manifest
FROM alpine:3.22

RUN apk add --no-cache age rclone postgresql17-client curl jq ca-certificates tzdata \
 && adduser -D -H -u 10003 -s /sbin/nologin backup \
 && mkdir -p /work /backups && chown backup /work /backups

COPY veromesh-backup.sh /usr/local/bin/veromesh-backup
COPY LICENSE NOTICE /licenses/
RUN chmod 0755 /usr/local/bin/veromesh-backup && veromesh-backup 2>&1 | grep -q usage

USER backup
WORKDIR /work
ENV HOME=/tmp XDG_CACHE_HOME=/tmp WORK_DIR=/work BACKUP_TARGET=/backups
ENTRYPOINT ["/usr/local/bin/veromesh-backup"]
CMD ["backup"]

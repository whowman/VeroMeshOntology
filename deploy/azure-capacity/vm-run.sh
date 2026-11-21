#!/usr/bin/env bash
# Runs on a capacity test machine (Ubuntu 24.04), as root, started by the
# capacity-azure workflow through Azure's run command. The workflow puts these
# settings in front of this file:
#
#   REF          the commit to test
#   MODE         bench (time each size) or grow (load step after step until the machine runs out)
#   SIZES        bench: sizes to run in order, e.g. "medium large" or "xl" (500,000 people)
#   THROUGHPUT   bench: concurrencies for checks per second, e.g. 1,4,16
#   HEAP         Fuseki's Java heap, e.g. 16g, or "auto" (a quarter of the machine's memory)
#   LOCAL_DATA   1: keep Docker's data (Fuseki's and PostgreSQL's storage) on the machine's
#                local NVMe drive (a size with a "d", e.g. D4ds v6) instead of its network disk
#
# Everything the steps print goes to /var/log/veromesh-capacity.log on the machine. Azure
# returns only the end of what a run command prints, so this prints only the results:
# the machine, then each size's report. On a failure it prints the end of the log.
set -euo pipefail

LOG=/var/log/veromesh-capacity.log
WORK=/opt/veromesh-capacity
REPO=https://github.com/whowman/VeroMeshOntology.git
: "${REF:?}" "${MODE:=bench}" "${SIZES:=medium large}" "${THROUGHPUT:=1,4,16}" "${HEAP:=auto}" "${LOCAL_DATA:=0}"

fail() {
  echo "FAILED at: $1"
  echo "--- the end of $LOG:"
  tail -c 3000 "$LOG"
  exit 1
}
step() { echo "[$(date -u +%H:%M:%S)] $*" >>"$LOG"; }

{
  step "machine"
  nproc
  free -g
  lscpu | grep -E "Model name"
} >>"$LOG" 2>&1

mem_gb=$(free -g | awk '/^Mem:/ {print $2}')
if [ "$HEAP" = auto ]; then HEAP="$((mem_gb / 4))g"; fi

step "packages"
{
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q docker.io git python3-venv python3-pip curl
  systemctl enable --now docker
} >>"$LOG" 2>&1 || fail "installing Docker, git and Python"

DATA_ON="network disk (OS disk)"
if [ "$LOCAL_DATA" = 1 ]; then
  step "local NVMe drive"
  {
    # v6 machines present their local NVMe drive raw and unformatted; managed (network)
    # disks show as "MSFT NVMe Accelerator", the local one as "Microsoft NVMe Direct Disk".
    lsblk -dnpo NAME,MODEL,SIZE,MOUNTPOINT
    dev=$(lsblk -dnpo NAME,MODEL | awk '/Direct Disk/ {print $1; exit}')
    [ -n "$dev" ]
    mnt=$(lsblk -nro MOUNTPOINT "$dev" | awk 'NF {print; exit}')
    if [ -z "$mnt" ]; then
      mkfs.ext4 -F -q "$dev"
      mnt=/mnt/local
      mkdir -p "$mnt"
      mount -o noatime,discard "$dev" "$mnt"
    fi
    systemctl stop docker docker.socket
    mkdir -p "$mnt/docker"
    printf '{"data-root": "%s/docker"}\n' "$mnt" > /etc/docker/daemon.json
    systemctl start docker
    docker info --format '{{.DockerRootDir}}'
  } >>"$LOG" 2>&1 || fail "putting Docker's data on the local NVMe drive (does this size have one?)"
  DATA_ON="local NVMe drive ($(lsblk -dnpo NAME,MODEL,SIZE "$(lsblk -dnpo NAME,MODEL | awk '/Direct Disk/ {print $1; exit}')" | tr -s ' '))"
fi

step "checkout $REF"
{
  rm -rf "$WORK" && mkdir -p "$WORK" && cd "$WORK"
  git init -q && git remote add origin "$REPO"
  git fetch -q --depth 1 origin "$REF" && git checkout -q FETCH_HEAD
} >>"$LOG" 2>&1 || fail "fetching $REF"
cd "$WORK"
# shellcheck disable=SC1091
source deploy/versions.env

step "Fuseki (heap $HEAP)"
{
  docker rm -f fuseki postgres 2>/dev/null || true
  docker build -q --build-arg JENA_VERSION="$JENA_VERSION" \
    --build-arg FUSEKI_KAFKA_VERSION="$FUSEKI_KAFKA_VERSION" -t veromesh/fuseki:capacity deploy/fuseki
  docker run -d --name fuseki -p 127.0.0.1:3030:3030 \
    -e JAVA_OPTS="-Xmx$HEAP -Xlog:gc:stdout:uptime" veromesh/fuseki:capacity
  for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' fuseki)" = healthy ] && break
    sleep 2
  done
  [ "$(docker inspect -f '{{.State.Health.Status}}' fuseki)" = healthy ]
} >>"$LOG" 2>&1 || fail "starting Fuseki"

step "PostgreSQL"
{
  docker run -d --name postgres -p 127.0.0.1:5432:5432 -e POSTGRES_USER=veromesh \
    -e POSTGRES_PASSWORD=test-only -e POSTGRES_DB=decisions "$POSTGRES_TEST_IMAGE"
  for _ in $(seq 60); do
    docker exec postgres pg_isready -U veromesh -d decisions && break
    sleep 2
  done
  docker exec postgres pg_isready -U veromesh -d decisions
} >>"$LOG" 2>&1 || fail "starting PostgreSQL"

step "the service"
{
  python3 -m venv /opt/venv
  /opt/venv/bin/pip install -q ./service
} >>"$LOG" 2>&1 || fail "installing the service"
PY=/opt/venv/bin/python
STORE=fuseki:http://localhost:3030/veromesh
DLOG=postgresql://veromesh:test-only@localhost:5432/decisions
mkdir -p results

echo "Machine: $(nproc) vCPU, ${mem_gb} GB, $(lscpu | sed -n 's/^Model name: *//p'); Fuseki heap $HEAP; data on $DATA_ON; commit $REF"
if [ "$MODE" = grow ]; then
  step "grow"
  stop=$((mem_gb * 3 / 5))  # the service may take 60% of memory; Fuseki and the system the rest
  $PY tests/perf/capacity.py --step 20 --people 1000 --store "$STORE" --log "$DLOG" \
    --stop-rss-gb "$stop" --out results/grow.json >results/grow.txt 2>>"$LOG" || fail "grow"
  tail -c 3500 results/grow.txt
else
  for size in $SIZES; do
    step "bench $size"
    $PY tests/perf/bench.py --size "$size" --store "$STORE" --log "$DLOG" --clear \
      --throughput "$THROUGHPUT" --edits-every 1 --out "results/bench-$size.json" \
      >"results/bench-$size.txt" 2>>"$LOG" || fail "bench $size"
    echo
    echo "=== $size"
    cat "results/bench-$size.txt"
  done
fi
echo
docker logs fuseki 2>&1 | grep -E "GC\([0-9]+\) Pause" | grep -oE "[0-9.]+ms$" | tr -d ms |
  awk '{ n++; t += $1; if ($1 > max) max = $1 } END { printf "Fuseki GC pauses: %d, longest %.0f ms, total %.1f s\n", n, max, t / 1000 }'
step "done"

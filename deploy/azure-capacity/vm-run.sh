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
#   STORAGE      where Docker's data (Fuseki's and PostgreSQL's storage) lives: os-disk (the
#                machine's network-attached OS disk), local-nvme (its local NVMe drive, on a size
#                with a "d", e.g. D4ds v6) or premium-ssd-v2 (an attached Premium SSD v2 disk)
#   COPIES       bench: copies of the service the agents are spread over (default 1)
#   FUSEKI       single (default) or replicated: a writer and two read replicas through Kafka
#                (tests/perf/replicated_fuseki.sh, each replica with half the writer's heap),
#                and after each bench the replication measurements (tests/perf/replication.py)
#
# Everything the steps print goes to /var/log/veromesh-capacity.log on the machine. Azure
# returns only the end of what a run command prints, so this prints only the results:
# the machine, then each size's report. On a failure it prints the end of the log.
set -euo pipefail

LOG=/var/log/veromesh-capacity.log
WORK=/opt/veromesh-capacity
REPO=https://github.com/whowman/VeroMeshOntology.git
: "${REF:?}" "${MODE:=bench}" "${SIZES:=medium large}" "${THROUGHPUT:=1,4,16}" "${HEAP:=auto}" "${STORAGE:=os-disk}" "${COPIES:=1}" "${FUSEKI:=single}"

fail() {
  echo "FAILED at: $1"
  echo "--- disk space:"
  df -h / "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" 2>/dev/null
  echo "--- the end of $LOG:"
  tail -c 3000 "$LOG"
  exit 1
}
step() { echo "[$(date -u +%H:%M:%S)] $*" >>"$LOG"; }
# Any command that fails outside a "|| fail" stops the run with the end of the log, so a
# failure is never silent (Azure would return only "Failed").
set -E
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR

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

# Docker's data on a disk of its own: v6 machines present disks through NVMe. The local
# drive shows as "Microsoft NVMe Direct Disk" and arrives raw; managed (network) disks,
# the OS disk and an attached data disk, show as "MSFT NVMe Accelerator".
data_device() {
  case "$STORAGE" in
    local-nvme) lsblk -dnpo NAME,MODEL | awk '/Direct Disk/ {print $1; exit}' ;;
    premium-ssd-v2)  # the attached disk: not the local drive, no partitions, nothing mounted
      local d
      for d in $(lsblk -dnpo NAME,TYPE | awk '$2 == "disk" {print $1}'); do
        lsblk -dno MODEL "$d" | grep -q "Direct Disk" && continue
        [ "$(lsblk -nro NAME "$d" | wc -l)" = 1 ] || continue
        [ -z "$(lsblk -nro MOUNTPOINT "$d" | tr -d '[:space:]')" ] || continue
        echo "$d"
        return
      done ;;
  esac
}
DATA_ON="network disk (the OS disk, premium SSD)"
if [ "$STORAGE" != os-disk ]; then
  step "storage: $STORAGE"
  (
    lsblk -dnpo NAME,MODEL,SIZE,TYPE,MOUNTPOINT
    dev=""
    for _ in $(seq 60); do dev=$(data_device); [ -n "$dev" ] && break; sleep 2; done  # an attached disk can take a moment
    [ -n "$dev" ] || { echo "no disk found for $STORAGE"; exit 1; }
    echo "using $dev"
    # Each step checked: "set -e" does not apply in a block whose failure "|| fail" handles.
    mnt=$(lsblk -nro MOUNTPOINT "$dev" | awk 'NF {print; exit}')
    if [ -z "$mnt" ]; then
      mkfs.ext4 -F -q "$dev" || exit 1
      mnt=/mnt/data
      mkdir -p "$mnt" || exit 1
      mount -o noatime,discard "$dev" "$mnt" || exit 1
    fi
    mountpoint -q "$mnt" || { echo "$mnt is not mounted"; exit 1; }
    systemctl stop docker docker.socket || exit 1
    mkdir -p "$mnt/docker" || exit 1
    printf '{"data-root": "%s/docker"}\n' "$mnt" > /etc/docker/daemon.json || exit 1
    systemctl start docker || exit 1
    docker info --format '{{.DockerRootDir}}' | grep -q "$mnt/docker" || { echo "Docker did not move to $mnt"; exit 1; }
    echo "$dev" > /run/veromesh-data-device
  ) >>"$LOG" 2>&1 || fail "putting Docker's data on the $STORAGE disk (does this machine have one?)"
  dev=$(cat /run/veromesh-data-device)
  DATA_ON="$STORAGE ($(lsblk -dnpo NAME,MODEL,SIZE "$dev" | tr -s ' '))"
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

step "Fuseki (heap $HEAP, $FUSEKI)"
{
  docker rm -f fuseki postgres 2>/dev/null || true
  docker build -q --build-arg JENA_VERSION="$JENA_VERSION" \
    --build-arg FUSEKI_KAFKA_VERSION="$FUSEKI_KAFKA_VERSION" -t veromesh/fuseki:capacity deploy/fuseki
  if [ "$FUSEKI" = replicated ]; then
    replica_heap="$(( ${HEAP%g} / 2 ))g"  # HEAP is in gigabytes (e.g. 16g)
    env HEAP="$HEAP" REPLICA_HEAP="$replica_heap" tests/perf/replicated_fuseki.sh start 2
  else
  docker run -d --name fuseki -p 127.0.0.1:3030:3030 \
    -e JAVA_OPTS="-Xmx$HEAP -Xlog:gc:stdout:uptime" veromesh/fuseki:capacity
  for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' fuseki)" = healthy ] && break
    sleep 2
  done
  [ "$(docker inspect -f '{{.State.Health.Status}}' fuseki)" = healthy ]
  fi
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
    fallback=()
    [ "$FUSEKI" = replicated ] && fallback=(--read-fallback http://localhost:3031/veromesh)
    $PY tests/perf/bench.py --size "$size" --store "$STORE" --log "$DLOG" --clear "${fallback[@]}" \
      --throughput "$THROUGHPUT" --edits-every 1 --copies "$COPIES" --out "results/bench-$size.json" \
      >"results/bench-$size.txt" 2>>"$LOG" || fail "bench $size"
    echo
    echo "=== $size"
    cat "results/bench-$size.txt"
    if [ "$FUSEKI" = replicated ]; then
      step "replication $size"
      $PY tests/perf/replication.py --writer http://localhost:3030/veromesh \
        --replicas http://localhost:3031/veromesh,http://localhost:3032/veromesh \
        --log "$DLOG" --out "results/replication-$size.json" \
        >"results/replication-$size.txt" 2>>"$LOG" || echo "(replication $size: a check did not pass)"
      cat "results/replication-$size.txt"
    fi
  done
fi
echo
docker logs fuseki 2>&1 | grep -E "GC\([0-9]+\) Pause" | grep -oE "[0-9.]+ms$" | tr -d ms |
  awk '{ n++; t += $1; if ($1 > max) max = $1 } END { printf "Fuseki GC pauses: %d, longest %.0f ms, total %.1f s\n", n, max, t / 1000 }'
df -h "$(docker info -f '{{.DockerRootDir}}')" | tail -1 | awk '{ print "Disk used: " $3 " of " $2 }'
step "done"

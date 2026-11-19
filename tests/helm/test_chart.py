"""The VeroMesh Helm chart and the DataHub overlays: lint, render and check.

Run with ``scripts/test.sh helm`` (the test-runner image has Helm and
kubeconform) or ``scripts/test.sh helm --local`` with Helm on PATH.

Checks are about what an operator relies on: the services find each other,
the knowledge graph is reachable only by the service, every container runs
unprivileged, values are validated, and the pinned versions agree with the
rest of the repository.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "veromesh"
CI = CHART / "ci"
DATAHUB_OVERLAYS = ROOT / "deploy" / "helm" / "datahub"
CACHE = ROOT / ".build" / "helm-charts"
DATAHUB_REPO = "https://helm.datahubproject.io"
KUBE_VERSION = "1.30.0"

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="Helm is not on PATH")

# Every install must name the customer's backup key (backup.encryption.recipients)
# and where backups go. "Defaults" below means the chart's defaults plus this test
# key, whose private key nobody holds, and a volume claim.
TEST_KEY = "age129qez9lp8qakwpaapv7af6ncqzw0g8a5lz4k9elc6njem29j0afs2v8mx9"
WITH_KEY = (
    "--set",
    f"backup.encryption.recipients={{{TEST_KEY}}}",
    "--set",
    "backup.volume.existingClaim=veromesh-backups",
)


def versions() -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (ROOT / "deploy" / "versions.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


def project_version(component: str) -> str:
    return re.search(r'^version = "(.+)"', (ROOT / component / "pyproject.toml").read_text(), re.M)[1]


def helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["helm", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        pytest.fail(f"helm {' '.join(args)} failed:\n{r.stderr}{r.stdout}")
    return r


def render(
    *values: str, sets: tuple[str, ...] = (), chart: Path | str = CHART, name: str = "veromesh"
) -> list[dict]:
    args = ["template", name, str(chart), "--namespace", "veromesh", "--kube-version", KUBE_VERSION]
    if not values and chart == CHART:
        args += WITH_KEY
    for v in values:
        args += ["-f", str(v if Path(v).is_absolute() else CI / v)]
    for s in sets:
        args += ["--set", s]
    out = helm(*args).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def one(docs: list[dict], kind: str, name: str | None = None) -> dict:
    found = [d for d in by_kind(docs, kind) if name is None or d["metadata"]["name"] == name]
    assert len(found) == 1, (
        f"expected one {kind} {name or ''}, found {[d['metadata']['name'] for d in found]}"
    )
    return found[0]


def fuseki_cm(docs: list[dict]) -> dict:
    """The Fuseki configuration (writer.ttl, replica.ttl)."""
    (cm,) = [d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"].endswith("-fuseki-config")]
    return cm


def env_of(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def selects(selector: dict, labels: dict) -> bool:
    """Whether a Kubernetes label selector (matchLabels + matchExpressions) matches these labels."""
    if not selector.get("matchLabels", {}).items() <= labels.items():
        return False
    for e in selector.get("matchExpressions", []):
        if e["operator"] == "In" and labels.get(e["key"]) not in e["values"]:
            return False
        if e["operator"] == "NotIn" and labels.get(e["key"]) in e["values"]:
            return False
    return True


def pod_labels(workload: dict) -> dict:
    return workload["spec"]["template"]["metadata"]["labels"]


WRITER = "veromesh-fuseki"
REPLICA = "veromesh-fuseki-replica"


def pod_specs(docs: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet"):
            out.append((f"{d['kind']}/{d['metadata']['name']}", d["spec"]["template"]["spec"]))
        elif d["kind"] == "CronJob":
            out.append(
                (f"CronJob/{d['metadata']['name']}", d["spec"]["jobTemplate"]["spec"]["template"]["spec"])
            )
        elif d["kind"] == "Pod":
            out.append((f"Pod/{d['metadata']['name']}", d["spec"]))
    return out


VARIANTS = sorted(p.name for p in CI.glob("*.yaml"))


# ---------------------------------------------------------------------------- lint and schema


@pytest.mark.parametrize("variant", ["(defaults)", *VARIANTS])
def test_lint(variant):
    args = ["lint", str(CHART), "--strict", "--kube-version", KUBE_VERSION]
    args += WITH_KEY if variant == "(defaults)" else ["-f", str(CI / variant)]
    helm(*args)


@pytest.mark.parametrize(
    "bad",
    [
        "api.replicas=2",  # typo: replicaCount
        "fuseki.dataset=bad/name",
        "fuseki.persistence.size=lots",
        "api.image.pullPolicy=Sometimes",
        "store.url=http://no-scheme-prefix:3030/ds",
        "api.datahub.gmsUrl=datahub:8080",
        "fuseki.replication.replicas=0",
        "fuseki.replication.kafka.bootstrapServers=kafka",
        "fuseki.replication.kafka.topic=bad topic",
        "fuseki.replication.kafka.replicationFactor=-1",
        "api.auth.issuer=http://login.example.com",  # an issuer must be https
        "api.auth.enabled=true",  # typo: the switch is api.auth.disabled
    ],
)
def test_schema_rejects_bad_values(bad):
    r = helm("template", "veromesh", str(CHART), "--set", bad, check=False)
    assert r.returncode != 0, f"--set {bad} was accepted"
    assert "values don't meet the specifications of the schema" in r.stderr, r.stderr


def test_external_store_is_required_without_fuseki():
    r = helm("template", "veromesh", str(CHART), "--set", "fuseki.enabled=false", check=False)
    assert r.returncode != 0 and "store.url is required" in r.stderr, r.stderr


# ---------------------------------------------------------------------------- default install


def test_default_install_resources():
    docs = render()
    kinds = sorted(f"{d['kind']}/{d['metadata']['name']}" for d in docs)
    assert kinds == [
        "ConfigMap/veromesh-backup-recipients",
        "ConfigMap/veromesh-fuseki-config",
        "CronJob/veromesh-backup",
        "Deployment/veromesh-api",
        "NetworkPolicy/veromesh-fuseki",
        "Pod/veromesh-test",
        "PodDisruptionBudget/veromesh-fuseki-replica",
        "Service/veromesh-api",
        "Service/veromesh-fuseki",
        "Service/veromesh-fuseki-headless",
        "Service/veromesh-fuseki-replica-headless",
        "Service/veromesh-fuseki-replicas",
        "ServiceAccount/veromesh",
        "ServiceAccount/veromesh-backup",
        "StatefulSet/veromesh-fuseki",
        "StatefulSet/veromesh-fuseki-replica",
    ], kinds


def test_service_finds_fuseki_and_datahub():
    docs = render()
    api = one(docs, "Deployment")["spec"]["template"]["spec"]
    env = env_of(api["containers"][0])
    fuseki_svc = one(docs, "Service", "veromesh-fuseki")
    assert env["VEROMESH_RDF_STORE"]["value"] == "fuseki:http://veromesh-fuseki:3030/veromesh"
    assert fuseki_svc["spec"]["ports"][0]["port"] == 3030
    # Writes go to the writer; reads fall back to the replicas while it is down.
    assert env["VEROMESH_RDF_STORE_READ_FALLBACK"]["value"] == "http://veromesh-fuseki-replicas:3030/veromesh"
    assert one(docs, "Service", "veromesh-fuseki-replicas")["spec"]["ports"][0]["port"] == 3030
    assert env["VEROMESH_DATAHUB_GMS_URL"]["value"] == "http://datahub-datahub-gms:8080"
    # No token given: no Secret, no token variable
    assert "VEROMESH_DATAHUB_TOKEN" not in env
    assert not by_kind(docs, "Secret")
    # Waits for Fuseki before starting
    assert api["initContainers"][0]["name"] == "wait-for-fuseki"
    assert "http://veromesh-fuseki:3030/$/ping" in api["initContainers"][0]["command"][-1]


def test_selectors_match_their_pods():
    docs = render()
    workloads = by_kind(docs, "Deployment") + by_kind(docs, "StatefulSet")
    assert len(workloads) == 3
    for d in workloads:
        want = d["spec"]["selector"]["matchLabels"]
        have = d["spec"]["template"]["metadata"]["labels"]
        assert want.items() <= have.items(), (d["metadata"]["name"], want, have)
    for svc in by_kind(docs, "Service"):
        targets = [
            d
            for d in by_kind(docs, "Deployment") + by_kind(docs, "StatefulSet")
            if svc["spec"]["selector"].items() <= d["spec"]["template"]["metadata"]["labels"].items()
        ]
        assert len(targets) == 1, f"Service {svc['metadata']['name']} selects {len(targets)} workloads"
    assert one(docs, "StatefulSet", WRITER)["spec"]["serviceName"] == "veromesh-fuseki-headless"
    assert one(docs, "StatefulSet", REPLICA)["spec"]["serviceName"] == "veromesh-fuseki-replica-headless"
    pdb = one(docs, "PodDisruptionBudget")
    assert pdb["spec"]["maxUnavailable"] == 1
    assert selects(pdb["spec"]["selector"], pod_labels(one(docs, "StatefulSet", REPLICA)))
    assert not selects(pdb["spec"]["selector"], pod_labels(one(docs, "StatefulSet", WRITER)))


def test_only_the_service_and_the_backup_job_may_reach_fuseki():
    docs = render()
    policy = one(docs, "NetworkPolicy")
    api_labels = one(docs, "Deployment")["spec"]["template"]["metadata"]["labels"]
    backup_labels = one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    test_labels = one(docs, "Pod")["metadata"]["labels"]
    # Covers the writer and every replica, and nothing else.
    for sts in by_kind(docs, "StatefulSet"):
        assert selects(policy["spec"]["podSelector"], pod_labels(sts)), sts["metadata"]["name"]
    assert not selects(policy["spec"]["podSelector"], api_labels)
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    selectors = [p["podSelector"]["matchLabels"] for p in rule["from"]]
    assert len(selectors) == 2
    assert selectors[0].items() <= api_labels.items()
    assert selectors[1].items() <= backup_labels.items()
    for sel in selectors:
        assert not sel.items() <= test_labels.items(), "the test pod must not be allowed in"
    assert rule["ports"] == [{"port": 3030, "protocol": "TCP"}]
    # Backups off: only the service.
    (rule,) = one(render(sets=("backup.enabled=false",)), "NetworkPolicy")["spec"]["ingress"]
    assert len(rule["from"]) == 1


@pytest.mark.parametrize("name", [WRITER, REPLICA])
def test_model_survives_pod_restarts_and_uninstall(name):
    sts = one(render(), "StatefulSet", name)
    (claim,) = sts["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "data"
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    mounts = sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {"name": "data", "mountPath": "/fuseki/databases"} in mounts


@pytest.mark.parametrize("variant", ["(defaults)", *VARIANTS])
def test_every_container_runs_unprivileged(variant):
    docs = render() if variant == "(defaults)" else render(variant)
    for where, spec in pod_specs(docs):
        assert spec["securityContext"]["runAsNonRoot"] is True, where
        assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, where
        assert spec.get("automountServiceAccountToken") is False, where
        for c in spec.get("initContainers", []) + spec["containers"]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, (where, c["name"])
            assert sc["capabilities"]["drop"] == ["ALL"], (where, c["name"])
            assert not c["image"].endswith(":latest") and ":" in c["image"], (where, c["image"])
            if c["name"] != "test":
                assert c["resources"]["limits"]["memory"], (where, c["name"])


def test_images_default_to_the_pinned_versions():
    docs = render()
    api = one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert api["image"] == f"veromesh/service:{project_version('service')}"
    backup = one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert backup["image"] == f"veromesh/backup:{(ROOT / 'deploy/backup/VERSION').read_text().strip()}"
    for sts in by_kind(docs, "StatefulSet"):
        fuseki = sts["spec"]["template"]["spec"]["containers"][0]
        assert fuseki["image"] == f"veromesh/fuseki:{versions()['JENA_VERSION']}"


def test_chart_app_version_is_the_service_version():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["appVersion"] == project_version("service")


# ---------------------------------------------------------------------------- variants


def test_external_store():
    docs = render("external-store-values.yaml")
    assert not by_kind(docs, "StatefulSet") and not by_kind(docs, "NetworkPolicy")
    api = one(docs, "Deployment")["spec"]["template"]["spec"]
    assert "initContainers" not in api
    assert (
        env_of(api["containers"][0])["VEROMESH_RDF_STORE"]["value"]
        == "fuseki:http://fuseki.graph.svc.cluster.local:3030/veromesh"
    )


def test_inline_token_creates_a_secret():
    docs = render("inline-token-values.yaml")
    secret = one(docs, "Secret")
    assert secret["metadata"]["name"] == "veromesh-datahub" and "token" in secret["data"]
    ref = env_of(one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0])[
        "VEROMESH_DATAHUB_TOKEN"
    ]
    assert ref["valueFrom"]["secretKeyRef"] == {"name": "veromesh-datahub", "key": "token"}
    # Persistence off: an emptyDir, no claim (a restarted Fuseki rebuilds from Kafka)
    for sts in by_kind(docs, "StatefulSet"):
        assert "volumeClaimTemplates" not in sts["spec"]
        assert {"name": "data", "emptyDir": {}} in sts["spec"]["template"]["spec"]["volumes"]
    assert not by_kind(docs, "NetworkPolicy")


def test_sign_in_settings_reach_the_service():
    env = env_of(
        one(render("production-values.yaml"), "Deployment")["spec"]["template"]["spec"]["containers"][0]
    )
    assert env["VEROMESH_OIDC_ISSUER"]["value"].startswith("https://login.microsoftonline.com/")
    assert env["VEROMESH_OIDC_AUDIENCE"]["value"] == "api://veromesh-acme"
    assert env["VEROMESH_OIDC_ROLES_CLAIM"]["value"] == "roles"
    assert env["VEROMESH_OIDC_ROLE_PREFIX"]["value"] == "VeroMesh."
    assert "VEROMESH_AUTH_DISABLED" not in env


def test_decision_log_url_comes_from_a_secret():
    env = env_of(
        one(render("production-values.yaml"), "Deployment")["spec"]["template"]["spec"]["containers"][0]
    )
    ref = env["VEROMESH_DECISION_LOG"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "veromesh-decision-log", "key": "url"}
    default = env_of(one(render(), "Deployment")["spec"]["template"]["spec"]["containers"][0])
    assert "VEROMESH_DECISION_LOG" not in default  # memory, with a warning in the notes


def test_sign_in_is_never_off_by_default():
    env = env_of(one(render(), "Deployment")["spec"]["template"]["spec"]["containers"][0])
    assert "VEROMESH_AUTH_DISABLED" not in env
    off = env_of(one(render("kind-values.yaml"), "Deployment")["spec"]["template"]["spec"]["containers"][0])
    assert off["VEROMESH_AUTH_DISABLED"]["value"] == "true"


def test_production_shape():
    docs = render("production-values.yaml", name="prod")
    assert not by_kind(docs, "Secret"), "an existing Secret is referenced, not created"
    dep = one(docs, "Deployment", "prod-veromesh-api")
    spec = dep["spec"]["template"]["spec"]
    env = env_of(spec["containers"][0])
    assert env["VEROMESH_DATAHUB_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "veromesh-datahub",
        "key": "token",
    }
    assert env["VEROMESH_GLOSSARY_ROOT_NAME"]["value"] == "Acme Ontology"
    assert env["VEROMESH_INSTANCE_BASE"]["value"] == "urn:veromesh:org:acme:"
    assert spec["imagePullSecrets"] == [{"name": "registry-credentials"}]
    assert spec["containers"][0]["image"].startswith("registry.example.com/veromesh/service:")
    assert dep["spec"]["replicas"] == 2
    ing = one(docs, "Ingress")
    assert ing["spec"]["ingressClassName"] == "nginx"
    assert ing["spec"]["tls"][0]["secretName"] == "veromesh-tls"
    assert ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "prod-veromesh-api"
    for sts in by_kind(docs, "StatefulSet"):
        claim = sts["spec"]["volumeClaimTemplates"][0]["spec"]
        assert claim["storageClassName"] == "premium-rwo"
        assert claim["resources"]["requests"]["storage"] == "50Gi"
    # Replication over a managed Kafka with TLS/SASL settings from a Secret
    assert one(docs, "StatefulSet", "prod-veromesh-fuseki-replica")["spec"]["replicas"] == 3
    for sts in by_kind(docs, "StatefulSet"):
        spec = sts["spec"]["template"]["spec"]
        assert {
            "name": "kafka",
            "secret": {
                "secretName": "veromesh-kafka",
                "items": [{"key": "kafka.properties", "path": "kafka.properties"}],
            },
        } in spec["volumes"]
        assert {"name": "kafka", "mountPath": "/etc/fuseki/kafka", "readOnly": True} in spec["containers"][0][
            "volumeMounts"
        ]
    config = fuseki_cm(docs)["data"]
    for f in ("writer.ttl", "replica.ttl"):
        assert '"/etc/fuseki/kafka/kafka.properties"' in config[f]
        assert '"b-1.kafka.acme.example:9094,b-2.kafka.acme.example:9094"' in config[f]
        assert '"acme.veromesh.graph"' in config[f]
    assert 'vmr:replicationFactor "3"' in config["writer.ttl"]


# ---------------------------------------------------------------------------- backups


def backup_pod(docs: list[dict]) -> dict:
    return one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]


def test_backups_need_the_customers_key_and_a_destination():
    r = helm("template", "veromesh", str(CHART), check=False)
    assert r.returncode != 0 and "backup.encryption.recipients is required" in r.stderr, r.stderr
    r = helm(
        "template",
        "veromesh",
        str(CHART),
        "--set",
        f"backup.encryption.recipients={{{TEST_KEY}}}",
        check=False,
    )
    assert r.returncode != 0 and "backups need somewhere to go" in r.stderr, r.stderr
    # Development only: no key, backups in the clear.
    docs = render(sets=("backup.encryption.enabled=false",))
    env = env_of(backup_pod(docs)["spec"]["containers"][0])
    assert env["BACKUP_ENCRYPT"]["value"] == "false" and "AGE_RECIPIENTS_FILE" not in env
    assert not [c for c in by_kind(docs, "ConfigMap") if "recipients" in c["metadata"]["name"]]


@pytest.mark.parametrize("bad", ["not-a-key", "age1short", "ssh-dss AAAA"])
def test_schema_rejects_a_malformed_key(bad):
    r = helm(
        "template", "veromesh", str(CHART), "--set", f"backup.encryption.recipients={{{bad}}}", check=False
    )
    assert r.returncode != 0 and "schema" in r.stderr, r.stderr


def test_nightly_encrypted_backup_to_a_volume():
    docs = render(sets=("api.timeZone=America/New_York",))
    job = one(docs, "CronJob")
    assert job["spec"]["schedule"] == "17 2 * * *"
    assert job["spec"]["timeZone"] == "America/New_York", "the schedule follows the customer's time zone"
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    pod = backup_pod(docs)
    spec = pod["spec"]
    assert spec["serviceAccountName"] == "veromesh-backup" and spec["automountServiceAccountToken"] is False
    (c,) = spec["containers"]
    assert c["args"] == ["backup"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    env = env_of(c)
    assert env["FUSEKI_URL"]["value"] == "http://veromesh-fuseki:3030/veromesh", "the writer by default"
    assert env["BACKUP_TARGET"]["value"] == "/backups"
    assert env["BACKUP_KEEP"]["value"] == "30"
    assert env["BACKUP_ENCRYPT"]["value"] == "true"
    assert "DECISION_LOG_URL" not in env, "no decision log database given"
    assert one(docs, "ConfigMap", "veromesh-backup-recipients")["data"]["recipients"].split() == [TEST_KEY]
    assert not by_kind(docs, "PersistentVolumeClaim"), "the claim is created outside the release"
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["backups"]["persistentVolumeClaim"]["claimName"] == "veromesh-backups"
    assert vols["recipients"]["configMap"]["name"] == "veromesh-backup-recipients"
    assert pod["metadata"]["labels"]["app.kubernetes.io/component"] == "backup"
    assert "istio.io/dataplane-mode" not in pod["metadata"]["labels"]


def test_backup_to_object_storage_with_the_decision_log():
    docs = render("production-values.yaml", name="prod")
    spec = backup_pod(docs)["spec"]
    env = env_of(spec["containers"][0])
    assert env["BACKUP_TARGET"]["value"] == "azure:veromesh-backups/acme-prod"
    assert env["RCLONE_CONFIG"]["value"] == "/etc/rclone/rclone.conf"
    assert env["FUSEKI_URL"]["value"] == "http://prod-veromesh-fuseki-replicas:3030/veromesh", (
        "graphFrom: replicas"
    )
    assert env["DECISION_LOG_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "veromesh-decision-log",
        "key": "url",
    }
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["rclone"]["secret"]["secretName"] == "veromesh-backup-storage"
    assert spec["containers"][0]["image"].startswith("registry.example.com/veromesh/backup:")
    assert spec["imagePullSecrets"] == [{"name": "registry-credentials"}]
    keys = (
        one(docs, "ConfigMap", "prod-veromesh-backup-recipients")["data"]["recipients"].strip().splitlines()
    )
    assert len(keys) == 2 and keys[1].startswith("ssh-ed25519 ")


def test_backup_settings_that_cannot_work_are_refused():
    r = helm(
        "template", "veromesh", str(CHART), *WITH_KEY, "--set", "backup.rclone.existingSecret=s", check=False
    )
    assert r.returncode != 0 and "backup.rclone.remote is required" in r.stderr, r.stderr
    r = helm(
        "template",
        "veromesh",
        str(CHART),
        *WITH_KEY,
        "--set",
        "backup.graphFrom=replicas",
        "--set",
        "fuseki.replication.enabled=false",
        check=False,
    )
    assert r.returncode != 0 and "needs fuseki.replication.enabled" in r.stderr, r.stderr


def test_what_is_backed_up_follows_what_the_chart_runs():
    assert not by_kind(render(sets=("backup.enabled=false",)), "CronJob")
    # Your own store and no decision log: nothing for the chart to back up.
    assert not by_kind(render("external-store-values.yaml"), "CronJob")
    # Your own store, our decision log: the log only.
    docs = render(
        "external-store-values.yaml",
        sets=(
            "api.decisionLog.existingSecret=dl",
            f"backup.encryption.recipients={{{TEST_KEY}}}",
            "backup.volume.existingClaim=b",
        ),
    )
    env = env_of(backup_pod(docs)["spec"]["containers"][0])
    assert "FUSEKI_URL" not in env and env["DECISION_LOG_URL"]["valueFrom"]["secretKeyRef"]["name"] == "dl"


# ---------------------------------------------------------------------------- encryption in transit


def all_pod_labels(docs: list[dict]) -> dict[str, dict]:
    out = {}
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet"):
            out[d["metadata"]["name"]] = d["spec"]["template"]["metadata"]["labels"]
        elif d["kind"] == "CronJob":
            out[d["metadata"]["name"]] = d["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        elif d["kind"] == "Pod":
            out[d["metadata"]["name"]] = d["metadata"]["labels"]
    return out


def test_mesh_off_by_default():
    docs = render()
    assert not [d for d in docs if d["apiVersion"].startswith("security.istio.io")]
    for name, labels in all_pod_labels(docs).items():
        assert "istio.io/dataplane-mode" not in labels, name


def test_every_pod_joins_the_mesh_and_fuseki_takes_only_mutual_tls():
    docs = render("mesh-values.yaml")
    pods = all_pod_labels(docs)
    assert set(pods) == {"veromesh-api", WRITER, REPLICA, "veromesh-backup", "veromesh-test"}
    for name, labels in pods.items():
        assert labels["istio.io/dataplane-mode"] == "ambient", name

    peer = {p["metadata"]["name"]: p for p in by_kind(docs, "PeerAuthentication")}
    graph = peer["veromesh-fuseki"]["spec"]
    assert graph["mtls"]["mode"] == "STRICT"
    for name in (WRITER, REPLICA):
        assert selects(graph["selector"], pods[name]), name
    assert not selects(graph["selector"], pods["veromesh-api"])
    api = peer["veromesh-api"]["spec"]
    assert api["mtls"]["mode"] == "STRICT" and selects(api["selector"], pods["veromesh-api"])
    assert not selects(api["selector"], pods[WRITER])
    permissive = render("mesh-values.yaml", sets=("mesh.apiMtls=PERMISSIVE",))
    assert one(permissive, "PeerAuthentication", "veromesh-api")["spec"]["mtls"]["mode"] == "PERMISSIVE"
    assert one(permissive, "PeerAuthentication", "veromesh-fuseki")["spec"]["mtls"]["mode"] == "STRICT"


def test_only_the_service_and_backup_identities_reach_fuseki():
    docs = render("mesh-values.yaml")
    policy = one(docs, "AuthorizationPolicy")["spec"]
    pods = all_pod_labels(docs)
    for name in (WRITER, REPLICA):
        assert selects(policy["selector"], pods[name]), name
    assert policy["action"] == "ALLOW"
    (rule,) = policy["rules"]
    assert rule["from"][0]["source"]["principals"] == [
        "cluster.local/ns/veromesh/sa/veromesh",
        "cluster.local/ns/veromesh/sa/veromesh-backup",
    ]
    api_sa = one(docs, "Deployment")["spec"]["template"]["spec"]["serviceAccountName"]
    assert api_sa == "veromesh"
    other = render("mesh-values.yaml", sets=("mesh.trustDomain=acme.example", "backup.enabled=false"))
    assert one(other, "AuthorizationPolicy")["spec"]["rules"][0]["from"][0]["source"]["principals"] == [
        "acme.example/ns/veromesh/sa/veromesh"
    ]


def test_network_policy_lets_the_mesh_through():
    """Ambient traffic arrives on ztunnel's HBONE port, and probes from Istio's probe address."""
    rules = one(render("mesh-values.yaml"), "NetworkPolicy")["spec"]["ingress"]
    assert rules[0]["ports"] == [{"port": 3030, "protocol": "TCP"}]
    assert rules[1]["ports"] == [{"port": 15008, "protocol": "TCP"}]
    assert rules[1]["from"] == rules[0]["from"], "the same peers, through the mesh"
    assert rules[2] == {
        "from": [{"ipBlock": {"cidr": "169.254.7.127/32"}}],
        "ports": [{"port": 3030, "protocol": "TCP"}],
    }
    assert len(one(render(), "NetworkPolicy")["spec"]["ingress"]) == 1


# ---------------------------------------------------------------------------- replication

VMR = "https://veromesh.ai/ns/fuseki-replication#"
FK = "http://jena.apache.org/fuseki/kafka#"
FUSEKI = "http://jena.apache.org/fuseki#"
TDB2 = "http://jena.apache.org/2016/tdb#"


def fuseki_config(docs: list[dict], name: str):
    """One of the rendered Fuseki configuration files, parsed."""
    rdflib = pytest.importorskip("rdflib")
    g = rdflib.Graph()
    g.parse(data=fuseki_cm(docs)["data"][name], format="turtle", publicID="file:///etc/fuseki/x")
    return g, rdflib


def values_of(g, rdflib, prop: str) -> set[str]:
    return {str(o) for o in g.objects(None, rdflib.URIRef(prop))}


def endpoints(g, rdflib) -> set[tuple[str, str]]:
    out = set()
    for ep in g.objects(None, rdflib.URIRef(FUSEKI + "endpoint")):
        op = g.value(ep, rdflib.URIRef(FUSEKI + "operation"))
        name = g.value(ep, rdflib.URIRef(FUSEKI + "name"))
        out.add((str(op).removeprefix(FUSEKI), str(name)))
    return out


def test_writer_takes_writes_and_publishes_them_to_kafka():
    docs = render()
    g, rdflib = fuseki_config(docs, "writer.ttl")
    assert values_of(g, rdflib, FUSEKI + "name") >= {"veromesh"}
    # The endpoints the service uses: query, update, Graph Store read/write
    assert endpoints(g, rdflib) == {("query", "query"), ("update", "update"), ("gsp-rw", "data")}
    assert (None, rdflib.RDF.type, rdflib.URIRef(VMR + "PublishingDataset")) in g
    assert values_of(g, rdflib, VMR + "bootstrapServers") == {"prerequisites-kafka:9092"}
    assert values_of(g, rdflib, VMR + "topic") == {"veromesh.graph"}
    assert values_of(g, rdflib, VMR + "stateFile") == {"/fuseki/databases/writer.state"}
    assert values_of(g, rdflib, VMR + "transactionalId") == {"veromesh-fuseki-veromesh.graph"}
    assert not values_of(g, rdflib, VMR + "configFile"), "no Kafka properties Secret by default"
    # The single-node dataset location, so switching replication on keeps an existing model.
    assert values_of(g, rdflib, TDB2 + "location") == {"/fuseki/databases/veromesh"}
    run_sh = (ROOT / "deploy/fuseki/run.sh").read_text()
    assert '--loc "$DB/$FUSEKI_DATASET"' in run_sh and "DB=/fuseki/databases" in run_sh


def test_replicas_are_read_only_and_follow_the_topic():
    docs = render()
    g, rdflib = fuseki_config(docs, "replica.ttl")
    assert endpoints(g, rdflib) == {("query", "query"), ("gsp-r", "data")}, "no update endpoint"
    assert values_of(g, rdflib, TDB2 + "location") == {"/fuseki/databases/veromesh"}
    assert values_of(g, rdflib, FK + "topic") == {"veromesh.graph"}
    assert values_of(g, rdflib, FK + "bootstrapServers") == {"prerequisites-kafka:9092"}
    assert values_of(g, rdflib, FK + "fusekiServiceName") == {"/veromesh"}
    assert values_of(g, rdflib, FK + "groupId") == {"env:{VEROMESH_KAFKA_GROUP_ID}"}
    assert values_of(g, rdflib, FK + "stateFile") == {"/fuseki/databases/replica.state"}
    # Kafka settings, as (key value) lists
    from rdflib.collection import Collection

    pairs = {}
    for lst in g.objects(None, rdflib.URIRef(FK + "config")):
        k, v = (str(x) for x in Collection(g, lst))
        pairs[k] = v
    assert pairs == {
        "isolation.level": "read_committed",
        "auto.offset.reset": "earliest",
        "allow.auto.create.topics": "false",
    }


def test_writer_and_replicas_run_their_configuration():
    docs = render()
    cm = fuseki_cm(docs)
    for name, role in ((WRITER, "writer"), (REPLICA, "replica")):
        sts = one(docs, "StatefulSet", name)
        spec = sts["spec"]["template"]["spec"]
        c = spec["containers"][0]
        env = env_of(c)
        assert env["FUSEKI_CONFIG"]["value"] == f"/etc/fuseki/{role}.ttl"
        assert ("REPLICA_ID_FILE" in env) == (role == "replica")
        assert {"name": "config", "mountPath": "/etc/fuseki", "readOnly": True} in c["volumeMounts"]
        assert {"name": "config", "configMap": {"name": cm["metadata"]["name"]}} in spec["volumes"]
        assert "checksum/config" in sts["spec"]["template"]["metadata"]["annotations"]
        # Catching up with Kafka can take a while before Fuseki answers.
        assert c["startupProbe"]["failureThreshold"] * c["startupProbe"]["periodSeconds"] >= 600
        # Spread over nodes by default
        terms = spec["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert terms[0]["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname"
    assert one(docs, "StatefulSet", WRITER)["spec"]["replicas"] == 1
    assert one(docs, "StatefulSet", REPLICA)["spec"]["replicas"] == 2
    assert one(docs, "StatefulSet", REPLICA)["spec"]["podManagementPolicy"] == "Parallel"


def test_replica_count_and_custom_affinity():
    docs = render(sets=("fuseki.replication.replicas=1", "fuseki.affinity.nodeAffinity.x=y"))
    assert one(docs, "StatefulSet", REPLICA)["spec"]["replicas"] == 1
    assert not by_kind(docs, "PodDisruptionBudget"), "a PDB on one replica would block node drains"
    for sts in by_kind(docs, "StatefulSet"):
        assert sts["spec"]["template"]["spec"]["affinity"] == {"nodeAffinity": {"x": "y"}}


def test_single_node_without_replication():
    docs = render("single-node-values.yaml")
    (sts,) = by_kind(docs, "StatefulSet")
    assert sts["metadata"]["name"] == WRITER
    configs = [c["metadata"]["name"] for c in by_kind(docs, "ConfigMap")]
    assert configs == ["veromesh-backup-recipients"] and not by_kind(docs, "PodDisruptionBudget")
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert "FUSEKI_CONFIG" not in env_of(c)
    assert c["startupProbe"]["failureThreshold"] == 36
    assert "affinity" not in sts["spec"]["template"]["spec"]
    api = env_of(one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0])
    assert "VEROMESH_RDF_STORE_READ_FALLBACK" not in api
    assert {s["metadata"]["name"] for s in by_kind(docs, "Service")} == {
        "veromesh-api",
        "veromesh-fuseki",
        "veromesh-fuseki-headless",
    }


def test_replication_versions_agree():
    v = versions()
    dockerfile = (ROOT / "deploy/fuseki/Dockerfile").read_text()
    assert f"ARG JENA_VERSION={v['JENA_VERSION']}" in dockerfile
    assert f"ARG FUSEKI_KAFKA_VERSION={v['FUSEKI_KAFKA_VERSION']}" in dockerfile
    pom = (ROOT / "deploy/fuseki/replication/pom.xml").read_text()
    assert f"<jena.version>{v['JENA_VERSION']}</jena.version>" in pom
    assert f"<fuseki-kafka.version>{v['FUSEKI_KAFKA_VERSION']}</fuseki-kafka.version>" in pom
    compose = (ROOT / "deploy/docker-compose.veromesh.yml").read_text()
    assert f"FUSEKI_KAFKA_VERSION:-{v['FUSEKI_KAFKA_VERSION']}" in compose


# ---------------------------------------------------------------------------- Kubernetes schemas


@pytest.mark.skipif(not shutil.which("kubeconform"), reason="kubeconform is not on PATH")
@pytest.mark.parametrize("variant", ["(defaults)", *VARIANTS])
def test_manifests_match_kubernetes_schemas(variant, tmp_path):
    args = ["template", "veromesh", str(CHART), "--kube-version", KUBE_VERSION, "--include-crds"]
    args += WITH_KEY if variant == "(defaults)" else ["-f", str(CI / variant)]
    out = tmp_path / "out.yaml"
    out.write_text(helm(*args).stdout)
    r = subprocess.run(
        # Istio's resources are checked by the mesh tests above and on a real mesh (k8s-mesh).
        [
            "kubeconform",
            "-strict",
            "-summary",
            "-kubernetes-version",
            KUBE_VERSION,
            "-skip",
            "PeerAuthentication,AuthorizationPolicy",
            str(out),
        ],
        text=True,
        capture_output=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------------------- DataHub overlays


def pulled_chart(name: str, version: str) -> Path:
    """DataHub's official chart at the pinned version, cached under .build/."""
    target = CACHE / f"{name}-{version}.tgz"
    if not target.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        r = helm("pull", name, "--repo", DATAHUB_REPO, "--version", version, "-d", str(CACHE), check=False)
        if r.returncode != 0:
            if os.environ.get("CI"):
                pytest.fail(f"could not download {name} {version}: {r.stderr}")
            pytest.skip(f"could not download {name} {version} from {DATAHUB_REPO} (offline?)")
    return target


def test_datahub_overlay_loads_the_plugin_and_the_brand():
    v = versions()
    chart = pulled_chart("datahub", v["DATAHUB_CHART_VERSION"])
    meta = yaml.safe_load(
        subprocess.run(["helm", "show", "chart", str(chart)], text=True, capture_output=True).stdout
    )
    assert meta["appVersion"] == v["DATAHUB_VERSION"], "chart version does not match DATAHUB_VERSION"

    docs = render(DATAHUB_OVERLAYS / "datahub-values.yaml", chart=chart, name="datahub")
    deployments = {d["metadata"]["name"]: d for d in by_kind(docs, "Deployment")}
    gms = deployments["datahub-datahub-gms"]["spec"]["template"]["spec"]
    front = deployments["datahub-datahub-frontend"]["spec"]["template"]["spec"]

    # The plugin image is copied into GMS's plugin folder before GMS starts
    (init,) = [c for c in gms.get("initContainers", []) if c["name"] == "veromesh-model-plugin"]
    assert init["image"] == f"veromesh/datahub-model-plugin:{project_version('service')}"
    gms_c = next(c for c in gms["containers"] if c["name"] == "datahub-gms")
    assert {
        "name": "veromesh-plugins",
        "mountPath": "/etc/datahub/plugins/models",
        "readOnly": True,
    } in gms_c["volumeMounts"]

    # Brand: the VeroMesh frontend image and theme settings on both
    front_c = front["containers"][0]
    assert front_c["image"] == f"docker.io/veromesh/datahub-frontend-react:{v['DATAHUB_VERSION']}"
    brand = dict(
        line.split("=", 1)
        for line in (ROOT / "branding/datahub/datahub.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    for c in (gms_c, front_c):
        env = {e["name"]: e.get("value") for e in c["env"]}
        for k, val in brand.items():
            assert env.get(k) == val, (c["name"], k)

    # PostgreSQL from the prerequisites chart
    env = {e["name"]: e.get("value") for e in gms_c["env"]}
    assert env["EBEAN_DATASOURCE_URL"] == "jdbc:postgresql://prerequisites-postgresql:5432/datahub"
    assert env["EBEAN_DATASOURCE_DRIVER"] == "org.postgresql.Driver"

    # Kept inside the cluster
    for svc in by_kind(docs, "Service"):
        if svc["metadata"]["name"] in ("datahub-datahub-gms", "datahub-datahub-frontend"):
            assert svc["spec"]["type"] == "ClusterIP", svc["metadata"]["name"]


def test_prerequisites_overlay_matches_the_local_stack():
    v = versions()
    chart = pulled_chart("datahub-prerequisites", v["DATAHUB_PREREQUISITES_CHART_VERSION"])
    docs = render(DATAHUB_OVERLAYS / "prerequisites-values.yaml", chart=chart, name="prerequisites")
    names = {d["metadata"]["name"] for d in by_kind(docs, "StatefulSet")}
    assert "prerequisites-postgresql" in names, names
    assert not any("mysql" in n for n in names), names
    assert any("opensearch" in n for n in names), names
    assert any("kafka" in n for n in names), names
    # One broker that allows transactions (the VeroMesh writer publishes in transactions)
    rendered = yaml.safe_dump_all(docs)
    for setting in ("transaction.state.log.replication.factor=1", "transaction.state.log.min.isr=1"):
        assert setting in rendered, setting

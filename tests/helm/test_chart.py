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


def env_of(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def pod_specs(docs: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet"):
            out.append((f"{d['kind']}/{d['metadata']['name']}", d["spec"]["template"]["spec"]))
        elif d["kind"] == "Pod":
            out.append((f"Pod/{d['metadata']['name']}", d["spec"]))
    return out


VARIANTS = sorted(p.name for p in CI.glob("*.yaml"))


# ---------------------------------------------------------------------------- lint and schema


@pytest.mark.parametrize("variant", ["(defaults)", *VARIANTS])
def test_lint(variant):
    args = ["lint", str(CHART), "--strict", "--kube-version", KUBE_VERSION]
    if variant != "(defaults)":
        args += ["-f", str(CI / variant)]
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
        "Deployment/veromesh-api",
        "NetworkPolicy/veromesh-fuseki",
        "Pod/veromesh-test",
        "Service/veromesh-api",
        "Service/veromesh-fuseki",
        "Service/veromesh-fuseki-headless",
        "ServiceAccount/veromesh",
        "StatefulSet/veromesh-fuseki",
    ], kinds


def test_service_finds_fuseki_and_datahub():
    docs = render()
    api = one(docs, "Deployment")["spec"]["template"]["spec"]
    env = env_of(api["containers"][0])
    fuseki_svc = one(docs, "Service", "veromesh-fuseki")
    assert env["VEROMESH_RDF_STORE"]["value"] == "fuseki:http://veromesh-fuseki:3030/veromesh"
    assert fuseki_svc["spec"]["ports"][0]["port"] == 3030
    assert env["VEROMESH_DATAHUB_GMS_URL"]["value"] == "http://datahub-datahub-gms:8080"
    # No token given: no Secret, no token variable
    assert "VEROMESH_DATAHUB_TOKEN" not in env
    assert not by_kind(docs, "Secret")
    # Waits for Fuseki before starting
    assert api["initContainers"][0]["name"] == "wait-for-fuseki"
    assert "http://veromesh-fuseki:3030/$/ping" in api["initContainers"][0]["command"][-1]


def test_selectors_match_their_pods():
    docs = render()
    for kind in ("Deployment", "StatefulSet"):
        d = one(docs, kind)
        want = d["spec"]["selector"]["matchLabels"]
        have = d["spec"]["template"]["metadata"]["labels"]
        assert want.items() <= have.items(), (kind, want, have)
    for svc in by_kind(docs, "Service"):
        targets = [
            d
            for d in by_kind(docs, "Deployment") + by_kind(docs, "StatefulSet")
            if svc["spec"]["selector"].items() <= d["spec"]["template"]["metadata"]["labels"].items()
        ]
        assert len(targets) == 1, f"Service {svc['metadata']['name']} selects {len(targets)} workloads"
    sts = one(docs, "StatefulSet")
    assert sts["spec"]["serviceName"] == "veromesh-fuseki-headless"


def test_only_the_service_may_reach_fuseki():
    docs = render()
    policy = one(docs, "NetworkPolicy")
    fuseki_labels = one(docs, "StatefulSet")["spec"]["template"]["metadata"]["labels"]
    api_labels = one(docs, "Deployment")["spec"]["template"]["metadata"]["labels"]
    test_labels = one(docs, "Pod")["metadata"]["labels"]
    assert policy["spec"]["podSelector"]["matchLabels"].items() <= fuseki_labels.items()
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    selectors = [p["podSelector"]["matchLabels"] for p in rule["from"]]
    assert len(selectors) == 1 and selectors[0].items() <= api_labels.items()
    assert not selectors[0].items() <= test_labels.items(), "the test pod must not be allowed in"
    assert rule["ports"] == [{"port": 3030, "protocol": "TCP"}]


def test_model_survives_pod_restarts_and_uninstall():
    sts = one(render(), "StatefulSet")
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
    fuseki = one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"][0]
    assert api["image"] == f"veromesh/service:{project_version('service')}"
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
    # Persistence off: an emptyDir, no claim
    sts = one(docs, "StatefulSet")
    assert "volumeClaimTemplates" not in sts["spec"]
    assert {"name": "data", "emptyDir": {}} in sts["spec"]["template"]["spec"]["volumes"]
    assert not by_kind(docs, "NetworkPolicy")


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
    claim = one(docs, "StatefulSet")["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["storageClassName"] == "premium-rwo" and claim["resources"]["requests"]["storage"] == "50Gi"
    froms = one(docs, "NetworkPolicy")["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app": "fuseki-backup"}}} in froms


# ---------------------------------------------------------------------------- Kubernetes schemas


@pytest.mark.skipif(not shutil.which("kubeconform"), reason="kubeconform is not on PATH")
@pytest.mark.parametrize("variant", ["(defaults)", *VARIANTS])
def test_manifests_match_kubernetes_schemas(variant, tmp_path):
    args = ["template", "veromesh", str(CHART), "--kube-version", KUBE_VERSION, "--include-crds"]
    if variant != "(defaults)":
        args += ["-f", str(CI / variant)]
    out = tmp_path / "out.yaml"
    out.write_text(helm(*args).stdout)
    r = subprocess.run(
        ["kubeconform", "-strict", "-summary", "-kubernetes-version", KUBE_VERSION, str(out)],
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

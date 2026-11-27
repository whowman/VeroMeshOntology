"""The Workday and Entra ID connectors against the VeroMesh test harness (a simulated company).

Skipped unless the harness is installed:

    pip install "veromesh-test-harness @ git+https://github.com/whowman/VeroMeshTestHarness"

The harness serves mocks of Workday's and Microsoft Graph's public APIs over a
company whose truth it knows, with each system's quirks recorded as defects.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

harness_truth = pytest.importorskip("harness.truth")
harness_app = pytest.importorskip("harness.app")

from fastapi.testclient import TestClient  # noqa: E402

from veromesh_connectors.cli import main  # noqa: E402

API = ["--api", "http://testserver"]
WORKDAY = ["workday", "import", "--url", "http://testserver/workday", "--tenant", "kvh"]
GRAPH = ["--tenant", "kvh", "--graph", "http://testserver/entra", "--login", "http://testserver/entra"]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    for k, v in {
        "WORKDAY_CLIENT_ID": "veromesh-connector",
        "WORKDAY_CLIENT_SECRET": "harness-secret",
        "WORKDAY_REFRESH_TOKEN": "harness-workday-refresh",
        "ENTRA_CLIENT_ID": "veromesh-connector",
        "ENTRA_CLIENT_SECRET": "harness-secret",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def company():
    truth = harness_truth.generate("healthcare", 300, 7)
    return truth, TestClient(harness_app.create_app(truth))


def load(http, h) -> None:
    import tempfile

    groups = Path(tempfile.mkdtemp()) / "groups.yaml"  # Workday security groups to model roles
    groups.write_text("Manager: PeopleManager\nHR Partner: HRPartner\n")
    wd = [*WORKDAY, "--manager-role", "Manager", "--role-map", str(groups)]
    assert main([*API, *wd], http=http, source_http=h) == 0
    # The business process rules (promotions, pay, hires, terminations) the workbook's agents rely on.
    approvals = ["workday", "approvals", *WORKDAY[2:], "--role-map", str(groups)]
    assert main([*API, *approvals], http=http, source_http=h) == 0
    assert (
        main([*API, "entra", "import", *GRAPH, "--role-group-prefix", "SG-"], http=http, source_http=h) == 0
    )


def test_everyone_working_is_loaded_and_agents_act_for_their_principals(http, company):
    truth, h = company
    load(http, h)
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    employees = {p.employee_id for p in truth.active_people() if p.worker_type == "employee"}
    assert employees <= people
    agents = {a["id"]: a for a in http.get("/api/v1/agents").json()}
    for a in truth.agents.values():
        assert agents[a.id]["actsFor"] == truth.people[a.acts_for].employee_id


def test_a_month_of_changes_through_both_feeds(http, company, tmp_path):
    truth, h = company
    load(http, h)
    state = tmp_path / "entra.json"
    entra = [*API, "entra", "changes", *GRAPH, "--role-group-prefix", "SG-", "--state", str(state)]
    assert main(entra, http=http, source_http=h) == 0  # records the position
    start = truth.today
    h.post("/harness/clock", json={"days": 30})
    changes = [*API, "workday", "changes", "--url", "http://testserver/workday", "--tenant", "kvh"]
    assert main([*changes, "--since", start.isoformat()], http=http, source_http=h) == 0
    assert main(entra, http=http, source_http=h) == 0
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    left = {p.employee_id for p in truth.people.values() if p.left and start < p.left <= truth.today}
    hired = {p.employee_id for p in truth.people.values() if start < p.hired <= truth.today}
    # A leaver whose reports' moves wait for attestation (the proposal step) is kept only as
    # an ended record, so the reporting line in force still resolves; no source lists them.
    ended = {p["id"] for p in http.get("/api/v1/people", params={"source": "ended"}).json()}
    assert left and (left & people) <= ended
    waiting = {p["inForce"] for p in http.get("/api/v1/proposals", params={"type": "people"}).json()}
    assert (left & people) <= waiting
    assert hired and hired <= people


def test_misbehaving_systems_give_the_same_result(http, company):
    truth, h = company
    h.put("/harness/chaos", json={"rate": 0.3, "seed": 1, "slow_ms": 20})
    load(http, h)
    seen = h.get("/harness/chaos").json()["seen"]
    assert seen["throttle"] + seen["server-error"] + seen["unavailable"] + seen["token-expired"] > 0
    calm = harness_truth.generate("healthcare", 300, 7)
    assert {p["id"] for p in http.get("/api/v1/people").json()} >= {
        p.employee_id for p in calm.active_people() if p.worker_type == "employee"
    }


ERP = ["--url", "http://testserver/erp"]
ERP_ROLES = {
    "ORA_ACCOUNTS_PAYABLE_SPECIALIST_JOB": "APClerk",
    "ORA_ACCOUNTS_PAYABLE_MANAGER_JOB": "APManager",
    "ORA_FINANCIAL_APPROVER_JOB": "FinanceApprover",
    "ORA_BUYER_JOB": "Buyer",
    "ORA_PROCUREMENT_MANAGER_JOB": "ProcurementApprover",
}


def test_oracle_erp_accounts_and_approval_exceptions(http, company, tmp_path, monkeypatch):
    truth, h = company
    monkeypatch.setenv("ORACLE_USER", "integration.user")
    monkeypatch.setenv("ORACLE_PASSWORD", "harness")
    load(http, h)
    book = tmp_path / "governance.xlsx"
    book.write_bytes(h.get("/governance/workbook.xlsx").content)
    assert main([*API, "spreadsheet", "import", str(book), "--source", "governance"], http=http) == 0
    for name, cat in (("workday", "hr"), ("entra", "iam"), ("governance", "governance"), ("erp", "erp")):
        assert http.put(f"/api/v1/sources/{name}", json={"category": cat}).status_code == 200
    roles = tmp_path / "erp-roles.yaml"
    roles.write_text("".join(f"{k}: {v}\n" for k, v in ERP_ROLES.items()))
    assert main([*API, "erp", "import", *ERP, "--role-map", str(roles)], http=http, source_http=h) == 0

    erp = {p["id"]: p for p in http.get("/api/v1/people", params={"source": "erp"}).json()}
    known = {p["id"] for p in http.get("/api/v1/people", params={"source": "entra"}).json()}
    assert erp and set(erp) <= known  # every ERP account matched to the person Entra ID holds
    assert "employeeId" not in next(iter(erp.values()))  # the ERP is not the record of employment
    by_eid = {p.employee_id: p for p in truth.people.values()}
    ap = [pid for pid, p in erp.items() if "APManager" in (p.get("rolesFilled") or [])]
    assert ap and all("APManager" in by_eid[pid].at(truth.today).roles for pid in ap if pid in by_eid)

    since = (truth.start - timedelta(days=90)).isoformat()
    assert main([*API, "erp", "exceptions", *ERP, "--since", since], http=http, source_http=h) == 0
    key = h.get("/harness/answer-key").json()["defects"]

    def got(kind, field):
        return {
            f["about"][field] for f in http.get("/api/v1/findings", params={"kind": kind}).json()["findings"]
        }

    assert got("self-approved-po", "po") == {d["about"]["po"] for d in key if d["kind"] == "self-approved-po"}
    assert got("approved-over-limit", "invoice") == {
        d["about"]["invoice"] for d in key if d["kind"] == "approved-over-limit"
    }


def test_servicenow_cmdb_systems_and_owner_roles(http, company, monkeypatch):
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    load(http, h)
    for name, cat in (("workday", "hr"), ("entra", "iam"), ("servicenow", "cmdb")):
        assert http.put(f"/api/v1/sources/{name}", json={"category": cat}).status_code == 200
    assert (
        main(
            [*API, "servicenow", "import", "--url", "http://testserver/servicenow"], http=http, source_http=h
        )
        == 0
    )
    systems = {s["id"]: s for s in http.get("/api/v1/systems", params={"source": "servicenow"}).json()}
    assert len(systems) == len(truth.systems) + 1  # one application is in the CMDB twice
    ehr = next(s for s in systems.values() if s["name"] == truth.systems["EHR"].name)
    assert ehr["businessOwner"] == f"{ehr['id']}.BusinessOwner" and ehr["criticality"] == "critical"
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    owner = truth.people[truth.systems["EHR"].business_owner]
    roles = people[owner.employee_id].get("rolesFilled") or []
    assert ehr["businessOwner"] in (roles if isinstance(roles, list) else [roles])
    dups = http.get("/api/v1/findings", params={"kind": "duplicate-system"}).json()["findings"]
    assert len(dups) == 1 and dups[0]["about"]["names"][1] == "Epic"


def test_halo_applications_and_agent_seats(http, company, monkeypatch):
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    monkeypatch.setenv("HALO_CLIENT_ID", "veromesh-connector")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "harness-secret")
    load(http, h)
    for name, cat, access in (
        ("workday", "hr", None),
        ("entra", "iam", None),
        ("servicenow", "cmdb", None),
        ("servicenow-users", "cmdb", True),
        ("halo", "cmdb", None),
        ("halo-agents", "cmdb", True),
    ):
        body = {"category": cat, **({"grantsAccess": access} if access is not None else {})}
        assert http.put(f"/api/v1/sources/{name}", json=body).status_code == 200
    assert (
        main(
            [*API, "servicenow", "import", "--url", "http://testserver/servicenow"], http=http, source_http=h
        )
        == 0
    )
    assert http.get("/api/v1/people", params={"source": "servicenow-users"}).json()
    assert main([*API, "halo", "import", "--url", "http://testserver/halo"], http=http, source_http=h) == 0
    sn = {s["name"]: s["id"] for s in http.get("/api/v1/systems", params={"source": "servicenow"}).json()}
    halo = {s["name"]: s for s in http.get("/api/v1/systems", params={"source": "halo"}).json()}
    assert halo and all(halo[n]["id"] == sn[n] for n in halo if n in sn)  # one system, two sources
    agents = http.get("/api/v1/people", params={"source": "halo-agents"}).json()
    it = {
        p.employee_id
        for p in truth.people.values()
        if (p.at(truth.today) or p.history[-1]).unit in ("IT", "INFRA", "APPS", "SEC")
    }
    assert agents and {a["id"] for a in agents} <= it | {
        a["id"] for a in agents if not a["id"].startswith("W")
    }


def _skip_without(h, path: str, what: str):
    if h.get(path).status_code == 404:
        pytest.skip(f"this harness has no {what}")


def test_servicenow_ea_capability_map(http, company, monkeypatch):
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    _skip_without(h, "/servicenow/api/now/table/cmdb_ci_business_capability?sysparm_limit=1", "ServiceNow EA")
    load(http, h)
    assert http.put("/api/v1/sources/servicenow-ea", json={"category": "ea"}).status_code == 200
    assert (
        main([*API, "servicenow", "ea", "--url", "http://testserver/servicenow"], http=http, source_http=h)
        == 0
    )
    caps = {c["id"]: c for c in http.get("/api/v1/capabilities", params={"source": "servicenow-ea"}).json()}
    prof = __import__(f"harness.profiles.{truth.profile}", fromlist=["EA_CAPABILITIES"])
    assert set(caps) == {c[0] for c in prof.EA_CAPABILITIES}
    for cid, _name, parent, role in prof.EA_CAPABILITIES:
        assert caps[cid].get("parentCapability") == parent
        holder = truth.holder(role)
        if holder:  # the owner role is filled by the person in it (as of the monthly review)
            assert caps[cid]["ownerRole"] == f"{cid}.Owner"


def test_leanix_gives_the_same_capabilities_as_servicenow_ea(http, company, monkeypatch):
    truth, h = company
    _skip_without(h, "/harness/status", "status")
    if "leanix" not in h.get("/harness/status").json()["systems"]:
        pytest.skip("this harness has no LeanIX")
    monkeypatch.setenv("LEANIX_API_TOKEN", "harness-leanix-api-token")
    load(http, h)
    assert (
        main([*API, "leanix", "import", "--url", "http://testserver/leanix"], http=http, source_http=h) == 0
    )
    lx = {c["id"]: c for c in http.get("/api/v1/capabilities", params={"source": "leanix"}).json()}
    prof = __import__(f"harness.profiles.{truth.profile}", fromlist=["EA_CAPABILITIES"])
    assert set(lx) == {c[0] for c in prof.EA_CAPABILITIES}  # the archived one is left out
    assert lx["AccountsPayable"]["parentCapability"] == "Finance"
    owners = {p["id"] for p in http.get("/api/v1/people", params={"source": "leanix"}).json()}
    assert owners  # Business Owner subscriptions matched to people by email


def test_workday_goals_as_objectives(http, company, tmp_path):
    truth, h = company
    load(http, h)
    goals = [*API, "workday", "goals", "--url", "http://testserver/workday", "--tenant", "kvh"]
    prof = __import__(f"harness.profiles.{truth.profile}", fromlist=["OBJECTIVES", "TITLE_ROLES"])
    if not hasattr(prof, "OBJECTIVES"):
        pytest.skip("this harness profile has no objectives")
    titles = tmp_path / "titles.yaml"
    titles.write_text("".join(f'"{t}": {roles[0]}\n' for t, roles in prof.TITLE_ROLES.items() if roles))
    book = tmp_path / "governance.xlsx"
    book.write_bytes(h.get("/governance/workbook.xlsx").content)
    assert (
        main([*API, "spreadsheet", "import", str(book), "--source", "governance"], http=http) == 0
    )  # the KPIs
    code = main([*goals, "--role-map", str(titles)], http=http, source_http=h)
    if (
        code != 0
        and h.get("/workday/ccx/service/customreport2/kvh/ISU_VeroMesh/VeroMesh_Goals").status_code == 404
    ):
        pytest.skip("this harness has no Workday goals report")
    assert code == 0
    got = {o["id"]: o for o in http.get("/api/v1/objectives", params={"source": "workday-goals"}).json()}
    assert set(got) == {o[0] for o in prof.OBJECTIVES}
    for oid, _name, level, parent, _role, _priority, _kpis, _conflicts in prof.OBJECTIVES:
        assert got[oid]["level"] == level
        assert got[oid].get("parentObjective") == parent


def test_dbt_tables_lineage_and_a_breaking_change(http, company, monkeypatch, tmp_path):
    """dbt's run artifacts give the lakehouse's tables, their lineage and the KPIs computed
    from them; a change to a bronze table reaches everything downstream and their owners."""
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    if "metrics" not in h.get("/dbt/manifest.json").json():
        pytest.skip("this harness's dbt project has no metrics")
    load(http, h)
    book = tmp_path / "governance.xlsx"
    book.write_bytes(h.get("/governance/workbook.xlsx").content)
    assert (
        main([*API, "spreadsheet", "import", str(book), "--source", "governance"], http=http) == 0
    )  # the KPIs
    assert (
        main(
            [*API, "servicenow", "import", "--url", "http://testserver/servicenow"], http=http, source_http=h
        )
        == 0
    )
    lake = next(
        s["id"] for s in http.get("/api/v1/systems").json() if s["name"] == truth.systems["LAKE"].name
    )
    assert http.put("/api/v1/sources/dbt", json={"category": "catalog"}).status_code == 200
    dbt = [*API, "dbt", "import", "--url", "http://testserver/dbt", "--runs-on", lake]
    assert main(dbt, http=http, source_http=h) == 0

    dps = {d["id"] for d in http.get("/api/v1/data-products", params={"source": "dbt"}).json()}
    assert dps == set(truth.assets)
    downstream = __import__("harness.scenarios", fromlist=["_downstream"])._downstream
    tracked = {k["id"] for k in http.get("/api/v1/kpis").json()}  # dbt links only KPIs the model tracks
    kpi_tables = {k.asset for k in truth.kpis.values() if k.id in tracked}
    asset = next(
        a
        for a in sorted(truth.assets)
        if truth.assets[a].layer == "bronze" and kpi_tables & set(downstream(truth, a))
    )
    got = http.get(f"/api/v1/impact/data-products/{asset}").json()
    assert sorted(d["id"] for d in got["downstream"]) == downstream(truth, asset)
    affected = {asset, *downstream(truth, asset)}
    expected = sorted(k.id for k in truth.kpis.values() if k.asset in affected and k.id in tracked)
    assert expected and sorted(k["id"] for k in got["kpis"]) == expected
    # Whom to tell: the owners of what changes, and the lakehouse's technical owner.
    told = {p["id"] if isinstance(p, dict) else p for e in got["notify"] for p in e.get("people", [])}
    owners = {truth.people[truth.assets[a].owner].employee_id for a in affected if truth.assets[a].owner}
    assert owners <= told


DATABRICKS = ["databricks", "import", "--url", "http://testserver/databricks"]


@pytest.mark.parametrize("warehouse", [None, "wh-1"], ids=["rest-lineage", "system-tables"])
def test_databricks_unity_catalog(http, company, monkeypatch, warehouse):
    truth, h = company
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-harness-token")
    load(http, h)
    assert http.put("/api/v1/sources/databricks", json={"category": "catalog"}).status_code == 200
    args = [*API, *DATABRICKS, *(["--warehouse", warehouse] if warehouse else [])]
    assert main(args, http=http, source_http=h) == 0
    dps = {d["id"]: d for d in http.get("/api/v1/data-products", params={"source": "databricks"}).json()}
    assert set(dps) == set(truth.assets)
    feeds = {
        (s, d)
        for s, e in dps.items()
        for d in (
            [e["feedsDataProducts"]]
            if isinstance(e.get("feedsDataProducts"), str)
            else e.get("feedsDataProducts") or []
        )
    }
    assert feeds == set(truth.lineage)
    assert all(
        e["catalogUrn"] == f"urn:li:dataset:(urn:li:dataPlatform:databricks,{i},PROD)" for i, e in dps.items()
    )
    key = h.get("/harness/answer-key").json()["defects"]
    group_owned = {d["about"]["asset"] for d in key if d["kind"] == "owner-is-group"}
    assert group_owned and not any(dps[a].get("ownerRole") for a in group_owned)  # a group is not an owner
    owned = [a for a in truth.assets if a not in group_owned and dps[a].get("ownerRole")]
    assert owned
    if warehouse:  # classification tags only through SQL
        phi = [a for a, x in truth.assets.items() if any(c[2] == "phi" for c in x.columns)]
        assert phi and all(dps[a].get("sensitivity") == "Restricted" for a in phi)
        # Every column is reported for binding suggestions, with its tags; the one the
        # harness left untagged shows up as a column with no classification.
        cols = http.get("/api/v1/catalog/columns/reported", params={"source": "databricks"}).json()
        assert len(cols) == sum(len(x.columns) for x in truth.assets.values())
        untagged = {
            (d["about"]["asset"], d["about"]["column"])
            for d in key
            if d["kind"] == "untagged-sensitive-column"
        }
        for c in cols:
            table = c["dataset"].split(",")[1]
            if (table, c["fieldPath"]) in untagged:
                assert not any(t.startswith("urn:li:tag:classification:") for t in c["terms"])
    else:
        assert not any(e.get("sensitivity") for e in dps.values())


def test_dbt_and_databricks_describe_one_table(http, company, monkeypatch):
    """The two catalog sources give a table the same ID and URN, so the model holds one."""
    truth, h = company
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-harness-token")
    load(http, h)
    assert main([*API, *DATABRICKS], http=http, source_http=h) == 0
    assert main([*API, "dbt", "import", "--url", "http://testserver/dbt"], http=http, source_http=h) == 0
    assert {d["id"] for d in http.get("/api/v1/data-products").json()} == set(truth.assets)


def _ea_support(http, h, truth, monkeypatch, tool: list[str], source: str) -> tuple[dict, dict]:
    """Load the CMDB, then an EA tool; the supporting systems it linked, and what the truth says."""
    prof = __import__(f"harness.profiles.{truth.profile}", fromlist=["EA_SUPPORT"])
    if not getattr(prof, "EA_SUPPORT", None):
        pytest.skip("this harness maps no applications to capabilities")
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    load(http, h)
    snow = ["servicenow", "import", "--url", "http://testserver/servicenow"]
    assert main([*API, *snow], http=http, source_http=h) == 0
    assert main([*API, *tool], http=http, source_http=h) == 0
    by_name = {
        s["name"]: s["id"] for s in http.get("/api/v1/systems", params={"source": "servicenow"}).json()
    }
    want: dict[str, set] = {}
    for cap, sid in prof.EA_SUPPORT:
        want.setdefault(cap, set()).add(by_name[truth.systems[sid].name])
    caps = http.get("/api/v1/capabilities", params={"source": source}).json()
    got = {
        c["id"]: set(v if isinstance(v, list) else [v]) for c in caps if (v := c.get("supportedBySystems"))
    }
    return got, want


def test_servicenow_ea_links_capabilities_to_the_applications_that_provide_them(http, company, monkeypatch):
    truth, h = company
    got, want = _ea_support(
        http,
        h,
        truth,
        monkeypatch,
        ["servicenow", "ea", "--url", "http://testserver/servicenow"],
        "servicenow-ea",
    )
    assert got == want


def test_leanix_links_the_same_applications_except_one_entered_by_hand(http, company, monkeypatch):
    truth, h = company
    if "leanix" not in h.get("/harness/status").json()["systems"]:
        pytest.skip("this harness has no LeanIX")
    monkeypatch.setenv("LEANIX_API_TOKEN", "harness-leanix-api-token")
    got, want = _ea_support(
        http, h, truth, monkeypatch, ["leanix", "import", "--url", "http://testserver/leanix"], "leanix"
    )
    unsynced = [
        d["about"]
        for d in h.get("/harness/answer-key").json()["defects"]
        if d["kind"] == "application-unsynced"
    ]
    # The application entered by hand matches no system by external ID or name: left out (and
    # reported); every other link is the same as ServiceNow's.
    missing = {c for c in want if want[c] - got.get(c, set())}
    assert missing == {d["capability"] for d in unsynced}
    assert all(got.get(c, set()) <= want[c] for c in want)

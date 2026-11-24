"""M2 (share of facts sourced automatically) and M6 (change latency), from provenance."""

from __future__ import annotations

import test_decide
from test_decide import API, DANA, LEE

ctx = test_decide.ctx


def test_share_sourced_automatically_and_latency(ctx):
    client, _, _ = ctx
    before = client.get(f"{API}/measures/model").json()
    assert before["M2"]["value"] == 0  # the example is a workbook: all by hand
    assert client.put(f"{API}/sources/workday", json={"category": "hr"}).status_code == 200
    cs = client.post(f"{API}/changesets", json={"source": "workday"}).json()["id"]
    items = [
        {"type": "people", "entity": DANA, "observedAt": "2026-11-20T08:00:00+00:00"},
        {"type": "people", "entity": LEE},
    ]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    got = client.get(f"{API}/measures/model").json()
    wd = next(r for r in got["sources"] if r["source"] == "workday")
    assert wd["automatic"] and wd["category"] == "hr" and wd["entities"] == 2 and wd["fields"] >= 4
    assert 0 < got["M2"]["value"] < 1
    assert got["M2"]["automaticFields"] == wd["fields"]
    lat = got["M6"]["bySource"]["workday"]
    assert lat["claims"] == 2 and lat["p90"] >= lat["median"] >= 0

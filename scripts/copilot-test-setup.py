#!/usr/bin/env python3
"""Prepare a VeroMesh install for the Copilot Studio end-to-end test (docs/copilot-studio-test.md).

    python3 scripts/copilot-test-setup.py --url https://dev-ontology.veromesh.ai \\
        --dana-email <you> --lee-email <the second test user> --agent-client-id <invoice assistant app>

Loads the procure-to-pay worked example as source "copilot-test", with Dana's and Lee's
emails set to two real sign-ins and the invoice assistant's sign-in client ID set to its
Copilot app registration, then attests every unconfirmed fact as Dana (the AP manager),
so the decision check can rely on them. Running it again replaces the source and attests
what is new.

The token: VEROMESH_TOKEN if set, else `az account get-access-token` for the API's
access_as_user scope (--api-client-id, default the dev install's). The signed-in person
needs VeroMesh.admin (to load) and VeroMesh.steward (to attest) and must be Dana.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SOURCE = "copilot-test"
DEV_API_CLIENT_ID = "6a85ba9a-b165-4ea8-875c-136db8c7dbc6"  # VeroMesh Ontology API (dev)


def token(api_client_id: str) -> str:
    if os.environ.get("VEROMESH_TOKEN"):
        return os.environ["VEROMESH_TOKEN"]
    out = subprocess.run(
        ["az", "account", "get-access-token", "--scope", f"api://{api_client_id}/access_as_user",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit("Could not get a token from az:\n" + out.stderr.strip())
    return out.stdout.strip()


class Api:
    def __init__(self, base: str, tok: str):
        self.base, self.tok = base.rstrip("/"), tok

    def __call__(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.tok}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                text = r.read()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True)
    p.add_argument("--dana-email", required=True, help="your sign-in: you test as Dana, the AP manager")
    p.add_argument("--lee-email", required=True, help="a second sign-in: Lee, the Controller")
    p.add_argument("--agent-client-id", required=True, help="the invoice assistant's Copilot app registration")
    p.add_argument("--api-client-id", default=DEV_API_CLIENT_ID)
    a = p.parse_args()
    api = Api(a.url, token(a.api_client_id))

    me = api("GET", "/api/v1/me")
    print(f"signed in: {json.dumps(me)}")

    items = api("GET", "/api/v1/examples/procure-to-pay")["items"]
    for it in items:
        e = it["entity"]
        if it["type"] == "people" and e.get("id") == "DanaExample":
            e["email"] = a.dana_email
        elif it["type"] == "people" and e.get("id") == "LeeExample":
            e["email"] = a.lee_email
        elif it["type"] == "agents" and e.get("id") == "InvoiceAssistant":
            e["signInClientId"] = a.agent_client_id
    cs = api("POST", "/api/v1/changesets", {"source": SOURCE, "mode": "replace",
                                             "description": "Copilot Studio end-to-end test (worked example)"})["id"]
    api("POST", f"/api/v1/changesets/{cs}/items", {"items": items})
    report = api("POST", f"/api/v1/changesets/{cs}/validate")
    if not report.get("valid"):
        sys.exit(f"the example does not validate: {json.dumps(report, indent=2)}")
    done = api("POST", f"/api/v1/changesets/{cs}/commit")
    print(f"loaded {len(items)} entities as source {SOURCE} ({done.get('facts')} facts)")

    rows = api("GET", "/api/v1/attestations?status=unconfirmed&limit=500") or []
    for row in rows:
        api("POST", f"/api/v1/{row['type']}/{urllib.request.quote(row['id'])}/attestations",
            {"verdict": "attested", "role": "ApManager", "note": "Copilot Studio test setup"})
    print(f"attested {len(rows)} entities as Dana (AP manager)")

    check = api("POST", "/api/v1/decisions/check",
                {"action": "ApproveSupplierInvoice", "agent": "InvoiceAssistant", "amount": 800, "currency": "USD"})
    print(f"check, 800 USD by the invoice assistant: {check.get('outcome')}")
    for r in check.get("reasons") or []:
        print(f"  {r}")


if __name__ == "__main__":
    main()

"""Enforcement at a gateway (gap G3, option C): agents reach their tools through VeroMesh.

The decision check on its own is advisory: an agent could act without asking. Here the agent
signs in to VeroMesh's MCP gateway (``/gateway/mcp``) instead of to the systems it acts on.
The gateway offers the tools of the upstream MCP servers an administrator registered, and
every call to a tool mapped to a VeroMesh action goes through the decision check first, as
the agent, on the decision log:

- **allow**: the call goes to the upstream server at once; the outcome (done, with what the
  server answered, or not done when it failed) is logged against the decision.
- **refuse**: the call never leaves VeroMesh; the agent gets the reasons.
- **consult**: the call is **held** (a ``gateway`` entry on the decision log keeps it, so a
  restart or another copy of the service loses nothing) while the owners are asked, as for
  any decision (the steward screen, and a notice by email or a Teams card with Approve and
  Reject). Once they approve,
  the held call goes through, then or the next time the agent asks; a rejection discards it.
  The agent asks with ``gateway_status`` and the reference it was given.

Tools that only read can be passed through unchecked (``passThrough``); a tool neither mapped
nor passed through is not offered. Upstream servers are reached over MCP's Streamable HTTP
with an optional header whose value comes from an environment variable (secrets never sit in
the settings).

Settings (``gateway``, beside the usage settings; ``GET``/``PUT /api/v1/gateway/settings``)::

    {"targets": [{"name": "erp", "url": "https://erp-mcp.example.com/mcp",
                  "header": "Authorization", "secretEnv": "GATEWAY_ERP_TOKEN",
                  "tools": {"approve_invoice": {"action": "ApproveSupplierInvoice",
                                                "amount": "amount", "currency": "currency",
                                                "note": "invoice_id"}},
                  "passThrough": ["get_invoice"]}]}

In a tool mapping, ``amount``, ``currency``, ``about``, ``value`` and ``note`` name the
argument that carries each (``currencyValue`` gives a fixed currency instead).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

import anyio

from . import followup

log = logging.getLogger("veromesh.gateway")

SETTING = "gateway"
SEP = "__"  # a gateway tool's name: <target>__<tool>
STATUS_TOOL = "gateway_status"
SUBJECT = "veromesh-gateway"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
ARG_KEYS = ("amount", "currency", "about", "value", "note")

INSTRUCTIONS = """These tools act in the organization's systems through VeroMesh. Every consequential call is
checked against the authority of the person you act for before it reaches the system.
- If a call is allowed, it runs and you get the system's answer.
- If it is refused, it did not run: explain the reasons; never retry with a smaller amount to get under a limit.
- If it is held, the owners are being asked: tell your user, then call gateway_status with the reference
  until it says done (you then get the system's answer) or rejected."""


# --------------------------------------------------------------------------- settings


def check_settings(value: dict | None) -> dict:
    """The gateway settings, checked (ValueError says what is wrong)."""
    value = value or {}
    unknown = set(value) - {"targets"}
    if unknown:
        raise ValueError(f"There is no gateway setting {', '.join(sorted(unknown))}.")
    targets = value.get("targets") or []
    if not isinstance(targets, list):
        raise ValueError("targets is a list.")
    names = set()
    out = []
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("Each target is an object.")
        name = str(t.get("name") or "")
        if not NAME.match(name):
            raise ValueError(
                f"A target's name is lowercase letters, digits and '-' (up to 31): not '{name}'."
            )
        if name in names:
            raise ValueError(f"Two targets are called {name}.")
        names.add(name)
        url = str(t.get("url") or "")
        if not re.match(r"^https?://", url):
            raise ValueError(f"Target {name} needs the URL of its MCP server (http:// or https://).")
        if bool(t.get("header")) != bool(t.get("secretEnv")):
            raise ValueError(f"Target {name}: give both header and secretEnv, or neither.")
        tools = t.get("tools") or {}
        if not isinstance(tools, dict):
            raise ValueError(f"Target {name}: tools maps a tool's name to its action.")
        for tool, m in tools.items():
            if not isinstance(m, dict) or not m.get("action"):
                raise ValueError(f"Target {name}, tool {tool}: give the VeroMesh action it takes.")
            extra = set(m) - {"action", "currencyValue", *ARG_KEYS}
            if extra:
                raise ValueError(f"Target {name}, tool {tool}: unknown keys {', '.join(sorted(extra))}.")
        passes = t.get("passThrough") or []
        if not isinstance(passes, list) or set(passes) & set(tools):
            raise ValueError(f"Target {name}: passThrough lists tools not also mapped to an action.")
        out.append(
            {
                "name": name,
                "url": url,
                **({"header": t["header"], "secretEnv": t["secretEnv"]} if t.get("header") else {}),
                "tools": tools,
                "passThrough": list(passes),
            }
        )
    return {"targets": out}


# --------------------------------------------------------------------------- upstream servers


class Upstream(Protocol):
    async def list_tools(self) -> list[dict]:
        """Each tool: name, description, inputSchema."""
        ...

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """The server's answer: content (a list of MCP content blocks) and isError."""
        ...


class McpUpstream:
    """An upstream MCP server over Streamable HTTP."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> None:
        self.url, self.headers, self.timeout = url, headers or {}, timeout

    async def _session(self, fn):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        timeout = httpx.Timeout(self.timeout, read=self.timeout * 5)
        async with create_mcp_http_client(headers=self.headers, timeout=timeout) as http:
            async with streamable_http_client(self.url, http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await fn(session)

    async def list_tools(self) -> list[dict]:
        res = await self._session(lambda s: s.list_tools())
        return [t.model_dump(exclude_none=True) for t in res.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        res = await self._session(lambda s: s.call_tool(name, arguments))
        return {
            "content": [c.model_dump(exclude_none=True) for c in res.content],
            "isError": bool(res.isError),
        }


def upstream_for(target: dict) -> Upstream:
    headers = {}
    if target.get("header"):
        secret = os.environ.get(target["secretEnv"], "")
        if not secret:
            raise RuntimeError(
                f"{target['secretEnv']} is not set, so target {target['name']} cannot be reached."
            )
        headers[target["header"]] = secret
    return McpUpstream(target["url"], headers)


def text(*parts: str, error: bool = False, content: list | None = None) -> dict:
    return {"content": [*(content or []), *({"type": "text", "text": p} for p in parts)], "isError": error}


def _summary(result: dict) -> str:
    words = " ".join(str(c.get("text", "")) for c in result.get("content") or [] if c.get("type") == "text")
    return words[:400]


# --------------------------------------------------------------------------- the gateway


class Gateway:
    """``settings_store``: where the install keeps its settings; ``log``: the decision log;
    ``upstream``: a factory from a target's settings to its server (tests pass their own)."""

    def __init__(
        self, settings_store, decision_log, upstream: Callable[[dict], Upstream] = upstream_for
    ) -> None:
        self.kv, self.log, self.upstream = settings_store, decision_log, upstream
        self._swept = 0  # the background sweep's watermark (decision log seq)
        self.after_hold: Callable[[str], Any] | None = None  # tell the owners (the notices)

    def settings(self) -> dict:
        return check_settings(self.kv.setting(SETTING) or {})

    def put_settings(self, body: dict, *, by: str | None = None) -> dict:
        out = check_settings(body)
        self.kv.put_setting(SETTING, out)
        self.log.append(
            {
                "kind": "configuration",
                "setting": SETTING,
                "value": out,
                "outcome": "changed",
                "subject": by or "unknown",
            }
        )
        return out

    def _target(self, name: str) -> tuple[dict, str]:
        tname, sep, tool = name.partition(SEP)
        for t in self.settings()["targets"]:
            if sep and t["name"] == tname:
                return t, tool
        raise LookupError(f"There is no gateway tool {name}.")

    # ------------------------------------------------------------ listing

    async def tools(self) -> list[dict]:
        out: list[dict] = []
        for t in self.settings()["targets"]:
            offered = set(t["tools"]) | set(t["passThrough"])
            try:
                listed = await self.upstream(t).list_tools()
            except Exception as e:  # noqa: BLE001 — one unreachable server hides only its own tools
                log.warning("gateway target %s: tools not listed: %s", t["name"], e)
                continue
            for tool in listed:
                if tool["name"] not in offered:
                    continue
                checked = tool["name"] in t["tools"]
                note = " (checked by VeroMesh before it runs)" if checked else ""
                out.append(
                    {
                        "name": f"{t['name']}{SEP}{tool['name']}",
                        "description": (tool.get("description") or tool["name"]) + note,
                        "inputSchema": tool.get("inputSchema") or {"type": "object"},
                    }
                )
        out.append(
            {
                "name": STATUS_TOOL,
                "description": "Where a held call stands (the reference a held call returned): pending, "
                "done (with the system's answer) or rejected.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"reference": {"type": "string"}},
                    "required": ["reference"],
                },
            }
        )
        return out

    async def target_tools(self, name: str) -> dict:
        """What one target's server offers, for the Administration page: each tool with its
        arguments and how the gateway offers it (checked, read, or not at all)."""
        t = next((t for t in self.settings()["targets"] if t["name"] == name), None)
        if t is None:
            raise LookupError(f"There is no gateway target {name}.")
        try:
            listed = await self.upstream(t).list_tools()
            problem = None
        except Exception as e:  # noqa: BLE001 — shown on the page
            listed, problem = [], f"{t['url']} could not be reached: {e}"
        names = [x["name"] for x in listed]
        names += [x for x in [*t["tools"], *t["passThrough"]] if x not in names]
        by = {x["name"]: x for x in listed}
        out = []
        for n in names:
            x = by.get(n, {})
            props = list(((x.get("inputSchema") or {}).get("properties") or {}).keys())
            offered = "checked" if n in t["tools"] else "read" if n in t["passThrough"] else None
            out.append(
                {
                    "name": n,
                    "description": x.get("description") or "",
                    "arguments": props,
                    "offered": offered,
                    "listed": n in by,  # false: configured, but the server no longer offers it
                    **({"mapping": t["tools"][n]} if offered == "checked" else {}),
                }
            )
        return {"target": name, "tools": out, "problem": problem}

    # ------------------------------------------------------------ calling

    def check_in(self, target: dict, tool: str, arguments: dict) -> dict:
        """The decision check's request for this call."""
        m = target["tools"][tool]
        get = lambda k: arguments.get(m[k]) if m.get(k) else None  # noqa: E731
        body: dict[str, Any] = {"action": m["action"]}
        if (a := get("amount")) is not None:
            body["amount"] = str(a)
            body["currency"] = str(get("currency") or m.get("currencyValue") or "") or None
        for k in ("about", "value"):
            if (v := get(k)) is not None:
                body[k] = str(v)
        note = get("note")
        body["note"] = f"{target['name']}.{tool}" + (f": {note}" if note else "") + " (through the gateway)"
        return body

    async def call(self, name: str, arguments: dict, check: Callable[[dict], dict], subject: str) -> dict:
        """One tool call from an agent. ``check`` runs the decision check as that agent."""
        if name == STATUS_TOOL:
            return await self.status(str(arguments.get("reference") or ""), subject)
        target, tool = self._target(name)
        if tool in target["passThrough"]:
            return await self.upstream(target).call_tool(tool, arguments)
        if tool not in target["tools"]:
            raise LookupError(f"There is no gateway tool {name}.")
        decision = await anyio.to_thread.run_sync(check, self.check_in(target, tool, arguments))
        d = decision.get("decision") or decision
        did = d.get("id") or decision.get("id")
        outcome = decision.get("outcome") or d.get("outcome")
        reasons = "; ".join(str(r) for r in (decision.get("reasons") or d.get("reasons") or []))
        if outcome == "refuse":
            esc = decision.get("escalateTo") or d.get("escalateTo")
            names = [
                e.get("roleName") or e.get("role", "") if isinstance(e, dict) else str(e) for e in esc or []
            ]
            who = f" It goes to {', '.join(names)}." if names else ""
            return text(
                f"Refused by VeroMesh (decision {did}): {reasons}.{who} The call did not run.", error=True
            )
        call = {"target": target["name"], "tool": tool, "arguments": arguments}
        if outcome == "consult":
            await self._append(
                {
                    "kind": "gateway",
                    "outcome": "held",
                    "refersTo": did,
                    "subject": subject,
                    "agent": d.get("agent"),
                    "action": d.get("action"),
                    "gatewayCall": call,
                }
            )
            if self.after_hold is not None:
                try:
                    self.after_hold(did)
                except Exception:  # noqa: BLE001 — the hourly notice run tells them
                    log.exception("telling the owners of held call %s", did)
            asked = ", ".join(
                c.get("roleName") or c.get("role", "")
                for c in decision.get("consult") or d.get("consult") or []
            )
            return text(
                f"Held by VeroMesh: {asked or 'the owners'} must approve first. Reference: {did}. "
                f"Tell your user, then call {STATUS_TOOL} with this reference."
            )
        return await self._run(d, call, subject)

    async def _run(self, decision: dict, call: dict, subject: str) -> dict:
        """Send an allowed (or approved) call upstream and log what happened."""
        # Claim the call first: whichever copy of the service logs the first claim sends it,
        # so an approval seen by two copies at once never sends it twice.
        claim = await self._append(
            {
                "kind": "gateway",
                "outcome": "sending",
                "refersTo": decision["id"],
                "subject": subject,
                "agent": decision.get("agent"),
                "action": decision.get("action"),
                "gatewayCall": call,
            }
        )
        claims = [
            e
            for e in await anyio.to_thread.run_sync(followup.followups, self.log, decision["id"])
            if e["kind"] == "gateway" and e["outcome"] == "sending"
        ]
        if claims[0]["id"] != claim["id"]:
            return text(
                f"The call for decision {decision['id']} is already being sent; ask {STATUS_TOOL} for its answer."
            )
        target = next((t for t in self.settings()["targets"] if t["name"] == call["target"]), None)
        try:
            if target is None:
                raise RuntimeError(f"target {call['target']} is no longer configured")
            result = await self.upstream(target).call_tool(call["tool"], call["arguments"])
        except Exception as e:  # noqa: BLE001 — recorded as not done, and told to the agent
            await self._outcome(decision, "not_done", subject, f"gateway: failed: {e}")
            return text(
                f"Allowed by VeroMesh (decision {decision['id']}), but the system failed: {e}", error=True
            )
        done = "not_done" if result.get("isError") else "done"
        await self._outcome(decision, done, subject, f"gateway: {_summary(result)}")
        return {
            **result,
            "content": [
                *result.get("content", []),
                {"type": "text", "text": f"(VeroMesh decision {decision['id']}: allowed)"},
            ],
        }

    async def _append(self, entry: dict) -> dict:
        return await anyio.to_thread.run_sync(self.log.append, entry)

    async def _outcome(self, decision: dict, outcome: str, subject: str, note: str) -> None:
        def report():
            try:
                followup.report_outcome(self.log, decision, outcome=outcome, subject=subject, note=note[:500])
            except followup.InvalidEntry:
                pass  # already reported: another copy of the service sent it first

        await anyio.to_thread.run_sync(report)

    def _held(self, reference: str) -> tuple[dict, dict] | None:
        d = self.log.get(reference)
        if d is None or d.get("kind") != "decision":
            return None
        held = [e for e in followup.followups(self.log, reference) if e["kind"] == "gateway"]
        if not held or held[0]["outcome"] != "held":
            return None
        return d, held[0]

    def _sending(self, decision_id: str) -> bool:
        return any(
            e["kind"] == "gateway" and e["outcome"] == "sending"
            for e in followup.followups(self.log, decision_id)
        )

    async def status(self, reference: str, subject: str) -> dict:
        found = self._held(reference)
        if found is None:
            raise LookupError(f"No held gateway call with reference {reference}.")
        d, held = found
        st = followup.status_of(d, followup.followups(self.log, d["id"]))
        if st.get("done"):
            note = next(
                (e.get("note") for e in followup.followups(self.log, d["id"]) if e["kind"] == "outcome"), ""
            )
            return text(f"Done earlier ({st['done']}). {note or ''}".strip(), error=st["done"] != "done")
        if st["status"] in ("rejected", "refused"):
            return text(f"Rejected: the call did not run and will not ({st['status']}).", error=True)
        if self._sending(d["id"]):
            return text(f"Approved, and being sent now. Ask again shortly with reference {reference}.")
        if not st["mayAct"]:
            waiting = ", ".join(st.get("waitingFor") or []) or "the owners"
            return text(f"Still held: waiting for {waiting}. Ask again later with reference {reference}.")
        return await self._run(d, held["gatewayCall"], subject)

    def release(self, decision_id: str) -> bool:
        """Send the call held for this decision if its owners have now approved it (called
        after each answer, outside the event loop). Whether it was sent."""
        found = self._held(decision_id)
        if not found:
            return False
        d, held = found
        st = followup.status_of(d, followup.followups(self.log, d["id"]))
        if not st["mayAct"] or st.get("done") or self._sending(d["id"]):
            return False
        asyncio.run(self._run(d, held["gatewayCall"], SUBJECT))
        return True

    def release_due(self) -> int:
        """Send every held call whose owners have approved (the background sweep, for answers
        given in Teams or while this copy of the service was down). Held calls already settled
        (sent, rejected) are not read again."""
        done, after = 0, self._swept
        settled = True  # every held entry so far is settled: the watermark may move on
        while True:
            page = self.log.find(kind="gateway", after_seq=after, limit=500)
            for e in page:
                if e["outcome"] == "held":
                    done += self.release(e["refersTo"])
                    open_ = not self._settled(e["refersTo"])
                    settled = settled and not open_
                if settled:
                    self._swept = e["seq"]
            if len(page) < 500:
                return done
            after = page[-1]["seq"]

    def _settled(self, decision_id: str) -> bool:
        d = self.log.get(decision_id)
        if d is None:
            return True
        st = followup.status_of(d, followup.followups(self.log, decision_id))
        return bool(st.get("done")) or st["status"] in ("rejected", "refused") or self._sending(decision_id)

    def run(self) -> dict:
        """For the background job (usage.Job calls run())."""
        return {"released": self.release_due()}


# --------------------------------------------------------------------------- the MCP endpoint


def build_server(gateway_of: Callable[[], Gateway]):
    """The MCP server at /gateway/mcp. Each request carries the signed-in caller (put there by
    the endpoint, as for /mcp) as ``state["veromesh"]``: ``check`` (the decision check as that
    caller) and ``subject``."""
    from mcp import types
    from mcp.server.lowlevel import Server

    server = Server("VeroMesh gateway", instructions=INSTRUCTIONS)

    def state() -> dict:
        return server.request_context.request.scope["state"]["veromesh"]

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool.model_validate(t) for t in await gateway_of().tools()]

    @server.call_tool(validate_input=False)  # the upstream server checks its own arguments
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        st = state()
        try:
            result = await gateway_of().call(name, arguments or {}, st["check"], st["subject"])
        except LookupError as e:
            result = text(str(e), error=True)
        except Exception as e:  # noqa: BLE001 — the agent hears why; the log keeps the detail
            log.exception("gateway call %s failed", name)
            detail = getattr(e, "detail", None) or str(e)
            result = text(f"VeroMesh could not check this call: {detail}", error=True)
        return types.CallToolResult.model_validate(result)

    return server

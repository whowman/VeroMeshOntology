"""VeroMesh as an MCP server, for agents (Copilot Studio, Claude, any MCP client).

Served at ``/mcp`` on the API itself (Streamable HTTP, stateless, JSON responses),
behind the same sign-in as every other route: a bearer token from the customer's
identity provider, need-to-know answers, and the same checks. The tools are thin:
each calls the function behind a REST route, so the two can never disagree.

Tools
    who_am_i          the agent, the one person it acts for, and the authorities it holds
    check_action      before a consequential action: allow, consult or refuse (logged)
    impact_of_action  what an action affects, the materiality thresholds and the owners
    who_owns          the owner and steward roles of something, and the people filling them
    get_entity        one entity of the model, need-to-know
    decision_status   where a decision stands: may the agent act yet (consulted owners answered)?
    report_outcome    after acting (or not): done | not_done, for the log and the measures
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

INSTRUCTIONS = """VeroMesh is the organization's model of who owns what, who may decide what (with limits),
and what each action affects. You act for one person, within a bounded part of their authority.

Before any consequential action (approving, paying, changing, sending on someone's behalf):
1. Call check_action with the action ID and, where it has one, the amount and currency.
2. If the outcome is "allow", act, then tell the people under "inform", then call report_outcome.
3. If it is "consult", do not act yet: ask the people under "consult" and wait; call decision_status
   with the decision's id until mayAct is true (act, then report_outcome) or the status is rejected.
4. If it is "refuse", do not act. Explain the reasons; if "escalateTo" names someone, hand it to them.
Never act on an action the check refused, and never retry with a smaller amount to get under a limit.
Use who_am_i to see whom you act for and what you may do; impact_of_action and who_owns to explain."""


def _call(fn: Callable[[], Any]) -> Any:
    """Run a route function; turn its HTTP refusals into tool errors the agent can read."""
    try:
        return fn()
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    except (ValueError, LookupError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        if type(exc).__name__ == "NotFound":
            raise ToolError(str(exc)) from exc
        raise


def build(handlers: dict[str, Callable[..., Any]]) -> FastMCP:
    """``handlers`` take (state, **arguments); state is what the /mcp route put on the request."""
    mcp = FastMCP(
        "VeroMesh",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # Every request is signed in with a bearer token, and the API sits behind the
        # customer's ingress under its own host name: host-header checks would only refuse it.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def state(ctx: Context) -> dict:
        return ctx.request_context.request.state.veromesh

    @mcp.tool()
    def who_am_i(ctx: Context, purpose: str | None = None) -> dict:
        """Who you are in the organization's model: the agent, the one person you act for (their roles,
        not their personal details), and the authorities you hold with their limits and dates.
        purpose: why you need it (e.g. "checking who approves invoice INV-1"); it is logged."""
        return _call(lambda: handlers["who_am_i"](state(ctx), purpose=purpose))

    @mcp.tool()
    def check_action(
        ctx: Context,
        action: str,
        amount: str | None = None,
        currency: str | None = None,
        note: str | None = None,
        agent: str | None = None,
    ) -> dict:
        """Ask before any consequential action whether you may take it now, for the person you act for.
        action: the action's ID (e.g. ApproveSupplierInvoice); amount and currency when it has one
        (e.g. "3000", "USD"); note: what it is about (e.g. the invoice number), kept in the decision log.
        agent: leave it out; your sign-in says which agent you are. (Only an administrator testing a
        check, or an install with sign-in off, names the agent.)
        Returns outcome allow | consult | refuse, reasons, whom to consult (above your limit: the
        next authority up), whom to inform, and escalateTo when no authority covers the amount.
        The decision is logged."""
        return _call(
            lambda: handlers["check_action"](
                state(ctx), action=action, amount=amount, currency=currency, note=note, agent=agent
            )
        )

    @mcp.tool()
    def decision_status(ctx: Context, decision_id: str, purpose: str | None = None) -> dict:
        """Where a decision stands (the id check_action returned): allowed, pending (waitingFor the
        consulted roles), approved, rejected or refused; mayAct says whether you may act now.
        purpose: why you need it (e.g. "checking who approves invoice INV-1"); it is logged."""
        return _call(
            lambda: handlers["decision_status"](state(ctx), purpose=purpose, decision_id=decision_id)
        )

    @mcp.tool()
    def report_outcome(ctx: Context, decision_id: str, outcome: str, note: str | None = None) -> dict:
        """After acting on a decision you were allowed (or approved) to take, report outcome "done";
        if you did not act after all, "not_done". note: e.g. the payment reference."""
        return _call(
            lambda: handlers["report_outcome"](
                state(ctx), decision_id=decision_id, outcome=outcome, note=note
            )
        )

    @mcp.tool()
    def impact_of_action(ctx: Context, action: str, purpose: str | None = None) -> dict:
        """What an action affects: the KPIs and capabilities it can move, the materiality threshold for
        each (below it, act and inform the owner; above it, consult first), and their owners.
        purpose: why you need it (e.g. "checking who approves invoice INV-1"); it is logged."""
        return _call(lambda: handlers["impact_of_action"](state(ctx), purpose=purpose, action=action))

    @mcp.tool()
    def who_owns(ctx: Context, type: str, id: str, purpose: str | None = None) -> dict:
        """The owner and steward roles of something in the model (a KPI, capability, process, system,
        data product, policy...) and the people who fill them. type is the plural type name, e.g. kpis.
        purpose: why you need it (e.g. "checking who approves invoice INV-1"); it is logged."""
        return _call(lambda: handlers["who_owns"](state(ctx), purpose=purpose, type_=type, rid=id))

    @mcp.tool()
    def get_entity(ctx: Context, type: str, id: str, purpose: str | None = None) -> dict:
        """One entity of the model as VeroMesh believes it now (need to know: no personal details).
        type is the plural type name: roles, people, authorities, actions, kpis, capabilities, ...
        purpose: why you need it (e.g. "checking who approves invoice INV-1"); it is logged."""
        return _call(lambda: handlers["get_entity"](state(ctx), purpose=purpose, type_=type, rid=id))

    @mcp.tool()
    def impact_of_data_change(ctx: Context, data_product: str, purpose: str | None = None) -> dict:
        """Before changing a data product (renaming a column, changing a definition): everything
        downstream of it (data products, KPIs, objectives, capabilities, agents) and the owner roles
        to tell, with the reasons. data_product: its ID.
        purpose: why you need it (e.g. "renaming invoice_amount"); it is logged."""
        return _call(
            lambda: handlers["impact_of_data_change"](state(ctx), purpose=purpose, data_product=data_product)
        )

    return mcp

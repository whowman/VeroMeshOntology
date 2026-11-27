"""The Teams channel for notices to owners (gap G9): an Adaptive Card from the VeroMesh bot.

Only the customer's Microsoft tenant is involved, not where VeroMesh runs: the bot is an
Azure Bot registration (free tier; Teams is a standard channel) that points Teams at this
service's public address, wherever it is hosted. Nothing here uses Azure hosting.

- **Installing.** A Teams administrator installs the VeroMesh app for owners and stewards
  with an app setup policy. When it is installed for someone, Teams tells the bot
  (``installationUpdate`` or ``conversationUpdate``). The service asks Teams for the
  person's email or user principal name and matches it to one person in the model, as
  sign-in does (``VEROMESH_PERSON_MATCH_FIELD``), then keeps the address of their chat.
- **Sending.** A notice becomes a card in that chat: what was recorded, in which meeting, by
  whom, until when they may object, with Confirm and Object for each fact and Confirm all.
  Someone the app is not installed for is ``Unreachable``, and the next channel (email) is
  tried.
- **Answering.** A press arrives as an ``adaptiveCard/action`` invoke. The request must carry
  a Bot Framework token for this bot (issuer, audience, signature, the service URL it names)
  and come from the customer's tenant; the person pressing must be the owner of each
  attestation, which must still be open. The answer is an ordinary attestation or dispute
  by that person, on the hash chain, noted as made in Teams. The card is replaced by one
  that shows where each fact now stands, so nothing can be pressed twice.
- **Closing.** When the objection window closes, the next run replaces open cards with
  "this stands".

Settings: ``VEROMESH_TEAMS_APP_ID`` (the bot's Entra application ID), ``VEROMESH_TEAMS_APP_PASSWORD``
(its client secret) and ``VEROMESH_TEAMS_TENANT_ID``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from . import meetings
from . import provenance as P
from .notify import Notice, Unreachable, _day, type_label

log = logging.getLogger("veromesh.teams")

BOT_ISSUER = "https://api.botframework.com"
BOT_OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_SCOPE = "https://api.botframework.com/.default"
LOGIN = "https://login.microsoftonline.com"
PEOPLE = "teams.people"  # person -> {serviceUrl, conversationId, aadObjectId, at}
CARDS = "teams.cards"  # "<meeting>|<person>" -> {activityId, open}
STANDING_TEXT = {
    "confirmed": "Confirmed by you",
    "disputed": "You objected",
    "stands": "The objection window closed; this stands",
}


class NotFromBotFramework(Exception):
    pass


class Teams:
    """The bot: inbound activities and the ``teams`` notice channel."""

    name = "teams"

    def __init__(
        self,
        settings,
        store,
        base: str,
        decision_log,
        settings_store,
        notifier,
        *,
        http: httpx.Client | None = None,
        key_for: Callable[[str], Any] | None = None,
    ) -> None:
        self.s, self.store, self.base, self.log = settings, store, base, decision_log
        self.kv, self.notifier = settings_store, notifier
        self.app_id, self.tenant = settings.teams_app_id, settings.teams_tenant_id
        self.http = http or httpx.Client(timeout=30)
        self._key_for = key_for  # tests: token -> public key
        self._jwks: jwt.PyJWKClient | None = None
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ the bot's own tokens

    def verify(self, authorization: str | None, activity: dict) -> dict:
        """The Bot Framework token on an inbound request, checked; raises NotFromBotFramework."""
        if not authorization or not authorization.startswith("Bearer "):
            raise NotFromBotFramework("No Bot Framework token.")
        tok = authorization[7:]
        try:
            if self._key_for is not None:
                key = self._key_for(tok)
            else:
                if self._jwks is None:
                    meta = self.http.get(BOT_OPENID).json()
                    self._jwks = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True, lifespan=3600)
                key = self._jwks.get_signing_key_from_jwt(tok).key
            claims = jwt.decode(
                tok, key, algorithms=["RS256"], audience=self.app_id, issuer=BOT_ISSUER, leeway=300
            )
        except (jwt.PyJWTError, KeyError, httpx.HTTPError) as e:
            raise NotFromBotFramework(f"Bot Framework token refused: {e}") from e
        if claims.get("serviceurl", claims.get("serviceUrl")) not in (None, activity.get("serviceUrl")):
            raise NotFromBotFramework("The token names another service URL.")
        tenant = (activity.get("channelData") or {}).get("tenant", {}).get("id") or (
            activity.get("conversation") or {}
        ).get("tenantId")
        if self.tenant and tenant != self.tenant:
            raise NotFromBotFramework("The activity is from another tenant.")
        return claims

    def _bearer(self) -> str:
        with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            r = self.http.post(
                f"{LOGIN}/{self.tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.app_id,
                    "client_secret": self.s.teams_app_password or "",
                    "scope": BOT_SCOPE,
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"the bot could not sign in ({r.status_code}): {r.text[:300]}")
            body = r.json()
            self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
            return self._token[0]

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        r = self.http.request(method, url, json=body, headers={"Authorization": f"Bearer {self._bearer()}"})
        if r.status_code >= 300:
            raise RuntimeError(f"Teams answered {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------ who is who

    def people(self) -> dict[str, dict]:
        return dict(self.kv.setting(PEOPLE) or {})

    def _person_for(self, activity: dict, *, refresh: bool = False) -> str | None:
        """The person in the model behind the Teams user who sent this activity."""
        oid = (activity.get("from") or {}).get("aadObjectId")
        if not refresh:
            for pid, ref in self.people().items():
                if oid and ref.get("aadObjectId") == oid:
                    return pid
        conv = (activity.get("conversation") or {}).get("id")
        member = self._call(
            "GET",
            f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/{conv}/members/{oid or activity['from']['id']}",
        )
        field = self.s.person_match_field
        value = (
            member.get("email") or member.get("userPrincipalName") if field == "email" else member.get(field)
        )
        return P.person_matching(self.store, field, value) if value else None

    def _remember(self, activity: dict) -> str | None:
        pid = self._person_for(activity, refresh=True)
        if pid is None:
            log.warning(
                "Teams user %s matches no one person in the model",
                (activity.get("from") or {}).get("aadObjectId"),
            )
            return None
        ppl = self.people()
        ppl[pid] = {
            "serviceUrl": activity["serviceUrl"],
            "conversationId": activity["conversation"]["id"],
            "aadObjectId": (activity.get("from") or {}).get("aadObjectId"),
            "at": activity.get("timestamp") or "",
        }
        self.kv.put_setting(PEOPLE, ppl)
        return pid

    def _forget(self, activity: dict) -> None:
        oid = (activity.get("from") or {}).get("aadObjectId")
        ppl = {k: v for k, v in self.people().items() if v.get("aadObjectId") != oid}
        self.kv.put_setting(PEOPLE, ppl)

    # ------------------------------------------------------------ inbound

    def handle(self, authorization: str | None, activity: dict) -> tuple[int, dict | None]:
        """One inbound activity: (HTTP status, body)."""
        self.verify(authorization, activity)
        kind = activity.get("type")
        personal = (activity.get("conversation") or {}).get("conversationType", "personal") == "personal"
        if kind == "installationUpdate":
            if activity.get("action", "").startswith("remove"):
                self._forget(activity)
            elif personal:
                self._welcome(activity, self._remember(activity))
            return 200, None
        if kind == "conversationUpdate":
            added = [m.get("id") for m in activity.get("membersAdded") or []]
            if personal and (activity.get("recipient") or {}).get("id") in added:
                self._welcome(activity, self._remember(activity))
            return 200, None
        if kind == "invoke" and activity.get("name") == "adaptiveCard/action":
            return 200, self._action(activity)
        if kind == "message":
            self._reply(
                activity,
                "I send you the facts a steward recorded for you in meetings, to confirm or object to. "
                + (
                    f"Everything else is on the steward screen: {self.notifier.link}"
                    if self.notifier.link
                    else ""
                ),
            )
        return 200, None

    def _reply(self, activity: dict, text: str) -> None:
        conv = activity["conversation"]["id"]
        self._call(
            "POST",
            f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/{conv}/activities",
            {"type": "message", "text": text},
        )

    def _welcome(self, activity: dict, pid: str | None) -> None:
        if pid is None:
            self._reply(
                activity,
                "VeroMesh could not match your Teams account to a person in the organizational model, "
                "so it cannot send you anything here. Ask a VeroMesh steward to check your email in the model.",
            )
        else:
            self._reply(
                activity,
                "VeroMesh will send you here the facts a steward records for you in meetings, "
                "so you can confirm them or object.",
            )

    def _action(self, activity: dict) -> dict:
        """A Confirm, Object or Confirm all press: attest or dispute as the person pressing."""
        action = (activity.get("value") or {}).get("action") or {}
        verb, data = action.get("verb"), action.get("data") or {}
        mid = data.get("meeting")
        pid = self._person_for(activity)
        if pid is None or not mid:
            return _message(400, "VeroMesh cannot tell who you are in the model, or which meeting this is.")
        rows = meetings.recorded(self.store, meeting=mid, person=pid)
        if verb == "confirmAll":
            chosen = [r for r in rows if r["standing"] == "open"]
            verdict = "attested"
        elif verb in ("confirm", "object"):
            chosen = [r for r in rows if r["attestation"] == data.get("attestation")]
            verdict = "attested" if verb == "confirm" else "disputed"
        else:
            return _message(400, f"Unknown action '{verb}'.")
        if not chosen:
            return _message(404, "That is no longer recorded for you.")
        note = (data.get("note") or "").strip()
        for r in chosen:
            if r["standing"] != "open":
                continue  # answered already, or the window closed: the card is just refreshed
            P.attest(
                self.store,
                self.base,
                r["type"],
                r["id"],
                verdict=verdict,
                attester=pid,
                role=r["role"],
                fields=r["fields"],
                note=(note + " " if note else "") + "(in Teams, answering a recorded attestation)",
                signed_in=True,
                owner_only=True,
                log=self.log,
            )
        card = self.card(self._notice(mid, pid))
        return {"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": card}

    def _notice(self, mid: str, pid: str, kind: str = "recorded") -> Notice:
        return self.notifier.build(kind, mid, pid, meetings.recorded(self.store, meeting=mid, person=pid))

    # ------------------------------------------------------------ the channel

    def send(self, notice: Notice) -> str:
        ref = self.people().get(notice.person)
        if ref is None:
            raise Unreachable("the VeroMesh app is not installed for them in Teams")
        full = self._notice(notice.meeting["id"], notice.person, notice.kind)
        url = f"{ref['serviceUrl'].rstrip('/')}/v3/conversations/{ref['conversationId']}/activities"
        body = {"type": "message", "attachments": [_attachment(self.card(full))], "summary": _summary(full)}
        made = self._call("POST", url, body)
        cards = dict(self.kv.setting(CARDS) or {})
        old = cards.get(f"{notice.meeting['id']}|{notice.person}")
        if old and notice.kind == "reminder":
            self._replace(ref, old["activityId"], full)  # the first card no longer has live buttons
        cards[f"{notice.meeting['id']}|{notice.person}"] = {"activityId": made.get("id"), "open": True}
        self.kv.put_setting(CARDS, cards)
        return f"card sent in Teams ({made.get('id')})"

    def _replace(self, ref: dict, activity_id: str, notice: Notice, *, live: bool = False) -> None:
        url = f"{ref['serviceUrl'].rstrip('/')}/v3/conversations/{ref['conversationId']}/activities/{activity_id}"
        self._call(
            "PUT", url, {"type": "message", "attachments": [_attachment(self.card(notice, live=live))]}
        )

    def sweep(self) -> int:
        """Replace the cards whose facts are all answered or past their window."""
        cards = dict(self.kv.setting(CARDS) or {})
        ppl = self.people()
        done = 0
        for key, c in list(cards.items()):
            if not c.get("open"):
                continue
            mid, pid = key.split("|", 1)
            notice = self._notice(mid, pid)
            if any(i.standing == "open" for i in notice.items) or pid not in ppl:
                continue
            try:
                self._replace(ppl[pid], c["activityId"], notice)
            except Exception as e:  # noqa: BLE001 — tried again at the next run
                log.warning("could not update the Teams card for %s: %s", key, e)
                continue
            cards[key] = {**c, "open": False}
            done += 1
        self.kv.put_setting(CARDS, cards)
        return done

    # ------------------------------------------------------------ the card

    def card(self, n: Notice, *, live: bool = True) -> dict:
        """The Adaptive Card (1.4, for Action.Execute): one container per fact, with its
        buttons while it is open, or where it now stands."""
        m = n.meeting
        body: list[dict] = [
            {
                "type": "TextBlock",
                "text": "Reminder: facts recorded for you"
                if n.kind == "reminder"
                else "Facts recorded for you",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "text": f"{n.recorded_by_name} recorded in **{m['name']}** ({_day(m['heldOn'])}) that you confirmed "
                f"these. They count now. If any is wrong, object by **{_day(n.objection_until)}**; after that they stand.",
            },
        ]
        open_any = False
        for it in n.items:
            facts = [
                {"title": label, "value": value if value is not None else "(recorded)"}
                for label, value in it.fields
            ]
            if it.hidden:
                facts.append({"title": "Not shown here", "value": ", ".join(it.hidden)})
            part: list[dict] = [
                {
                    "type": "TextBlock",
                    "text": f"**{it.name}** ({type_label(it.type)}), as {it.role_name}",
                    "wrap": True,
                },
                {"type": "FactSet", "facts": facts},
            ]
            if it.standing == "open" and live:
                open_any = True
                part.append(
                    {
                        "type": "ActionSet",
                        "actions": [
                            _execute("Confirm", "confirm", m["id"], it.attestation, "positive"),
                            _execute("Object", "object", m["id"], it.attestation, "destructive"),
                        ],
                    }
                )
            else:
                part.append(
                    {
                        "type": "TextBlock",
                        "text": STANDING_TEXT.get(it.standing, f"Open until {_day(n.objection_until)}"),
                        "color": "Attention" if it.standing == "disputed" else "Good",
                        "weight": "Bolder",
                    }
                )
            body.append({"type": "Container", "separator": True, "spacing": "Medium", "items": part})
        actions = []
        if open_any:
            body.append(
                {
                    "type": "Input.Text",
                    "id": "note",
                    "placeholder": "Note (optional; say what is wrong if you object)",
                }
            )
            if sum(1 for i in n.items if i.standing == "open") > 1:
                actions.append(_execute("Confirm all", "confirmAll", m["id"], None, "positive"))
        if n.link:
            actions.append({"type": "Action.OpenUrl", "title": "Open the steward screen", "url": n.link})
        out = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": body,
        }
        if actions:
            out["actions"] = actions
        return out


def _execute(title: str, verb: str, meeting: str, attestation: str | None, style: str) -> dict:
    data = {"meeting": meeting, **({"attestation": attestation} if attestation else {})}
    return {"type": "Action.Execute", "title": title, "verb": verb, "data": data, "style": style}


def _attachment(card: dict) -> dict:
    return {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}


def _summary(n: Notice) -> str:
    return f"{len(n.items)} fact(s) recorded for you in {n.meeting['name']}"


def _message(code: int, text: str) -> dict:
    return {
        "statusCode": code,
        "type": "application/vnd.microsoft.error",
        "value": {"code": "BadRequest", "message": text},
    }

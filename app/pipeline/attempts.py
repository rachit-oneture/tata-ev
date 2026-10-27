"""Group parsed events into charging attempts.

An attempt is one try to charge on one connector: it starts at a RemoteStart (or a
local StartTransaction) and ends when the connector reports Available again, the
connection drops, or a new attempt begins on the same connector.

Phase rule (the only definition used anywhere):
    after_start   an accepted transaction, Charging status or session stop was seen
    before_start  the log does not confirm that a session began

Order in the log is authoritative. Timestamps are only used for durations and for
choosing which earlier events count as "context before" the attempt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .lossless import Event

CONTEXT_WINDOW_SECONDS = 300


def when(event: Event) -> datetime | None:
    try:
        return datetime.fromisoformat(event.ts) if event.ts else None
    except ValueError:
        return None


def connector_of(event: Event) -> int | None:
    value = event.get("connector_id")
    return int(value) if value is not None and value.isdigit() else None


def outcome_of(event: Event) -> str | None:
    """Accepted / Rejected / ... for response-style lines."""
    for name in ("result", "status", "text"):
        value = event.get(name)
        if value and value.split()[0] in {"Accepted", "Rejected", "Blocked", "Expired", "Invalid",
                                          "ConcurrentTx", "Pending", "Failed", "Registration"}:
            return value
    return None


@dataclass
class Attempt:
    number: int
    connector: int | None
    trigger: str                      # remote_start | local_start | implicit
    id_tag: str | None = None
    transaction_id: str | None = None
    events: list[Event] = field(default_factory=list)
    # Connector-0 / charger-wide reports that arrived while this attempt was open.
    # Kept apart because they are not tied to this connector.
    charger_events: list[Event] = field(default_factory=list)
    context_before: list[Event] = field(default_factory=list)
    closed: bool = False
    close_reason: str | None = None   # available | connection_lost | next_attempt | end_of_log
    charger: str | None = None

    @property
    def phase(self) -> str:
        confirmed = any(
            (e.action == "StartTransaction" and e.direction == "response" and outcome_of(e) == "Accepted")
            or (e.action == "StatusNotification" and e.get("status") == "Charging")
            or e.action in ("StopTransaction", "TransactionStopped")
            for e in self.events
        )
        return "after_start" if confirmed else "before_start"

    def first(self, action: str, direction: str | None = None) -> Event | None:
        for e in self.events:
            if e.action == action and (direction is None or e.direction == direction):
                return e
        return None


def charger_of(event: Event) -> str | None:
    value = event.get("identifier") or event.get("charger")
    return value if value not in (None, "", "null", "None") else None


def group(events: list[Event]) -> tuple[list[Attempt], list[Event]]:
    """Keep charger identities separate before correlating connectors or transactions.

    Unnamed lines inherit the identity only when the input names one charger.
    In mixed input they stay in a separate, unknown scope rather than assigning
    faults, connection changes or responses to an arbitrary named charger.
    """
    names = {name for e in events if (name := charger_of(e)) is not None}
    default = next(iter(names)) if len(names) == 1 else None
    scopes: dict[str | None, list[Event]] = {}
    owner_by_connector: dict[int, str] = {}
    for event in events:
        charger = charger_of(event)
        connector = connector_of(event)
        if charger is not None and connector is not None:
            owner_by_connector[connector] = charger
        # Command request/response lines in some CSMS exports omit the charger
        # id. They can inherit the most recently identified charger on that
        # connector. Fault/status lines never inherit identity: a mixed export
        # could otherwise attribute a fault to the wrong physical charger.
        if (charger is None and default is None and connector is not None
                and event.action in ("RemoteStartTransaction", "RemoteStopTransaction", "Authorize", "Authorization")):
            charger = owner_by_connector.get(connector)
        scopes.setdefault(charger or default, []).append(event)
    attempts, loose = [], []
    for charger, scoped_events in scopes.items():
        scoped_attempts, scoped_loose = _group_one(scoped_events)
        for att in scoped_attempts:
            att.charger = charger
        attempts.extend(scoped_attempts)
        loose.extend(scoped_loose)
    attempts.sort(key=lambda a: a.events[0].line_no or 0)
    for number, att in enumerate(attempts, 1):
        att.number = number
    loose.sort(key=lambda e: e.line_no or 0)
    return attempts, loose


def _group_one(events: list[Event]) -> tuple[list[Attempt], list[Event]]:
    """Return (attempts, loose_events). Every event ends up in exactly one place
    (an attempt, or loose), except charger-level events that are attached to
    every attempt open at that moment."""
    attempts: list[Attempt] = []
    open_by_connector: dict[int, Attempt] = {}
    loose: list[Event] = []
    last_remote: Attempt | None = None

    def new_attempt(conn, trigger, id_tag=None) -> Attempt:
        previous = open_by_connector.get(conn) if conn is not None else None
        if previous is not None:
            previous.closed, previous.close_reason = True, "next_attempt"
        att = Attempt(len(attempts) + 1, conn, trigger, id_tag)
        attempts.append(att)
        if conn is not None:
            open_by_connector[conn] = att
        return att

    def find_by_transaction(tx: str | None) -> Attempt | None:
        if tx is None:
            return None
        for att in reversed(attempts):
            if att.transaction_id == tx:
                return att
        return None

    for e in events:
        conn = connector_of(e)
        act, direction = e.action, e.direction

        if act == "RemoteStartTransaction" and direction == "request":
            att = new_attempt(conn, "remote_start", e.get("id_tag"))
            att.events.append(e)
            last_remote = att

        elif act == "RemoteStartTransaction":
            target = last_remote if last_remote and not last_remote.closed else None
            (target.events if target else loose).append(e)

        elif act in ("Authorize", "Authorization"):
            tag = e.get("id_tag")
            target = next((a for a in reversed(attempts) if not a.closed and (tag is None or a.id_tag in (None, tag))), None)
            if target and target.id_tag is None:
                target.id_tag = tag
            (target.events if target else loose).append(e)

        elif act == "StartTransaction":
            tx = e.get("transaction_id")
            target = find_by_transaction(tx) if direction == "response" else None
            if target is None:
                target = open_by_connector.get(conn) if conn is not None else None
                if target is not None and (target.closed or (target.transaction_id and direction == "request")):
                    target = None
            if target is None:
                target = new_attempt(conn, "local_start", e.get("id_tag"))
            if tx and target.transaction_id is None:
                target.transaction_id = tx
            target.events.append(e)

        elif act in ("StopTransaction", "TransactionStopped", "MeterValue", "RemoteStopTransaction", "DataTransfer"):
            target = find_by_transaction(e.get("transaction_id")) or (open_by_connector.get(conn) if conn is not None else None)
            if target is None and act == "RemoteStopTransaction" and direction == "response":
                target = next((a for a in reversed(attempts) if not a.closed), None)
            if target is None and act == "DataTransfer":
                target = next((a for a in reversed(attempts) if not a.closed), None)
            if target and act == "DataTransfer" and target.transaction_id is None:
                tx = e.get("transaction_id")
                if tx:
                    target.transaction_id = tx
            (target.events if target else loose).append(e)

        elif act == "StatusNotification" and conn in (None, 0):
            open_now = [a for a in attempts if not a.closed]
            if open_now:
                for att in open_now:
                    att.charger_events.append(e)
            else:
                loose.append(e)

        elif act == "StatusNotification":
            target = open_by_connector.get(conn) if conn is not None else None
            if target is not None and not target.closed:
                target.events.append(e)
                if e.get("status") == "Available":
                    target.closed, target.close_reason = True, "available"
                    open_by_connector.pop(conn, None)
            else:
                loose.append(e)

        elif act in ("ConnectionLost/Disconnected", "ConnectionInitiated", "BootNotification"):
            open_now = [a for a in attempts if not a.closed]
            for att in open_now:
                att.events.append(e)
                if act != "ConnectionInitiated":
                    att.closed, att.close_reason = True, "connection_lost" if act.startswith("Connection") else "reboot"
            for conn_id in [c for c, a in open_by_connector.items() if a.closed]:
                open_by_connector.pop(conn_id)
            if not open_now:
                loose.append(e)

        else:
            target = open_by_connector.get(conn) if conn is not None else None
            (target.events if target and not target.closed else loose).append(e)

    for att in attempts:
        if not att.closed:
            att.close_reason = "end_of_log"

    # Context before: loose events shortly before each attempt starts.
    for att in attempts:
        start = when(att.events[0]) if att.events else None
        if start is None:
            continue
        for e in loose:
            t = when(e)
            if t is None or e.line_no is None or att.events[0].line_no is None:
                continue
            if e.line_no < att.events[0].line_no and 0 <= (start - t).total_seconds() <= CONTEXT_WINDOW_SECONDS:
                att.context_before.append(e)
    return attempts, loose

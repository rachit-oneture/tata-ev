"""Build the small, de-identified fact sheet that a model is allowed to explain.

Nothing here is a raw log line. Identifiers (charger id, ID tag, transaction id) are replaced by
placeholders, absolute dates/times are replaced by seconds since the attempt's first event, and
every event gets a short id (E1, E2, ...) the model must cite.
"""
from __future__ import annotations

import re
from datetime import datetime

MAX_FAMILIES = 3
MAX_PER_FAMILY = 5


def _clock(ts_raw: str) -> datetime | None:
    m = re.match(r"(\d\d)-(\d\d)-(\d{4}) (\d\d:\d\d:\d\d)", ts_raw or "")
    return datetime.strptime(f"{m[3]}-{m[2]}-{m[1]} {m[4]}", "%Y-%m-%d %H:%M:%S") if m else None


def secrets_of(attempt: dict) -> dict[str, str]:
    """original identifier -> placeholder"""
    out = {}
    for value, label in ((attempt.get("id_tag"), "TAG-1"), (attempt.get("transaction_id"), "TX-1"), (attempt.get("charger"), "CHARGER-1")):
        if value and value not in ("null", "None"):
            out[str(value)] = label
    return out


def _scrub(text: str, secrets: dict[str, str]) -> str:
    for original, label in secrets.items():
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(original)}(?![A-Za-z0-9])", label, text)
    return text


def build_facts(attempt: dict) -> tuple[dict, dict[str, str]]:
    secrets = secrets_of(attempt)
    timeline = attempt["timeline"]
    origin = next((_clock(t["time"]) for t in timeline if not t["context"]), None) or _clock(timeline[0]["time"])
    ids = {t["line"]: f"E{i}" for i, t in enumerate(timeline, 1)}

    events = []
    for t in timeline:
        when = _clock(t["time"])
        events.append({
            "id": ids[t["line"]],
            "t_seconds": int((when - origin).total_seconds()) if when and origin else None,
            "what": _scrub(t["text"], secrets),
            "abnormal": t["abnormal"],
            "before_attempt": t["context"],
            "charger_wide": t["charger_level"],
        })
    intervals = [{"from": a["id"], "to": b["id"], "seconds": b["t_seconds"] - a["t_seconds"]}
                 for a, b in zip(events, events[1:]) if a["t_seconds"] is not None and b["t_seconds"] is not None]
    if len(events) > 1 and events[0]["t_seconds"] is not None:
        intervals.append({"from": events[0]["id"], "to": events[-1]["id"], "seconds": events[-1]["t_seconds"] - events[0]["t_seconds"]})

    def signal(s):
        lk = s.get("lookup") or {}
        entry = (lk.get("entries") or [None])[0]
        return {
            "kind": s["kind"], "value": s["value"],
            "event": ids.get(s["line"]),
            "table_status": lk.get("status"),
            "table_description": entry["description"] if entry else None,
            "table_manufacturer": entry.get("manufacturer") if entry else None,
            "standard_meaning": s.get("meaning"),
        }

    a = attempt["assessment"]
    ca, st, df = a["cause_assessment"], a["stage_assessment"], a["documented_faults_to_check"]
    candidates = []
    for fam in df["families"][:MAX_FAMILIES]:
        for e in fam["entries"][:MAX_PER_FAMILY]:
            candidates.append({"code": e["code"], "description": e["description"], "family": fam["family"],
                               "protocol": e["protocol"], "contradicts": e["contradicts"], "caveats": e["caveats"]})

    facts = {
        "reported_vendor_id": attempt.get("reported_vendor_id"),
        "phase": attempt["phase"],
        "how_attempt_started": attempt["trigger"],
        "events": events,
        "key_intervals_seconds": intervals,
        "reported_signals": [signal(s) for s in attempt["signals"]],
        "reported_before_attempt": [signal(s) for s in attempt["signals_before_attempt"]],
        "cause_result": ca["result"],
        "reported_codes": ca.get("codes", []),
        "stage": {
            "ladder": [{"stage": s["stage"], "reached": s["reached"]} for s in st["ladder"]],
            "last_confirmed_stage": st["last_confirmed_stage"]["value"],
            "next_expected_stage": st["next_expected_stage"],
            "failure_window": st["failure_window"]["value"],
            "failure_window_certainty": st["failure_window"]["certainty"],
            "sub_stage_note": st["sub_stage"]["note"],
        },
        "observed": {k: v["value"] for k, v in a["observed"].items()},
        "checked_and_not_seen": [n["check"] for n in a["not_observed"] if not n["observed"]],
        "seen": [n["check"] for n in a["not_observed"] if n["observed"]],
        "likely_domain": (ca.get("likely_domain") or {}).get("value"),
        "candidate_faults_not_reported": candidates,
        "candidate_shared_contradictions": df.get("shared_contradictions", []),
        "connector_type_known": bool(a["context"]["connector_protocol"]["value"]),
    }
    return facts, secrets

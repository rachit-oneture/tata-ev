"""Turn parsed events + attempts into a plain-language report (no model involved).

Everything here is derived from the log lines and the supplied code tables. The
report separates three things on purpose:
  * observed   - what the log literally says, with line numbers
  * defined    - what a code table says a code means (with its confidence)
  * unknown    - what the log cannot tell (physical root cause)
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime

from .attempts import Attempt, charger_of, group, when
from .catalogue import lookup
from .glossary import (NON_CODE_VALUES, NORMAL_STOP_REASONS, OCPP_ERROR_CODES, SOURCE, STATUS_WORDS,
                       STOP_REASONS)
from .lossless import Event, parse_text
from .assessment import assess
from .symptoms import detect

CODE_LIKE = re.compile(r"^[0-9A-Fa-f]{1,7}$|^\d{1,3}(?:-\d+)+$")
REJECT_WORDS = {"Rejected", "Blocked", "Expired", "Invalid", "ConcurrentTx", "Failed"}


# ----------------------------------------------------------------- small helpers

def fmt_time(event: Event) -> str:
    return event.ts_raw or "time unknown"


def _seconds(a: Event | None, b: Event | None) -> float | None:
    ta, tb = (when(a) if a else None), (when(b) if b else None)
    return (tb - ta).total_seconds() if ta and tb else None


def human_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _result_word(event: Event) -> str | None:
    for name in ("result", "text", "status"):
        value = event.get(name)
        if value:
            for word in value.split():
                if word in {"Accepted", "Rejected", "Blocked", "Expired", "Invalid", "ConcurrentTx", "Pending", "Failed"}:
                    return word
    return None


def _vendor_map(events: list[Event]) -> dict[str, str | None]:
    """charger id -> vendor, only when the log names exactly one vendor for it."""
    seen: dict[str, set[str]] = defaultdict(set)
    for e in events:
        charger = e.get("identifier") or e.get("charger")
        vendor = e.get("vendor_id")
        if charger and vendor and vendor not in NON_CODE_VALUES:
            seen[charger].add(vendor)
    return {c: (next(iter(v)) if len(v) == 1 else None) for c, v in seen.items()}


def _charger_of(events: list[Event]) -> str | None:
    counts = Counter(e.get("identifier") or e.get("charger") for e in events)
    counts.pop(None, None)
    counts.pop("null", None)
    return counts.most_common(1)[0][0] if counts else None


def _case_seconds(start: str | None, problem: str | None) -> int | None:
    try:
        fmt = "%d-%m-%Y %H:%M:%S IST"
        return int((datetime.strptime(problem or "", fmt) - datetime.strptime(start or "", fmt)).total_seconds())
    except ValueError:
        return None


def device_context(events: list[Event]) -> list[dict]:
    """Charger identity as it was reported by a BootNotification request.

    This is deliberately kept separate from OCPP vendorId: the former is boot metadata,
    while the latter is a field on an individual status report.
    """
    seen: set[tuple] = set()
    out = []
    for event in events:
        if event.action != "BootNotification" or event.direction != "request":
            continue
        vendor, model, firmware = (event.get(name) for name in
                                   ("charge_point_vendor", "charge_point_model", "firmware_version"))
        if not any((vendor, model, firmware)):
            continue
        item = (charger_of(event), vendor, model, firmware)
        if item in seen:
            continue
        seen.add(item)
        out.append({"line": event.line_no, "charger": charger_of(event),
                    "vendor": vendor, "model": model, "firmware": firmware})
    return out


def case_assessment(attempts: list[dict], other_signals: list[dict], devices: list[dict] | None = None) -> dict | None:
    """Evidence-bound diagnosis for the whole pasted case, above individual attempts."""
    affected = [a for a in attempts if a["verdict"] in ("problem", "symptom_only")]
    if not affected:
        return None
    ocpp = Counter(s["value"] for a in affected for s in a["signals"] if s["kind"] == "ocpp_error")
    vendors = Counter(s["value"] for a in affected for s in a["signals"] if s["kind"] == "vendor_message")
    messages = Counter(s["value"] for a in affected for s in a["signals"] if s["kind"] == "message")
    code, code_count = ocpp.most_common(1)[0] if ocpp else (None, 0)
    vendor_value = vendors.most_common(1)[0][0] if vendors else None
    message = messages.most_common(1)[0][0] if messages else None
    connectors = sorted({a["connector"] for a in affected if a["connector"] is not None})
    zero_energy = sum(a["meter"].get("change") == 0 for a in affected)
    elapsed = [_case_seconds(a["when"].get("session_started"), a["when"].get("first_problem")) for a in affected]
    elapsed = [n for n in elapsed if n is not None and n >= 0]
    code_lines = sorted({s["line"] for a in affected for s in a["signals"] if s["kind"] == "ocpp_error" and s["value"] == code}) if code else []
    repeated = code is not None and code_count == len(affected)
    vehicle_pause = all(a["no_code_reported"]
                        and any(s["id"] == "ev_paused" for s in a["symptoms"])
                        and "SuspendedEVSE" not in a["assessment"]["observed"]["ocpp_statuses_in_attempt"]["value"]
                        for a in affected)
    meaning = OCPP_ERROR_CODES.get(code) if code else None
    if vehicle_pause:
        domain = "Vehicle not accepting energy (SuspendedEV)"
        root_limit = "This points toward the vehicle not accepting energy; the specific reason is not recorded."
    elif not repeated:
        domain = "multiple reported failure patterns"
        root_limit = "No single reported error covers every affected attempt, so the physical root cause must be assessed attempt by attempt."
    elif code == "EVCommunicationError":
        domain = "EV ↔ charger communication / handshake"
        root_limit = ("The log establishes a communication failure, but cannot distinguish vehicle BMS behaviour, connector or communication-path hardware, compatibility, or charger software. Use the charger’s internal protocol logs and vehicle-side diagnostics to isolate it.")
    else:
        domain = "reported charger / vehicle interaction"
        root_limit = "The log identifies the reported failure, but does not establish its physical root cause."
    immediate = (f"The charger reported {code}" + (f" — {meaning}" if meaning else "") if repeated else
                 "The log contains multiple affected attempts without one common reported error")
    if vehicle_pause:
        immediate = ("The charger reported no fault and identified a vehicle-side pause (SuspendedEV)."
                     if all(a.get("suspension_no_error") for a in affected) else
                     "The charger identified a vehicle-side pause (SuspendedEV); no fault code was reported.")
    elif not ocpp and all(a["no_code_reported"] for a in affected):
        immediate = "A charging problem was observed; no fault code was reported."
        domain = "Charging did not progress as expected"
        root_limit = "The log records the behaviour, but does not identify the specific reason."
    if repeated and message:
        immediate += f". Reported detail: {message}"
    if repeated and vendor_value:
        immediate += f". Vendor error-field value: {vendor_value}"
    evidence = []
    if repeated:
        evidence.append(f"{code} was reported in all {len(affected)} affected attempt(s) (lines {', '.join(map(str, code_lines))}).")
    if vehicle_pause:
        evidence.append("SuspendedEV indicates that the vehicle was not taking energy, according to the charger's status report.")
    if zero_energy:
        evidence.append(f"No energy was delivered in {zero_energy} of {len(affected)} affected attempt(s).")
    if elapsed:
        span = str(elapsed[0]) if len(set(elapsed)) == 1 else f"{min(elapsed)}–{max(elapsed)}"
        evidence.append(f"The first reported problem appeared {span} s after session start in the affected attempt(s).")
    if repeated and len(connectors) > 1:
        evidence.append(f"The same reported failure occurred on connectors {', '.join(map(str, connectors))}; one connector alone is therefore a less complete explanation.")
    separate = []
    for signal in other_signals:
        if signal["kind"] == "ocpp_error":
            detail = next((x["value"] for x in other_signals if x["line"] == signal["line"] and x["kind"] in ("vendor_message", "message")), None)
            item = signal["value"] + (f" / {detail}" if detail else "")
            if item not in separate:
                separate.append(item)
    unknown_vendor_values: dict[str, list[int]] = {}
    for signal in (s for a in affected for s in a["signals"]):
        lookup_result = signal.get("lookup") or {}
        if signal["kind"] not in ("vendor_code", "vendor_message", "code_in_info") or lookup_result.get("status") != "not_found":
            continue
        unknown_vendor_values.setdefault(signal["value"], []).append(signal["line"])
    timing = f"{min(elapsed)}–{max(elapsed)} s" if len(set(elapsed)) > 1 else (f"{elapsed[0]} s" if elapsed else "not available")
    return {"immediate_reason": immediate, "fault_domain": domain, "root_cause_limit": root_limit,
            "evidence": evidence, "separate_reports": separate, "devices": devices or [],
            "unresolved_vendor_values": [{"value": value, "lines": lines}
                                        for value, lines in unknown_vendor_values.items()],
            "kpis": [{"label": "Affected attempts", "value": str(len(affected))},
                     {"label": "Reported error", "value": code if repeated else ("No fault code reported" if all(a["no_code_reported"] for a in affected) else "multiple patterns")},
                     {"label": "Energy delivered", "value": f"0 in {zero_energy}/{len(affected)}" if zero_energy else "not all zero"},
                     {"label": "First error after start", "value": timing}]}


# ----------------------------------------------------------------- signals

def signals_of(event: Event, vendor: str | None) -> list[dict]:
    """Everything abnormal that one event reports."""
    out: list[dict] = []

    def add(kind, value, **extra):
        out.append({"kind": kind, "value": value, "line": event.line_no, "time": fmt_time(event),
                    "connector": event.get("connector_id"), **extra})

    if event.action == "StatusNotification":
        ec, vc, info, status = (event.get(n) for n in ("error_code", "vendor_error_code", "info", "status"))
        vendor = event.get("vendor_id") if event.get("vendor_id") not in NON_CODE_VALUES else vendor
        if ec not in NON_CODE_VALUES:
            add("ocpp_error", ec, meaning=OCPP_ERROR_CODES.get(ec), meaning_source=SOURCE if ec in OCPP_ERROR_CODES else None,
                lookup=lookup(ec, vendor, field="ocpp_error"))
        if vc not in NON_CODE_VALUES and vc is not None:
            found = lookup(vc, vendor, field="vendorErrorCode", companion_text=info)
            if found:
                add("vendor_code" if CODE_LIKE.match(vc) else "vendor_message", vc, lookup=found)
        if info not in NON_CODE_VALUES and info is not None and info != vc:
            found = lookup(info, vendor, field="info")
            add("code_in_info" if CODE_LIKE.match(info) else "message", info, lookup=found)
        if status == "Faulted" and not out:
            add("faulted", "Faulted")
    elif event.action in ("StopTransaction", "TransactionStopped"):
        reason = event.get("reason")
        if reason and reason not in NORMAL_STOP_REASONS:
            add("stop_reason", reason, meaning=STOP_REASONS.get(reason), meaning_source=SOURCE if reason in STOP_REASONS else None,
                lookup=lookup(reason, vendor, field="stop_reason"))
    elif event.direction == "response" or event.action in ("Authorization", "StartTransaction", "RemoteStartTransaction"):
        word = _result_word(event)
        if word in REJECT_WORDS:
            add("rejected", f"{event.action} {word}")
    elif event.action == "ConnectionLost/Disconnected":
        add("connection_lost", "Connection lost")
    return out


def _merge(signals: list[dict]) -> list[dict]:
    """Merge repeats of the same signal; keep the first occurrence and all line numbers."""
    merged: dict[tuple, dict] = {}
    for s in signals:
        key = (s["kind"], s["value"], s.get("connector"))
        if key in merged:
            merged[key]["lines"].append(s["line"])
        else:
            merged[key] = {**s, "lines": [s["line"]]}
    return list(merged.values())


# ----------------------------------------------------------------- narrative

def describe(event: Event) -> tuple[str, bool]:
    """Plain-English line for the timeline, plus whether it is abnormal."""
    act, d = event.action, event.direction
    conn = event.get("connector_id")
    on = f" on connector {conn}" if conn and conn != "0" else ""
    tag = event.get("id_tag")
    if act == "RemoteStartTransaction":
        if d == "request":
            return f"Remote start requested{on}" + (f" for ID {tag}" if tag else ""), False
        word = _result_word(event)
        return f"Charger {word.lower() if word else 'answered'} the remote start", word in REJECT_WORDS
    if act in ("Authorize", "Authorization"):
        word = _result_word(event)
        if word:
            return f"ID {tag or ''} checked: {word}".replace("  ", " "), word in REJECT_WORDS
        return f"Authorization requested for ID {tag}", False
    if act == "StartTransaction":
        if d == "request":
            meter = event.get("meter_start_value")
            return f"Charging session starting{on}" + (f" (meter {meter})" if meter else ""), False
        word = _result_word(event)
        tx = event.get("transaction_id")
        return f"Session {'accepted' if word == 'Accepted' else (word or 'answered')}" + (f", transaction {tx}" if tx else ""), word in REJECT_WORDS
    if act in ("StopTransaction", "TransactionStopped"):
        reason = event.get("reason")
        meaning = STOP_REASONS.get(reason or "", None)
        text = "Session stopped" if act == "StopTransaction" else "Charger confirmed the session stopped"
        if reason:
            text += f": reason {reason}" + (f" ({meaning})" if meaning else "")
        return text, bool(reason and reason not in NORMAL_STOP_REASONS)
    if act == "StatusNotification":
        status, ec = event.get("status"), event.get("error_code")
        base = f"Connector {conn} is {STATUS_WORDS.get(status, status)}" if conn not in (None, "0") else f"Charger is {STATUS_WORDS.get(status, status)}"
        bits = []
        if ec not in NON_CODE_VALUES:
            bits.append(f"error {ec}" + (f" ({OCPP_ERROR_CODES[ec]})" if ec in OCPP_ERROR_CODES else ""))
        vc, info = event.get("vendor_error_code"), event.get("info")
        if vc not in NON_CODE_VALUES and vc is not None:
            bits.append(f"vendor code {vc}")
        if info not in NON_CODE_VALUES and info is not None and info != vc:
            bits.append(f"info \"{info}\"")
        return base + ("; " + ", ".join(bits) if bits else ""), bool(bits) or status == "Faulted"
    if act == "MeterValue":
        return f"Meter reading {event.get('meter_value')} {event.get('unit') or ''}".strip() + (f", battery {event.get('soc')}%" if event.get("soc") else ""), False
    if act == "ConnectionLost/Disconnected":
        return "Charger lost its connection to the server", True
    if act == "ConnectionInitiated":
        return "Charger connected to the server", False
    if act == "BootNotification":
        word = _result_word(event)
        return "Charger booted" + (f" ({word})" if word else ""), False
    if act == "RemoteStopTransaction":
        return "Remote stop " + ("requested" if d == "request" else (_result_word(event) or "answered")).lower(), False
    return event.label or event.action or "Unrecognised entry", False


# ----------------------------------------------------------------- one attempt

def build_attempt(att: Attempt, vendor: str | None, charger: str | None) -> dict:
    start_req = att.first("StartTransaction", "request")
    stop = att.first("StopTransaction") or att.first("TransactionStopped")
    own = [s for e in att.events for s in signals_of(e, vendor)]
    own += [{**s, "scope": "charger"} for e in att.charger_events for s in signals_of(e, vendor)]
    earlier = [s for e in att.context_before for s in signals_of(e, vendor)]
    signals, context_signals = _merge(own), _merge(earlier)

    meter_start = start_req.get("meter_start_value") if start_req else None
    meter_stop = stop.get("meter_stop_value") if stop else None
    delta = None
    if meter_start and meter_stop:
        try:
            delta = float(meter_stop) - float(meter_start)
            if not math.isfinite(delta):
                delta = None
        except ValueError:
            pass
    duration = _seconds(start_req, stop)

    first_event, last_event = att.events[0], att.events[-1]
    ordered = sorted(att.context_before + att.events + att.charger_events, key=lambda x: x.line_no or 0)
    timeline = [{"line": e.line_no, "time": fmt_time(e), "text": describe(e)[0], "abnormal": describe(e)[1],
                 "context": any(e is c for c in att.context_before),
                 "charger_level": any(e is c for c in att.charger_events)} for e in ordered]

    reached = []
    if att.first("RemoteStartTransaction", "response"):
        reached.append(f"remote start {(_result_word(att.first('RemoteStartTransaction', 'response')) or 'answered').lower()}")
    if att.first("Authorization") or att.first("Authorize"):
        a = att.first("Authorization") or att.first("Authorize")
        reached.append(f"ID check {(_result_word(a) or 'sent').lower()}")
    if any(e.get("status") == "Preparing" for e in att.events + att.context_before if e.get("connector_id") == str(att.connector)):
        reached.append("connector reached Preparing (vehicle plugged in)")
    if any(e.get("status") == "Charging" for e in att.events):
        reached.append("connector reached Charging")

    # verdict
    stop_reason = stop.get("reason") if stop else None
    start_resp = att.first("StartTransaction", "response")
    symptoms = detect(att, start_resp, stop, delta)
    real_symptoms = [s for s in symptoms if s["id"] != "ended_by_command"]
    if signals:
        verdict = "problem"
    elif real_symptoms:
        verdict = "symptom_only"
    elif stop and stop_reason in NORMAL_STOP_REASONS:
        verdict = "ended_normally"
    else:
        verdict = "unclear"

    primary = next((s for s in signals if s["kind"] in ("vendor_code", "code_in_info")), None) \
        or next((s for s in signals if s["kind"] in ("ocpp_error", "vendor_message", "message")), None) \
        or (signals[0] if signals else None)

    assessment = assess(att, signals, vendor, charger, delta, stop_reason, verdict)
    earliest = min(signals, key=lambda x: x["line"] or 0) if signals else None
    summary = _summary(att, primary, signals, stop, stop_reason, duration, delta, reached, verdict, real_symptoms)
    how = _how(att, earliest, stop, timeline, reached, real_symptoms, signals)
    after = att.phase == "after_start"
    return {
        "number": att.number,
        "connector": att.connector,
        "charger": charger,
        "reported_vendor_id": vendor,
        "id_tag": att.id_tag,
        "transaction_id": att.transaction_id,
        "trigger": att.trigger,
        "phase": att.phase,
        "phase_label": "After start" if after else "Before start",
        "phase_basis": ("The log confirms a session through an accepted StartTransaction, Charging status or session stop."
                        if after else "The log does not confirm that a charging session began; a start request alone is insufficient."),
        "verdict": verdict,
        "summary": summary,
        "how": how,
        "assessment": assessment,
        "no_code_reported": not signals,
        "suspension_no_error": any(e.get("status") == "SuspendedEV" and e.get("error_code") == "NoError"
                                   for e in att.events if e.action == "StatusNotification"),
        "symptoms": symptoms,
        "code_note": ("The charger reported no error code, and none can be derived from the log. "
                      "Statuses such as SuspendedEV are standard OCPP states, not manufacturer error codes."
                      if not signals and real_symptoms else None),
        "primary_signal": primary,
        "signals": signals,
        "signals_before_attempt": context_signals,
        "when": {
            "first_entry": fmt_time(first_event), "last_entry": fmt_time(last_event),
            "session_started": fmt_time(start_resp or start_req) if after and (start_resp or start_req) else None,
            "session_stopped": fmt_time(stop) if stop else None,
            "first_problem": (earliest["time"] if earliest else
                              next((x["time"] for x in sorted((x for x in real_symptoms if x["line"]), key=lambda x: x["line"])), None)),
            "duration": human_duration(duration),
            "ended_by": {"available": "connector returned to Available", "connection_lost": "connection to server was lost",
                         "next_attempt": "a new attempt began", "end_of_log": "end of the pasted log (no later entries)",
                         "reboot": "charger rebooted"}.get(att.close_reason or ""),
        },
        "meter": {"start": meter_start, "stop": meter_stop, "change": delta},
        "reached": reached,
        "timeline": timeline,
        "lines": [e.line_no for e in att.events + att.charger_events],
    }


def _summary(att, primary, signals, stop, stop_reason, duration, delta, reached, verdict, symptoms=()) -> dict:
    conn = f"connector {att.connector}" if att.connector is not None else "the charger"
    if att.phase == "before_start":
        happened = f"Charging never started on {conn}: the log does not confirm a charging session."
        if reached:
            happened += " It got as far as: " + ", ".join(reached) + "."
    else:
        happened = f"A session started on {conn}"
        if stop:
            happened += f" and the session stopped after {human_duration(duration) or 'an unknown time'}"
            if stop_reason:
                happened += f" (reason: {stop_reason})"
            happened += "."
        else:
            happened += ", but the log shows no stop."
        if delta is not None:
            happened += f" The meter moved by {delta:g}" + (" (almost no energy delivered)." if delta <= 100 else ".")

    if primary is None and symptoms:
        titles = "; ".join(x["title"][0].lower() + x["title"][1:] for x in symptoms[:3])
        error = ("The charger reported no error code. What the log shows instead: " + titles + ". "
                 "So the problem is read from behaviour, not from a code.")
    elif primary is None:
        error = ("No error code or fault was reported in this attempt." if verdict != "ended_normally"
                 else "No fault or error code was reported. The session was ended by a "
                      + ("remote command." if stop_reason == "Remote" else "stop at the charger."))
    else:
        kind, value = primary["kind"], primary["value"]
        if kind in ("vendor_code", "code_in_info"):
            lk = primary.get("lookup") or {}
            if lk.get("entries"):
                desc = lk["entries"][0]["description"]
                qualifier = {"documented": "per the manufacturer's table",
                             "unscoped_table": "per a code table that names no manufacturer, not confirmed for this charger",
                             "vendor_unknown": "per another manufacturer's table; the log does not name the maker",
                             "other_vendor": "only listed by a different manufacturer, probably does not apply",
                             "phrase_match": "text matches a table entry but the code differs"}.get(lk["status"], "unconfirmed")
                error = f"Reported error code {value}: \"{desc}\" ({qualifier})."
            else:
                error = f"Reported error code {value}, which is not in the supplied code tables, so its meaning is unknown."
        elif kind == "ocpp_error":
            error = f"Reported standard error {value}" + (f" ({primary['meaning']})." if primary.get("meaning") else ".")
        elif kind in ("message", "vendor_message"):
            error = f"The charger reported \"{value}\"."
        elif kind == "stop_reason":
            error = f"The session stopped with reason {value}" + (f" ({primary['meaning']})." if primary.get("meaning") else ".")
        else:
            error = f"Problem noted: {primary['value']}."
        extra = [s for s in signals if s is not primary]
        if extra:
            error += f" {len(extra)} other signal(s) were also reported."
    return {"what_happened": happened, "error": error}


def signal_text(sig: dict) -> str:
    kind, value = sig["kind"], sig["value"]
    if kind in ("vendor_code", "code_in_info"):
        lookup_result = sig.get("lookup") or {}
        entries = lookup_result.get("entries") or []
        if entries and lookup_result.get("status") == "documented":
            return f"error code {value} (table definition, model to confirm: \"{entries[0]['description']}\")"
        if entries:
            return f"error code {value} (table match is not confirmed for this charger)"
        return f"error code {value} (no definition in the supplied tables)"
    if kind == "ocpp_error":
        return f"standard error {value}" + (f" ({sig['meaning']})" if sig.get("meaning") else "")
    if kind == "stop_reason":
        return f"stop reason {value}" + (f" ({sig['meaning']})" if sig.get("meaning") else "")
    if kind in ("message", "vendor_message"):
        return f"message \"{value}\""
    return str(value)


def _how(att: Attempt, earliest: dict | None, stop: Event | None, timeline: list[dict], reached: list[str], symptoms=(), signals=()) -> list[str]:
    steps = []
    if reached:
        steps.append("Before any problem: " + ", ".join(reached) + ".")
    if earliest is None and symptoms:
        located = [x for x in symptoms if x["line"] is not None]
        first = min(located, key=lambda x: x["line"]) if located else symptoms[0]
        steps.append("The charger reported no error code, so the problem is read from what the log shows.")
        where = f", at {first['time']} (line {first['line']})" if first["line"] else ""
        steps.append(f"The first sign of trouble{where}: {first['title'][0].lower() + first['title'][1:]}. {first['detail']}")
        after = [t for t in timeline if first["line"] and (t["line"] or 0) > first["line"] and not t["context"]][:4]
        if after:
            steps.append("What the log shows next: " + "; ".join(f"{t['text']} (line {t['line']})" for t in after) + ".")
        if stop is not None and first["line"] and stop.line_no and stop.line_no > first["line"]:
            steps.append("The stop came after this, so it looks like a reaction to the stalled session. The log does not say why the stop was sent.")
        steps.append("The log does not show why this happened (for example a vehicle-side or charger-to-vehicle communication cause).")
        return steps
    if earliest is None:
        steps.append("No abnormal report appears in this attempt, so the log does not show how it failed.")
        return steps
    scope = " (a charger-wide report, not tied to this connector)" if earliest.get("scope") == "charger" else ""
    steps.append(f"The first problem, at {earliest['time']} (line {earliest['line']}), was {signal_text(earliest)}{scope}.")
    after = [t for t in timeline if (t["line"] or 0) > (earliest["line"] or 0) and not t["context"]][:4]
    if after:
        steps.append("What the log shows next: " + "; ".join(f"{t['text']} (line {t['line']})" for t in after) + ".")
    if stop is not None and stop.line_no is not None and earliest["line"] is not None:
        if earliest["kind"] == "stop_reason":
            # The first sign of trouble IS the stop (its reason is abnormal). Any other report is judged against it.
            steps.append("The stop itself is the first sign of trouble: it ended with an abnormal reason, so the session did not end normally.")
            later = [s for s in signals if s["kind"] != "stop_reason" and (s["line"] or 0) > stop.line_no]
            if later:
                first_line = min(s["line"] for s in later)
                group = sorted((s for s in later if s["line"] == first_line),
                               key=lambda s: s["kind"] not in ("vendor_code", "code_in_info"))   # vendor code first
                steps.append(f"The charger's own error report came afterwards, at {group[0]['time']} (line {first_line}): "
                             + "; ".join(signal_text(s) for s in group) + ". "
                             "Because it was recorded after the stop, the log cannot show whether it caused the stop or was raised because of it.")
        elif earliest["line"] < stop.line_no:
            steps.append("The problem was reported before the session stopped, so the stop may be a result of it. "
                         "The log alone cannot prove that.")
        elif earliest["line"] > stop.line_no:
            steps.append("The session stop was recorded before this problem report, so the report may describe a "
                         "consequence of the stop rather than its cause.")
    return steps


# ----------------------------------------------------------------- whole log

def analyze_text(text: str) -> dict:
    events, blanks = parse_text(text)
    if not events:
        raise ValueError("Paste at least one log line.")
    attempts, loose = group(events)
    vendors = _vendor_map(events)
    charger_names = {name for e in events if (name := charger_of(e)) is not None}
    charger_default = next(iter(charger_names)) if len(charger_names) == 1 else None
    built = []
    for att in attempts:
        charger = att.charger
        vendor = vendors.get(charger) if charger else None
        if vendor is None:
            names = {v for v in vendors.values() if v}
            if len(vendors) <= 1 and len(names) == 1:
                vendor = next(iter(names))
        built.append(build_attempt(att, vendor, charger))

    used_context = {e.line_no for att in attempts for e in att.context_before}
    attempt_lines = {n for a in built for n in a["lines"]}
    other = _merge([s for e in loose if e.line_no not in used_context
                    for s in signals_of(e, vendors.get(e.get("identifier") or "", None))])

    partial = [e for e in events if e.status != "parsed"]
    warnings = []
    if len(charger_names) > 1 and any(charger_of(e) is None for e in events):
        warnings.append("This log contains multiple chargers. Lines without a charger identity are kept separate; "
                        "include a charger identifier on each line or analyze one charger at a time.")
    if any(a["meter"]["start"] is not None and a["meter"]["stop"] is not None
           and a["meter"]["change"] is None for a in built):
        warnings.append("Some meter readings are invalid or non-finite. Energy delivered is unknown for those attempts; "
                        "the original readings are preserved.")
    if partial:
        warnings.append(f"{len(partial)} line(s) could not be fully understood and are shown as written "
                        f"(lines {', '.join(str(e.line_no) for e in partial[:10])}).")
    if not attempts:
        warnings.append("No charging attempt (remote start, local start or transaction) was found in these lines. "
                        "Paste the surrounding start/stop sequence for an attempt-level analysis.")
    if any(a["reported_vendor_id"] is None for a in built if a["signals"]):
        warnings.append("The log does not contain a vendorId for every reported code, so a code table cannot be narrowed by its reported vendor field.")

    first, last = events[0], events[-1]
    devices = device_context(events)
    return {
        "schema": "analysis-1",
        "engine": "rules",
        "summary": {
            "lines": len(events), "blank_lines": blanks, "attempts": len(built),
            "before_start": sum(a["phase"] == "before_start" for a in built),
            "after_start": sum(a["phase"] == "after_start" for a in built),
            "with_problem": sum(a["verdict"] in ("problem", "symptom_only") for a in built),
            "charger": charger_default, "from": fmt_time(first), "to": fmt_time(last),
        },
        "attempts": built,
        "devices": devices,
        "case_assessment": case_assessment(built, other, devices),
        "other_signals": other,
        "warnings": warnings,
        "lines": [{"line": e.line_no, "raw": e.raw, "status": e.status,
                   "explanation": describe(e)[0], "abnormal": describe(e)[1],
                   "attempt": next((a["number"] for a in built if e.line_no in set(a["lines"])), None),
                   "context_for": next((a["number"] for a in built if any(t["line"] == e.line_no and t["context"] for t in a["timeline"])), None)} for e in events],
    }

"""Find the problem when the charger reported NO error code.

A charger that raises no code leaves nothing to look up, and a code cannot be
recovered from a log that never contained one. What the log does contain is
behaviour: a vehicle that stopped taking energy, a meter that did not move, a
session that never reached Charging. This module names those symptoms from the
log lines, using the OCPP 1.6 meaning of each status, and nothing more.

No code is guessed from the symptoms. A trial that matched symptom wording to the code
tables returned unrelated entries (e.g. "Stop charging by RFID" for a SuspendedEV
stall), so that idea was dropped on purpose: a wrong code misleads a technician more
than an honest "no code was reported".
"""
from __future__ import annotations

from .attempts import Attempt, when
from .glossary import SOURCE

# Standard OCPP 1.6 status meanings, in plain words.
STATUS_MEANING = {
    "SuspendedEV": "the charger was offering energy but the vehicle was not taking any",
    "SuspendedEVSE": "the charger was not offering energy to the vehicle",
}

def seconds_between(a, b):
    ta, tb = (when(a) if a else None), (when(b) if b else None)
    return int(round((tb - ta).total_seconds())) if ta and tb else None


def detect(att: Attempt, start_resp, stop, meter_change) -> list[dict]:
    out: list[dict] = []
    started = att.phase == "after_start"

    def add(sid, title, detail, event=None, source=None):
        out.append({"id": sid, "title": title, "detail": detail, "line": event.line_no if event else None,
                    "time": (event.ts_raw if event else None), "source": source})

    statuses = [e for e in att.events if e.action == "StatusNotification"]
    first_start_line = start_resp.line_no if start_resp is not None else None

    if started:
        for e in statuses:
            st = e.get("status")
            if st in STATUS_MEANING and (first_start_line is None or (e.line_no or 0) > first_start_line):
                gap = seconds_between(start_resp, e)
                when_txt = f"{gap} s after the session was accepted" if gap is not None and gap >= 0 else "after the session started"
                sid = "ev_paused" if st == "SuspendedEV" else "evse_paused"
                add(sid, f"Connector went to {st} {when_txt}",
                    f"{st} means {STATUS_MEANING[st]}.", e, SOURCE)
                break

        if not any(e.get("status") == "Charging" for e in statuses):
            add("never_charging", "The connector never reported Charging",
                "A session was created, but the connector status never changed to Charging.", None, "log")

        if meter_change is not None:
            if meter_change == 0:
                add("no_energy", "No energy was delivered",
                    "The meter reading at stop equals the reading at start.", stop, "log")
            elif 0 < meter_change <= 100:
                add("little_energy", "Almost no energy was delivered",
                    f"The meter moved by only {meter_change:g}.", stop, "log")
    else:
        reached_preparing = any(e.get("status") == "Preparing" for e in att.events)
        returned_available = any(e.get("status") == "Available" for e in att.events)
        if not returned_available:
            add("no_session", "No charging session is confirmed",
                "The log contains no accepted start or other confirmation of a session" + (", although the connector reached Preparing." if reached_preparing else "."),
                att.events[0], "log")

    if stop is not None and out:
        reason = stop.get("reason")
        if reason in ("Remote", "Local"):
            add("ended_by_command", f"The session was ended by a {reason.lower()} stop",
                "A stop command is how the session ended. It is not a fault by itself, and the log does not say why the stop was sent.",
                stop, SOURCE)
    return out

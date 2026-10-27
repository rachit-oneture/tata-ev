"""Structured assessment of one attempt: what is observed, where progress stopped, what is not
known, and which documented faults a technician could check.

Principles
----------
* Stage (where progress stopped) and cause (why) are assessed separately. The stage can be
  well supported while the cause is "INSUFFICIENT_EVIDENCE". Abstaining is a valid result.
* No code is ever promoted to "the error" unless the charger reported it. Documented faults that
  fit the stage are listed for checking, each with evidence for AND against.
* No percentages. Certainty is a category: direct / strongly_inferred / weakly_inferred.
* Every fact states its provenance: log, protocol_knowledge, catalogue, supplied_logs_history.
* There is no rule of the form "status X => code Y". Candidates come from the catalogue text and
  are filtered by stage, vendor, history and negative evidence.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..config import ROOT
from .attempts import Attempt, outcome_of
from .catalogue import _load, _same_vendor
from .glossary import NON_CODE_VALUES

HISTORY_PATH = ROOT / "data" / "history.json"
REGISTRY_PATH = ROOT / "data" / "chargers.json"      # optional: model + connector types per charger

# ---- generic knowledge (not tied to any code) ---------------------------------------------
# Window -> fault families that can occur in it. Based on the order of a DC charging session
# (OCPP start, vehicle/charger preparation, power transfer), not on any single code.
WINDOW_FAMILIES = {
    "BEFORE_POWER_TRANSFER": ["EV_READINESS_TIMEOUT", "EV_COMMUNICATION", "EV_REPORTED_FAULT",
                              "CABLE_INSULATION_CHECK", "PRECHARGE"],
    "DURING_OR_AFTER_POWER_TRANSFER": ["CHARGING_PHASE", "EV_REPORTED_FAULT", "EV_COMMUNICATION"],
}
# Families whose faults a Delta charger would raise explicitly (see history) at any time.
UNBOUND_FAMILIES = ["HARDWARE_PROTECTION", "NETWORK_BACKEND", "USER_OR_BACKEND_STOP"]

# Keyword taxonomy over catalogue DESCRIPTIONS. Keyword-derived and unreviewed; an engineer
# should approve it before anything depends on it.
TAXONOMY = [
    ("CHARGING_PHASE", r"after charging|during charging"),
    ("EV_READINESS_TIMEOUT", r"timeout.*(readytocharge|charging enable|charging current request|contactor close|permission)|permission.*time ?out"),
    ("CABLE_INSULATION_CHECK", r"cablecheck|insulation"),
    ("PRECHARGE", r"precharge"),
    ("CHARGING_PHASE", r"currentdemand|bulkchargingcomplete|chargingcomplete|powerdelivery"),
    ("EV_REPORTED_FAULT", r"\bev error\b|come from chademo ev|ev other fault|error status noticed by ev|vehicle voltage|battery otp|position shift"),
    ("EV_COMMUNICATION", r"comm(unication)? with ev|\bcan\b|\bplc\b|v2g|supportedappprotocol|sessionsetup|servicediscovery|servicepaymentselection|contractauthentication|chargeparameterdiscovery|pilot"),
    ("USER_OR_BACKEND_STOP", r"rfid|local stop|server stop|emergency|ev request stop|reach stop"),
    ("NETWORK_BACKEND", r"3g|backend|server|network|heartbeat"),
    ("HARDWARE_PROTECTION", r"temperature|voltage|contactor|welding|rectifier|power module|smr|ground|leak|\bfan\b|door|surge|\bspd\b|breaker|fuse|overload|current"),
]


def family_of(description: str) -> str:
    text = description.lower()
    for family, pattern in TAXONOMY:
        if re.search(pattern, text):
            return family
    return "UNCLASSIFIED"


def protocol_of(description: str) -> str | None:
    text = description.lower()
    if "chademo" in text:
        return "CHAdeMO"
    if re.search(r"cablecheck|precharge|v2g|supportedappprotocol|sessionsetup|servicediscovery|currentdemand|powerdelivery|pilot|readytocharge|ev error - failed", text):
        return "CCS"
    return None


@lru_cache(maxsize=1)
def _history() -> dict:
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError):
        return {"codes": {}, "vendor_reporting": {}, "source": "unavailable"}


def _registry() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        return {}


def supplied_log_evidence(att: Attempt, vendor: str | None, charger: str | None,
                          observed_statuses: list[str]) -> dict:
    """Return strictly descriptive corpus context; it must not select a cause or code.

    The current log might itself be part of the stored corpus.  Therefore this deliberately says
    "the supplied corpus contains" instead of presenting the counts as prior independent cases.
    """
    hist = _history()
    source = hist.get("source", "the supplied logs")
    corpus = {"samples": hist.get("sample_count"), "events": hist.get("event_count"), "source": source}
    result = {"corpus": corpus, "exact_charger": None, "reported_vendor": None, "repeated_statuses": []}

    if charger:
        profile = hist.get("chargers", {}).get(charger)
        if profile:
            result["exact_charger"] = {
                "state": "seen_in_corpus",
                "charger": charger,
                "samples": len(profile.get("samples", [])),
                "statuses": profile.get("status_counts", {}),
                "status_error_count": profile.get("status_error_count", 0),
                "note": "These are recorded observations for this identifier, not a diagnosis or a confirmed hardware profile.",
            }
        else:
            result["exact_charger"] = {
                "state": "not_seen_in_corpus", "charger": charger,
                "note": "No matching charger identifier exists in the supplied reference logs.",
            }

    if not vendor:
        return result
    vendor_profile = hist.get("vendors", {}).get(vendor, {})
    reporting = hist.get("vendor_reporting", {}).get(vendor, {})
    asserted = reporting.get("asserted_with_error_code", 0)
    quiet = reporting.get("reported_without_error_code", 0)
    result["reported_vendor"] = {
        "name": vendor,
        "chargers": len(vendor_profile.get("chargers", [])),
        "samples": len(vendor_profile.get("samples", [])),
        "documented_code_reports": asserted + quiet,
        "code_reports_with_ocpp_error": asserted,
        "note": ("This is the OCPP vendorId field. Its match to a code-table manufacturer does not confirm this charger's physical make, model or cause."),
    }
    for status in observed_statuses:
        profile = hist.get("vendor_statuses", {}).get(vendor, {}).get(status)
        if profile and profile.get("occurrences", 0) > 1:
            result["repeated_statuses"].append({
                "status": status,
                "occurrences": profile["occurrences"],
                "chargers": len(profile.get("chargers", [])),
                "samples": len(profile.get("samples", [])),
                "note": "A repeated OCPP status is a shared observation, not a shared cause or an error code.",
            })
    return result


# ---- provenance helper ---------------------------------------------------------------------

def fact(value, source: str, lines=None, certainty: str = "direct", note: str | None = None) -> dict:
    out = {"value": value, "source": source, "certainty": certainty}
    if lines:
        out["lines"] = sorted({n for n in lines if n is not None})
    if note:
        out["note"] = note
    return out


# ---- stage engine --------------------------------------------------------------------------

def stage_assessment(att: Attempt, energy_change: float | None) -> dict:
    events = att.events
    ctx = att.context_before

    def lines(pred, pool=None):
        return [e.line_no for e in (pool if pool is not None else events) if pred(e)]

    plug = lines(lambda e: e.get("status") == "Preparing" and e.get("connector_id") == str(att.connector), events + ctx)
    cmd = lines(lambda e: e.action == "RemoteStartTransaction" and e.direction == "response" and outcome_of(e) == "Accepted")
    auth = lines(lambda e: e.action in ("Authorization", "Authorize") and outcome_of(e) == "Accepted")
    start = lines(lambda e: e.action == "StartTransaction" and e.direction == "response" and outcome_of(e) == "Accepted")
    charging = lines(lambda e: e.get("status") == "Charging")
    energy = [] if not energy_change or energy_change <= 0 else lines(lambda e: e.action in ("StopTransaction",))
    stop = lines(lambda e: e.action in ("StopTransaction", "TransactionStopped"))

    ladder = [
        ("PLUGGED_IN", plug),
        ("START_COMMAND_ACCEPTED", cmd if att.trigger == "remote_start" else None),
        ("AUTHORIZED", auth),
        ("TRANSACTION_STARTED", start),
        ("ENERGY_TRANSFER", charging + energy),
    ]
    stages = []
    for name, found in ladder:
        if found is None:
            continue
        stages.append({"stage": name, "reached": bool(found),
                       "evidence": fact(bool(found), "log", found, "direct")})

    reached = [s["stage"] for s in stages if s["reached"]]
    last = reached[-1] if reached else None
    order = [s["stage"] for s in stages]
    nxt = next((s for s in order[order.index(last) + 1:]), None) if last else (order[0] if order else None)

    closed = bool(stop) or att.close_reason in ("available", "connection_lost", "next_attempt", "reboot")
    if last == "ENERGY_TRANSFER":
        window, certainty = "DURING_OR_AFTER_POWER_TRANSFER", "direct"
    elif last is None:
        window, certainty = "BEFORE_POWER_TRANSFER", "weakly_inferred"
    else:
        window = "BEFORE_POWER_TRANSFER"
        # Absence of the next stage only means something if the log goes on past where it would be.
        certainty = "strongly_inferred" if closed else "weakly_inferred"

    return {
        "ladder": stages,
        "last_confirmed_stage": fact(last, "log", None, "direct" if last else "weakly_inferred"),
        "next_expected_stage": nxt,
        "failure_window": {"value": window, "source": "protocol_knowledge", "certainty": certainty,
                           "basis": (f"{last} was seen and {nxt} was not, and the attempt shows an ending."
                                     if last and nxt and closed else
                                     f"{nxt} was not seen, but the attempt has no clear ending, so the log may be cut off."
                                     if nxt else "All expected stages were seen.")},
        "sub_stage": {"value": "not determinable from OCPP logs", "source": "protocol_knowledge",
                      "certainty": "weakly_inferred",
                      "note": "OCPP does not show the vehicle/charger handshake steps (for example ReadyToCharge, cable check, precharge)."},
    }


# ---- observed / negative evidence ----------------------------------------------------------

def observed_block(att: Attempt, signals: list[dict], energy_change, stop_reason) -> dict:
    statuses = [e.get("status") for e in att.events if e.action == "StatusNotification"]
    uniq = list(dict.fromkeys(s for s in statuses if s))
    errors = sorted({e.get("error_code") for e in att.events + att.charger_events
                     if e.action == "StatusNotification" and e.get("error_code") not in (None, "null")})
    vendor_fault = any(s["kind"] in ("vendor_code", "code_in_info") for s in signals)
    return {
        "ocpp_statuses_in_attempt": fact(uniq, "log"),
        "ocpp_error_codes": fact(errors, "log"),
        "vendor_fault_reported": fact(vendor_fault, "log"),
        "energy_delivered": fact(energy_change, "log", note="meter stop minus meter start, unit as reported by the charger (normally Wh)"),
        "stop_reason": fact(stop_reason, "log"),
    }


def negative_evidence(att: Attempt, signals: list[dict], energy_change) -> list[dict]:
    pool = att.events + att.charger_events

    def check(label, observed_lines, detail_if_seen):
        seen = bool(observed_lines)
        return {"check": label, "observed": seen, "source": "log",
                "lines": sorted({n for n in observed_lines if n}) if seen else [],
                "detail": detail_if_seen if seen else "Not observed in this attempt."}

    refusals = {"Rejected", "Blocked", "Expired", "Invalid", "ConcurrentTx"}

    def rejected(action):
        return [e.line_no for e in att.events if e.action == action and e.direction == "response" and outcome_of(e) in refusals]

    return [
        check("Authorization rejected", [e.line_no for e in att.events if e.action in ("Authorization", "Authorize") and outcome_of(e) in refusals], "An authorization was refused."),
        check("Remote start rejected", rejected("RemoteStartTransaction"), "The remote start was refused."),
        check("StartTransaction rejected", rejected("StartTransaction"), "The session was refused."),
        check("Faulted status", [e.line_no for e in pool if e.get("status") == "Faulted"], "The connector reported Faulted."),
        check("OCPP error code other than NoError",
              [e.line_no for e in pool if e.action == "StatusNotification" and e.get("error_code") not in (None, "NoError", "null")],
              "The charger asserted an OCPP error code."),
        check("Vendor code reported", [s["line"] for s in signals if s["kind"] in ("vendor_code", "code_in_info")], "A vendor code was reported."),
        check("Connection lost during the attempt", [e.line_no for e in att.events if e.action == "ConnectionLost/Disconnected"], "The charger lost its server connection."),
        check("Meter moved (energy delivered)", [] if not energy_change or energy_change <= 0 else [att.events[-1].line_no], "The meter reading increased."),
    ]


# ---- candidates ----------------------------------------------------------------------------

def documented_faults(att: Attempt, vendor: str | None, window: str, negative: list[dict], connector_protocol: str | None) -> dict:
    empty = {"families": [], "shared_contradictions": []}
    if vendor is None:
        return {**empty, "note": "The log does not name the manufacturer, so vendor-scoped documented faults cannot be listed."}
    records, _, _ = _load()
    mine = [r for r in records if r.get("manufacturer") and _same_vendor(r["manufacturer"], vendor)]
    if not mine:
        return {**empty, "note": f"The supplied code tables contain no entries for {vendor}."}

    hist = _history()
    channel = hist.get("vendor_reporting", {}).get(vendor, {})
    asserted, quiet = channel.get("asserted_with_error_code", 0), channel.get("reported_without_error_code", 0)
    fault_asserted_now = next((n for n in negative if n["check"] == "OCPP error code other than NoError"), {}).get("observed", False)

    # Contradictions that apply to every candidate are stated once, not repeated per code.
    shared = []
    if not fault_asserted_now and asserted:
        shared.append(f"The charger asserted no fault in this attempt. In the supplied logs, {vendor} asserted its documented codes "
                      f"with an OCPP error code in {asserted} of {asserted + quiet} reports. (Only {asserted + quiet} reports exist, so this is thin evidence.)")

    wanted = WINDOW_FAMILIES.get(window, [])
    by_family: dict[str, list[dict]] = {}
    for r in mine:
        fam = family_of(r["description"])
        if fam not in wanted:
            continue
        proto = protocol_of(r["description"])
        supports, contra, caveats = [], [], []
        past = hist.get("codes", {}).get(r["code"], [])
        if past:
            phases = sorted({p["phase"] for p in past if p["asserted"]})
            if phases and att.phase not in phases:
                contra.append(f"Reported {len(past)} time(s) in the supplied logs, only in {', '.join(p.replace('_', ' ') for p in phases)} attempts; this one is {att.phase.replace('_', ' ')}.")
            elif phases:
                supports.append(f"Reported in {att.phase.replace('_', ' ')} attempts in the supplied logs.")
        if proto and connector_protocol and proto != connector_protocol:
            contra.append(f"Applies to {proto}; this connector is {connector_protocol}.")
        elif proto:
            caveats.append(f"Applies to {proto} only; this charger's connector type is not known.")
        by_family.setdefault(fam, []).append({
            "code": r["code"], "description": r["description"], "manufacturer": r["manufacturer"],
            "page": r.get("source_page"), "protocol": proto, "supports": supports, "contradicts": contra,
            "caveats": caveats, "reported_by_charger": False,
            "provenance": {"description": "catalogue", "family": "keyword_derived_unreviewed", "history": "supplied_logs"},
        })

    families = []
    for fam in wanted:
        items = sorted(by_family.get(fam, []), key=lambda x: x["code"])
        if items:
            families.append({"family": fam, "documented_entries": len(items), "entries": items})
    return {
        "families": families,
        "shared_contradictions": shared,
        "note": ("Documented faults whose description fits the stage where progress stopped, grouped by family. None was reported by "
                 "the charger. They are listed in code order, not by likelihood, and the families come from keywords in the "
                 "descriptions that no engineer has reviewed yet."),
    }


# ---- top level -----------------------------------------------------------------------------

def assess(att: Attempt, signals: list[dict], vendor: str | None, charger: str | None,
           energy_change, stop_reason, verdict: str) -> dict:
    stage = stage_assessment(att, energy_change)
    observed = observed_block(att, signals, energy_change, stop_reason)
    negative = negative_evidence(att, signals, energy_change)
    corpus_evidence = supplied_log_evidence(att, vendor, charger, observed["ocpp_statuses_in_attempt"]["value"])

    registry = _registry().get(charger or "", {})
    conn_proto = (registry.get("connectors") or {}).get(str(att.connector))
    provenance_registry = "registry" if conn_proto else "not_available"

    code_signals = [s for s in signals if s["kind"] in ("vendor_code", "code_in_info")]
    std_signals = [s for s in signals if s["kind"] == "ocpp_error"]
    if code_signals:
        cause = {"result": "CODE_REPORTED", "codes": [s["value"] for s in code_signals], "source": "log",
                 "note": "The charger reported these codes. Their meaning comes from the catalogue; the physical cause is still not established."}
        candidates = {"families": [], "shared_contradictions": [], "note": "A code was reported, so no inference from stage is needed."}
    elif std_signals:
        cause = {"result": "STANDARD_ERROR_REPORTED", "codes": [s["value"] for s in std_signals], "source": "log",
                 "note": "The charger reported a standard OCPP error but no manufacturer code."}
        candidates = {"families": [], "shared_contradictions": [], "note": "A standard error was reported; no vendor code to explain."}
    elif verdict in ("symptom_only", "problem"):
        status_seen = set(observed["ocpp_statuses_in_attempt"]["value"])
        domain = ("EV_SIDE_SUSPENSION" if "SuspendedEV" in status_seen else
                  "EVSE_SIDE_SUSPENSION" if "SuspendedEVSE" in status_seen else "UNDETERMINED")
        cause = {
            "result": "INSUFFICIENT_EVIDENCE",
            "likely_domain": {"value": domain, "source": "protocol_knowledge", "certainty": "strongly_inferred" if domain != "UNDETERMINED" else "weakly_inferred",
                              "note": "OCPP 1.6 defines SuspendedEV as the vehicle suspending charging." if domain == "EV_SIDE_SUSPENSION" else None},
            "note": ("The charger reported no fault. The stage where progress stopped can be located, but the log does not "
                     "contain enough to name the cause."),
        }
        candidates = documented_faults(att, vendor, stage["failure_window"]["value"], negative, conn_proto)
    else:
        cause = {"result": "NO_PROBLEM_SEEN", "note": "No fault, symptom or abnormal stop was found in this attempt."}
        candidates = {"families": [], "shared_contradictions": [], "note": ""}

    return {
        "observed": observed,
        "stage_assessment": stage,
        "cause_assessment": cause,
        "not_observed": negative,
        "supplied_logs_evidence": corpus_evidence,
        "documented_faults_to_check": candidates,
        "context": {"connector_protocol": {"value": conn_proto, "source": provenance_registry,
                                           "note": None if conn_proto else "No charger registry entry; CCS2/CHAdeMO cannot be told apart."},
                    "reported_vendor_id": fact(vendor, "log" if vendor else "not_available",
                                                note="OCPP vendorId; this does not confirm the charger's physical manufacturer or model."),
                    "model_and_firmware": {"value": registry.get("model"), "source": provenance_registry}},
    }

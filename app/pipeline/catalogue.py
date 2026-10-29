"""Look up a reported code in the supplied code tables.

Rules (the LLM never decides these):
  * Exact, case-sensitive match on the code. No prefix, fuzzy or numeric matching:
    10C316 is NOT 00C316.
  * An exact OCPP vendorId / table-manufacturer text match narrows the table, but does
    not prove this charger's physical manufacturer, model or firmware. A table with no
    manufacturer is reported as "unscoped": it might not describe this charger.
  * Every definition in the supplied tables is flagged requires_confirmation, so no
    result is ever presented as a confirmed meaning for this exact model/firmware.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from ..config import ROOT
from .glossary import NON_CODE_VALUES

CATALOGUE_PATH = ROOT / "data" / "catalogue.json"
OEM_CATALOGUE_PATH = ROOT / "data" / "oem_workbook_catalogue.json"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@lru_cache(maxsize=1)
def _load(path: str = str(CATALOGUE_PATH)):
    records = json.loads(Path(path).read_text())
    if Path(path) == CATALOGUE_PATH and OEM_CATALOGUE_PATH.exists():
        records.extend(json.loads(OEM_CATALOGUE_PATH.read_text()))
    by_code = defaultdict(list)
    by_text = defaultdict(list)
    for record in records:
        by_code[record["code"]].append(record)
        by_text[_norm(record["description"])].append(record)
    return records, by_code, by_text


def _entry(record: dict) -> dict:
    return {
        "code": record["code"],
        "description": record["description"],
        "manufacturer": record.get("manufacturer"),
        "source": record.get("source"),
        "page": record.get("source_page"),
        "model": record.get("model"),
        "source_row": record.get("source_row"),
        "field": record.get("field"),
        "recommended_actions": record.get("recommended_actions", []),
        "applicability_note": record.get("applicability_note"),
        "requires_confirmation": record.get("requires_confirmation", True),
    }


def _same_vendor(a: str | None, b: str | None) -> bool:
    return bool(a and b and _norm(a) == _norm(b))


def lookup(value: str | None, vendor: str | None, *, field: str, companion_text: str | None = None) -> dict | None:
    """Return a lookup result for one reported value, or None if it is not a code at all.

    companion_text: other free text reported on the same event (e.g. the info field),
    used only to note when the log text agrees with a table entry.
    """
    if value is None or value in NON_CODE_VALUES:
        return None
    _, by_code, by_text = _load()
    result = {"value": value, "field": field, "vendor": vendor, "status": "not_found", "entries": [], "note": ""}

    def applicable(record):
        kind = record.get("field")
        if kind == "stop_reason":
            return field == "stop_reason"
        if kind == "ocpp_error":
            return field == "ocpp_error"
        return field not in ("stop_reason", "ocpp_error")

    exact = [r for r in by_code.get(value, []) if applicable(r)]
    if exact:
        mine = [r for r in exact if _same_vendor(r.get("manufacturer"), vendor)]
        unscoped = [r for r in exact if not r.get("manufacturer")]
        other = [r for r in exact if r.get("manufacturer") and not _same_vendor(r.get("manufacturer"), vendor)]
        if mine:
            result["status"], chosen = "documented", mine
            result["note"] = "The reported vendor matches the table. Confirm the charger model and firmware before applying its definition or actions."
        elif vendor is None and other:
            result["status"], chosen = "vendor_unknown", other
            result["note"] = (f"The log does not contain a vendorId. {other[0]['manufacturer']} lists this code, "
                              "but it may not apply to this charger.")
        elif unscoped:
            result["status"], chosen = "unscoped_table", unscoped
            result["note"] = ("Listed in a code table that names no manufacturer, so it cannot be confirmed "
                              f"for {vendor or 'this charger'}.")
        else:
            result["status"], chosen = "other_vendor", other
            result["note"] = (f"Only {other[0]['manufacturer']}'s table lists this code, while the log's vendorId is "
                              f"{vendor}. It probably does not apply.")
        result["entries"] = [_entry(r) for r in chosen]
        if companion_text and any(_norm(companion_text) == _norm(r["description"]) for r in chosen):
            result["note"] += " The log's own text matches this description."
        return result

    phrase = [r for r in by_text.get(_norm(value), []) if applicable(r)]
    if phrase:
        result["status"] = "phrase_match"
        result["entries"] = [_entry(r) for r in phrase]
        result["note"] = ("The reported text equals a table description, but the table code differs from the "
                          "reported value. Not confirmed.")
        return result

    result["note"] = "No definition for this value in the supplied code tables."
    return result

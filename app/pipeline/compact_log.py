"""Compact source evidence without replacing reported values with interpretations."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .lossless import parse_line

FIELD_NAMES = {
    "connector_id": "connector", "error_code": "error", "vendor_id": "vendor",
    "vendor_error_code": "vendorError", "id_tag": "tag", "transaction_id": "transaction",
    "identifier": "charger", "meter_start_value": "meterStart", "meter_stop_value": "meterStop",
    "charge_point_vendor": "bootVendor", "charge_point_model": "model",
    "firmware_version": "firmware",
}
IDENTITIES = {"identifier": "C", "charger": "C", "id_tag": "U", "transaction_id": "T",
              "vendor_id": "V", "charge_point_vendor": "V"}
EMPTY_IDENTITIES = {"", "null", "None"}
BOILERPLATE = {"Request for", "Response for", "System Accepted Transaction :", "System Rejected Transaction :"}


def quoted(value: str) -> str:
    """Quote scalars only when needed; never alter their content."""
    return value if value and re.fullmatch(r"[\w./:+-]+", value) else json.dumps(value, ensure_ascii=False)


def build_compact_log(report: dict) -> dict:
    events = [parse_line(line["raw"], line["line"]) for line in report["lines"]]
    aliases: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        for field in event.fields:
            prefix = IDENTITIES.get(field.name)
            if prefix and field.value not in EMPTY_IDENTITIES:
                key = (prefix, field.value)
                if key not in aliases:
                    counts[prefix] += 1
                    aliases[key] = f"{prefix}{counts[prefix]}"

    def field_value(name: str, value: str) -> str:
        return aliases.get((IDENTITIES.get(name, ""), value), value)

    named_chargers = [alias for (prefix, _), alias in aliases.items() if prefix == "C"]
    dates = {event.ts_raw.split(" ")[0] for event in events if event.ts_raw}
    zones = {event.ts_raw.split(" ")[-1] for event in events if event.ts_raw}
    common_clock = len(dates) == len(zones) == 1
    header = ["COMPACT LOG | dates=DD-MM-YYYY | source order retained"]
    if common_clock:
        header.append(f"Date: {next(iter(dates))} | Timezone: {next(iter(zones))}")
    header.extend(f"{alias} = {quoted(original)}" for (_, original), alias in aliases.items())
    header.append("charger_inferred is a single-charger association, not an identifier reported on that line.")
    rows, records = [], []
    for event in events:
        fields = [(field.name, field_value(field.name, field.value)) for field in event.fields]
        time = event.ts_raw.split(" ")[1] if common_clock and event.ts_raw else (event.ts_raw or "time=unknown")
        parts = [f"L{event.line_no}", time, event.action or "Unknown"]
        if event.direction:
            parts.append(event.direction)
        parts.extend(f"{FIELD_NAMES.get(name, name)}={quoted(value)}" for name, value in fields)
        if event.status == "parsed" and not any(name in ("identifier", "charger") for name, _ in fields) and len(named_chargers) == 1:
            parts.append(f"charger_inferred={named_chargers[0]}")
        # Preserve non-boilerplate free text, duplicate fields and nested JSON. Partial
        # and unknown lines fall back to the exact original instead of guessing.
        extras = [span.text for span in event.spans if span.kind == "text"
                  and span.text not in BOILERPLATE and span.text != event.action
                  and not any(field.start == span.start and field.end == span.end for field in event.fields)]
        if extras:
            parts.append("extra=" + quoted(" ".join(extras)))
        for span in event.spans:
            if span.kind == "json":
                parts.append("payload=" + quoted(span.text))
        if event.status != "parsed":
            parts.append(f"parser={event.status} original={quoted(event.raw)}")
        rows.append(" ".join(parts))
        records.append({"line": event.line_no, "timestamp": event.ts_raw, "action": event.action,
                        "direction": event.direction, "fields": fields, "parser_status": event.status})
    return {"text": "\n".join(header) + "\n\n" + "\n".join(rows), "records": records,
            "aliases": aliases, "events": events}


def compact_log_preview(report: dict) -> dict:
    compact = build_compact_log(report)
    return {"format": "compact-text-1", "text": compact["text"], "line_count": len(compact["records"]),
            "note": "Timestamps and all parsed fields are retained. Identifier aliases are defined above; inferred charger associations are marked. Unparsed text is kept as originally written."}

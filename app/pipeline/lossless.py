"""Lossless log-line parser.

Design
------
One input line becomes one Event. Nothing is thrown away or rewritten.

* ``raw`` is the exact original line.
* ``spans`` partition ``raw`` completely: they are contiguous, never overlap,
  and ``"".join(span.text) == raw``. Every character belongs to exactly one span.
* A span has a kind:
    timestamp  the "DD-MM-YYYY HH:MM:SS TZ" prefix, kept as written
    label      the text before the first colon, e.g. "Status Notification Request"
    key        a field name as written in the log
    value      a field value as written (strings only, never converted)
    json       a whole JSON body (fields are derived from it)
    text       free text that was kept but not turned into a field
    glue       punctuation and connector words: "[", "=", ", ", ":", "with", "for"
    ws         whitespace
    residual   content the parser could NOT place. This is the "something was
               lost track of" signal. Any residual makes the event "partial".
* ``fields`` are ordered, may repeat a key, and each one points at its value span.
* Normalized values (ISO timestamp, canonical key names) are added NEXT TO the
  originals. They never replace them.
* parse_line never raises. Bad input becomes status "partial" or "unknown".

Status: parsed  = no residual; partial = header understood but some residual;
        unknown = no recognizable header, the whole line is residual.

Known assumption: dates are read as DD-MM-YYYY. The logs only contain 08-09 and
09-09, so the format cannot be proven from the data. ``ts_raw`` always keeps the
original so a wrong assumption can be corrected later.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

TZ_MINUTES = {"IST": 330, "UTC": 0, "GMT": 0}
DIRECTIONS = {"Request": "request", "Response": "response", "Notification": "notification",
              "Success": "success", "Status": "status"}
DATE_FORMAT_ASSUMPTION = "DD-MM-YYYY"

HEADER = re.compile(
    r"^(?P<dd>\d\d)-(?P<mm>\d\d)-(?P<yyyy>\d{4}) (?P<time>\d\d:\d\d:\d\d) (?P<tz>[A-Z]{2,5}) "
    r"(?P<label>[^:]+?)\s*:"
)
BRACKET_BODY = re.compile(r"^(?P<lead>\s*)(?P<name>[A-Za-z][\w]*)\s*\[(?P<inner>.*)\](?P<trail>\s*)$", re.S)
BRACKET_KEY = re.compile(r"\s*([A-Za-z_][\w.]*)\s*=")
BRACKET_NEXT = re.compile(r",\s*[A-Za-z_][\w.]*\s*=")

# Field names seen in sentence-style lines. Unknown "Key: value" phrases are still
# captured by the generic fallback below; this list only makes common ones precise.
SENTENCE_KEYS = ["Connector Id", "idTag", "Transaction Id", "Meter Start Value", "Meter Stop Value",
                 "Meter Value", "Reason", "Unit", "SoC", "is"]
SENTENCE_KEY = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in sorted(SENTENCE_KEYS, key=len, reverse=True)) + r")\s*:\s*"
)
GLUE_WORDS = {"with", "for", "and", "is", "of"}
GENERIC_PAIR = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<v>\S+)\s*$")
CONNECTION = re.compile(r"^\s*(?P<charger>[^\s:]+)::(?P<endpoint>\S+)\s*$")
TRAILING_GLUE = re.compile(r"(?:\s+(?:with|for|and|is|of))+\s*$")

ALIASES = {"SoC": "soc", "is": "status"}


def canonical_key(key: str) -> str:
    """Stable snake_case name. The original spelling stays in Field.key."""
    if key in ALIASES:
        return ALIASES[key]
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    return re.sub(r"[\s.]+", "_", key).lower()


@dataclass(frozen=True)
class Span:
    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Field:
    key: str            # exactly as written in the log
    value: str          # exactly as written in the log (always a string)
    name: str           # canonical key (derived)
    start: int | None   # value span in raw (None for values derived from JSON)
    end: int | None
    source: str         # bracket | sentence | json | connection | text


@dataclass
class Event:
    raw: str
    line_no: int | None
    status: str                       # parsed | partial | unknown
    ts_raw: str | None = None
    ts: str | None = None             # ISO-8601 with offset, derived from ts_raw
    label: str | None = None
    action: str | None = None
    direction: str | None = None
    fields: list[Field] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def get(self, name: str, default: str | None = None) -> str | None:
        for item in self.fields:
            if item.name == name:
                return item.value
        return default

    def reconstruct(self) -> str:
        return "".join(span.text for span in self.spans)

    def to_dict(self) -> dict:
        return {
            "line_no": self.line_no, "status": self.status, "ts_raw": self.ts_raw, "ts": self.ts,
            "label": self.label, "action": self.action, "direction": self.direction,
            "fields": [{"key": f.key, "name": f.name, "value": f.value, "source": f.source} for f in self.fields],
            "issues": self.issues, "raw": self.raw,
        }


# ---------------------------------------------------------------- helpers

def _strip_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _is_glue(gap: str) -> bool:
    rest = re.sub(r"[\s:,\[\]=]+", " ", gap).strip()
    if not rest:
        return True
    return all(word in GLUE_WORDS for word in rest.split())


class _Builder:
    def __init__(self, raw: str):
        self.raw = raw
        self.spans: list[Span] = []
        self.fields: list[Field] = []

    def span(self, kind: str, start: int, end: int) -> None:
        if end > start:
            self.spans.append(Span(kind, start, end, self.raw[start:end]))

    def field(self, key_range: tuple[int, int] | None, value_range: tuple[int, int], source: str, key: str | None = None) -> None:
        if key_range is not None:
            self.span("key", *key_range)
            key = self.raw[key_range[0]:key_range[1]]
        self.span("value", *value_range)
        value = self.raw[value_range[0]:value_range[1]]
        self.fields.append(Field(key or "", value, canonical_key(key or ""), value_range[0], value_range[1], source))

    def finish(self, gap_kind) -> list[Span]:
        """Fill every gap so spans cover the line exactly. gap_kind(text) -> span kind."""
        ordered = sorted(self.spans, key=lambda s: (s.start, s.end))
        result: list[Span] = []
        cursor = 0
        for span in ordered:
            if span.start < cursor:
                raise ValueError(f"overlapping spans at {span.start}")
            if span.start > cursor:
                gap = self.raw[cursor:span.start]
                result.append(Span(gap_kind(gap), cursor, span.start, gap))
            result.append(span)
            cursor = span.end
        if cursor < len(self.raw):
            gap = self.raw[cursor:]
            result.append(Span(gap_kind(gap), cursor, len(self.raw), gap))
        return result


def _timestamp(match: re.Match) -> tuple[str | None, list[str]]:
    offset = TZ_MINUTES.get(match["tz"])
    if offset is None:
        return None, [f"unknown_timezone:{match['tz']}"]
    try:
        stamp = datetime.strptime(
            f"{match['yyyy']}-{match['mm']}-{match['dd']} {match['time']}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone(timedelta(minutes=offset)))
    except ValueError:
        return None, ["invalid_timestamp"]
    return stamp.isoformat(), []


def _action(label: str) -> tuple[str, str | None]:
    words = label.split()
    if len(words) > 1 and words[-1] in DIRECTIONS:
        return "".join(words[:-1]), DIRECTIONS[words[-1]]
    return "".join(words), None


# ---------------------------------------------------------------- body parsers

def _json_body(b: _Builder, start: int, issues: list[str]) -> None:
    raw = b.raw
    s, e = _strip_range(raw, start, len(raw))
    text = raw[s:e]

    def pairs(items):
        return items  # keep duplicates and order

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except ValueError:
        issues.append("invalid_json")
        b.span("residual", s, e)
        return
    b.span("json", s, e)
    if isinstance(value, list) and value and isinstance(value[0], list) is False and all(isinstance(x, tuple) for x in value):
        for key, item in value:
            rendered = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            b.fields.append(Field(key, rendered, canonical_key(key), None, None, "json"))
    else:
        b.fields.append(Field("json", text, "json", s, e, "json"))


def _bracket_body(b: _Builder, match: re.Match, offset: int, issues: list[str]) -> None:
    name_start, name_end = offset + match.start("name"), offset + match.end("name")
    b.span("text", name_start, name_end)
    inner = match["inner"]
    base = offset + match.start("inner")
    pos = 0
    while pos < len(inner):
        key_match = BRACKET_KEY.match(inner, pos)
        if not key_match:
            nxt = BRACKET_NEXT.search(inner, pos)
            stop = nxt.start() if nxt else len(inner)
            s, e = _strip_range(inner, pos, stop)
            if e > s:
                b.span("residual", base + s, base + e)
                issues.append("unkeyed_text_in_brackets")
            pos = stop + 1 if nxt else len(inner)
            continue
        key_range = (base + key_match.start(1), base + key_match.end(1))
        value_from = key_match.end()
        nxt = BRACKET_NEXT.search(inner, value_from)
        value_to = nxt.start() if nxt else len(inner)
        vs, ve = _strip_range(inner, value_from, value_to)
        b.field(key_range, (base + vs, base + ve), "bracket")
        if "," in inner[vs:ve]:
            # "a, b" could be one value or a value plus stray text; we cannot know.
            issues.append(f"ambiguous_comma_in_value:{inner[key_range[0]-base:key_range[1]-base]}")
        pos = value_to + 1 if nxt else len(inner)


def _sentence_body(b: _Builder, start: int, action: str) -> None:
    raw = b.raw
    marks = list(SENTENCE_KEY.finditer(raw, start))
    for i, mark in enumerate(marks):
        end_limit = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        # A value is one token. Anything after it is classified separately below,
        # so an unknown "Voltage: 400" is never silently swallowed into the value.
        token = re.compile(r"\S+").search(raw, mark.end(), end_limit)
        if not token:
            continue
        b.field((mark.start(1), mark.end(1)), (token.start(), token.end()), "sentence")
    # Free-text gaps: try "phrase : value" pairs, then connection ids, then keep as text.
    ordered = sorted(b.spans, key=lambda s: s.start)
    gaps: list[tuple[int, int]] = []
    cursor = start
    for span in ordered:
        if span.start > cursor:
            gaps.append((cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < len(raw):
        gaps.append((cursor, len(raw)))
    first_mark = marks[0].start() if marks else None
    for gs, ge in gaps:
        gap = raw[gs:ge]
        if not gap.strip() or _is_glue(gap):
            continue
        # drop leading connector words ("with Voltage: 400" -> "Voltage: 400")
        lead = re.match(r"\s*(?:(?:with|for|and|is|of)\s+)+", gap)
        if lead and gs + lead.end() < ge:
            gs, gap = gs + lead.end(), gap[lead.end():]
        conn = CONNECTION.match(gap)
        if conn:
            b.field(None, (gs + conn.start("charger"), gs + conn.end("charger")), "connection", key="charger")
            b.field(None, (gs + conn.start("endpoint"), gs + conn.end("endpoint")), "connection", key="endpoint")
            continue
        pair = GENERIC_PAIR.match(gap)
        if pair:
            b.field((gs + pair.start("k"), gs + pair.end("k")), (gs + pair.start("v"), gs + pair.end("v")), "sentence")
            continue
        ts, te = _strip_range(raw, gs, ge)
        # Descriptive words before the first field ("System Accepted Transaction :")
        # are fine as text. Words stranded between/after fields were not understood.
        leading = first_mark is None or ge <= first_mark
        b.span("text" if leading else "residual", ts, te)
    if not b.fields:
        s, e = _strip_range(raw, start, len(raw))
        if e > s:
            # Whole body is a plain phrase such as "Accepted"; expose it as a field too.
            b.fields.append(Field("text", raw[s:e], "text", s, e, "text"))


RESULT_WORD = re.compile(r"\b(Accepted|Rejected|Blocked|Expired|Invalid|ConcurrentTx|Pending|Failed)\b")


def _derive_result(b: _Builder, body_start: int) -> None:
    """Expose an outcome word buried in free text (e.g. "System Accepted Transaction")
    as a ``result`` field. The word stays in its text span; this only adds a pointer."""
    known = {f.value for f in b.fields}
    for match in RESULT_WORD.finditer(b.raw, body_start):
        if match[1] in known:
            continue
        inside_json = any(s.kind == "json" and s.start <= match.start() < s.end for s in b.spans)
        if inside_json:
            continue
        b.fields.append(Field("result", match[1], "result", match.start(1), match.end(1), "derived"))
        known.add(match[1])


# ---------------------------------------------------------------- public API

def parse_line(raw: str, line_no: int | None = None) -> Event:
    """Parse one line. Never raises; failures become partial/unknown events."""
    try:
        return _parse(raw, line_no)
    except Exception as exc:  # defensive: keep the line, record why
        return Event(raw, line_no, "unknown", issues=[f"parser_exception:{type(exc).__name__}"],
                     spans=[Span("residual", 0, len(raw), raw)] if raw else [])


def _parse(raw: str, line_no: int | None) -> Event:
    match = HEADER.match(raw)
    if not match:
        spans = [Span("residual", 0, len(raw), raw)] if raw else []
        return Event(raw, line_no, "unknown", issues=["no_header"], spans=spans)

    b = _Builder(raw)
    issues: list[str] = []
    ts, ts_issues = _timestamp(match)
    issues += ts_issues
    label = match["label"]
    action, direction = _action(label)
    b.span("timestamp", 0, match.end("tz"))
    b.span("label", match.start("label"), match.end("label"))
    body_start = match.end()

    body = raw[body_start:]
    stripped = body.strip()
    bracket = BRACKET_BODY.match(body) if stripped else None
    if stripped[:1] in ("{", "["):
        _json_body(b, body_start, issues)
        gap_kind = lambda g: "ws" if not g.strip() else "glue"
    elif bracket:
        _bracket_body(b, bracket, body_start, issues)
        gap_kind = lambda g: "ws" if not g.strip() else ("glue" if re.fullmatch(r"[\s\[\],=:]*", g) else "residual")
    elif stripped:
        _sentence_body(b, body_start, action)
        gap_kind = lambda g: "ws" if not g.strip() else ("glue" if _is_glue(g) else "text")
    else:
        gap_kind = lambda g: "ws" if not g.strip() else "glue"

    _derive_result(b, body_start)
    spans = b.finish(gap_kind)
    status = "partial" if (issues or any(s.kind == "residual" for s in spans)) else "parsed"
    event = Event(raw, line_no, status, ts_raw=raw[:match.end("tz")], ts=ts, label=label, action=action,
                  direction=direction, fields=b.fields, spans=spans, issues=issues)
    return event


def parse_text(text: str) -> tuple[list[Event], int]:
    """Parse a pasted log. Returns (events for every non-blank line, blank line count)."""
    events: list[Event] = []
    blanks = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            events.append(parse_line(line, number))
        else:
            blanks += 1
    return events, blanks

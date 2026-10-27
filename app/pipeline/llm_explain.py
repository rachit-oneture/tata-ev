"""Prompt, output schema and automatic checks for the LLM explanation step.

The model only EXPLAINS facts that software already established. Every output is checked by code;
anything that breaks a hard rule is rejected, and the page falls back to the rule-based report.
"""
from __future__ import annotations

import json
import re

VERDICTS = ["CODE_REPORTED", "STANDARD_ERROR_REPORTED", "INSUFFICIENT_EVIDENCE", "NO_PROBLEM_SEEN"]
STANCES = ["fits_stage", "contradicted", "unclear"]
ANSWERS = ["confirmed", "not_confirmed", "cannot_determine", "no_question"]

SYSTEM_PROMPT = """You explain one EV charger session to a field technician. Software has already read the log and produced FACTS. You only explain those facts.

RULES
1. Use only the FACTS. Do not add events, numbers, times, codes or causes that are not in them.
2. Cite events like [E3]. Only use event ids that exist.
3. cause.verdict MUST equal facts.cause_result.
4. cause.reported_codes MUST equal facts.reported_codes exactly (an empty list if there are none).
5. If cause_result is INSUFFICIENT_EVIDENCE: say plainly that the log does not show the cause. Do NOT write any candidate code in "summary" or in "cause.explanation".
6. candidate_faults_not_reported are documented faults that fit the stage. They were NOT reported by the charger and are NOT findings. In candidate_review list each candidate (use the listed code only) and classify it fits_stage, contradicted or unclear, using only the contradictions listed for it and candidate_shared_contradictions.
7. No percentages and no probabilities.
8. If the technician asks a question, answer only from the facts: "confirmed" only if the facts directly show it; "not_confirmed" if the facts contradict it; otherwise "cannot_determine". Do not agree just because the technician suggests something. With no question, use "no_question".
9. Plain English, short sentences. Quote durations only from key_intervals_seconds or event times.

Return ONE JSON object and nothing else (no markdown fences), with exactly these keys:
{
 "summary": "2-3 plain sentences: before or after start, what failed, what is reported",
 "what_happened": ["short step [E#]", ...],
 "how_it_happened": ["order of events and what comes before or after what [E#]", ...],
 "when": ["timing facts using seconds from the facts [E#]", ...],
 "cause": {"verdict": "CODE_REPORTED|STANDARD_ERROR_REPORTED|INSUFFICIENT_EVIDENCE|NO_PROBLEM_SEEN", "reported_codes": [], "explanation": "..."},
 "candidate_review": [{"code": "...", "stance": "fits_stage|contradicted|unclear", "reason": "..."}],
 "answer_to_question": {"answer": "confirmed|not_confirmed|cannot_determine|no_question", "reason": "..."},
 "not_known": ["things the log cannot tell"],
 "cited_events": ["E1", "E2"]
}"""


def user_message(facts: dict, question: str | None) -> str:
    body = "FACTS:\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    return body + ("\n\nTECHNICIAN QUESTION: " + question if question else "\n\nTECHNICIAN QUESTION: (none)")


def extract_json(text: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                return None
    return None


CODE_TOKEN = re.compile(r"\b(?:[0-9A-F]{6,7}|\d{3}-\d+-\d+|\d{3}-\d+)\b")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


# A sentence that asserts what caused or initiated something, with no hedge, is an over-claim when the
# facts only show ORDER. These are heuristics: they flag sentences for a human to read, nothing more.
CAUSAL = [
    r"\b(?:error|report|code|308-3-3|noticed)\b.{0,70}\b(?:caus\w+|led to|leading to|result\w* in|trigger\w+|initiat\w+)\b",
    r"\b(?:stop\w*|session|disconnect\w*|ended)\b.{0,50}\b(?:due to|because of|caused by|as a result of)\b.{0,40}\b(?:error|report|code)\b",
    r"\b(?:vehicle|ev)\b.{0,25}\b(?:itself\s+)?(?:detected|initiated|decided)\b",
    r"\b(?:causing|caused|causes)\b.{0,30}\b(?:session|stop|pause|disconnect)",
]
HEDGE = re.compile(r"\b(may|might|could|cannot|can't|can not|not show|does not|do not|unknown|unclear|whether|possibly|possible|not known|no evidence|did not|not clear|not enough|insufficient)\b", re.I)


def causal_claims(out: dict) -> list[str]:
    sentences = []
    fields = [out.get("summary", ""), (out.get("cause") or {}).get("explanation", "")] + [x for x in out.get("how_it_happened", []) if isinstance(x, str)]
    for field in fields:
        sentences += [x.strip() for x in re.split(r"(?<=[.;])\s+", field or "") if x.strip()]
    return [x for x in sentences if any(re.search(p, x, re.I) for p in CAUSAL) and not HEDGE.search(x)]


def check(text: str, facts: dict, *, asked: bool = False, expect_not_confirmed: bool = False) -> dict:
    """Hard checks return True/False. 'soft' entries are counts for information only."""
    r: dict = {}
    out = extract_json(text)
    r["json_valid"] = isinstance(out, dict)
    if not r["json_valid"]:
        return {"hard": {"json_valid": False}, "soft": {}, "parsed": None}

    keys = {"summary", "what_happened", "how_it_happened", "when", "cause", "candidate_review", "answer_to_question", "not_known", "cited_events"}
    cause = out.get("cause") if isinstance(out.get("cause"), dict) else {}
    answer = out.get("answer_to_question") if isinstance(out.get("answer_to_question"), dict) else {}
    review = out.get("candidate_review") if isinstance(out.get("candidate_review"), list) else []
    lists_ok = all(isinstance(out.get(k), list) for k in ("what_happened", "how_it_happened", "when", "not_known", "cited_events"))
    r["schema_ok"] = (keys <= set(out) and lists_ok and isinstance(out.get("summary"), str) and cause.get("verdict") in VERDICTS
                      and isinstance(cause.get("reported_codes"), list) and isinstance(cause.get("explanation"), str)
                      and answer.get("answer") in ANSWERS
                      and all(isinstance(x, dict) and x.get("stance") in STANCES for x in review))
    r["verdict_matches"] = cause.get("verdict") == facts["cause_result"]
    r["codes_match"] = sorted(map(str, cause.get("reported_codes", []))) == sorted(facts["reported_codes"])

    facts_text = json.dumps(facts, ensure_ascii=False)
    facts_tokens = set(CODE_TOKEN.findall(facts_text))
    all_text = " ".join(_strings(out))
    invented = sorted({t for t in CODE_TOKEN.findall(all_text) if t not in facts_tokens and re.search(r"[A-F]|^0|-", t)})
    r["no_invented_codes"] = not invented

    valid_ids = {e["id"] for e in facts["events"]}
    cited = set(re.findall(r"\bE\d+\b", all_text)) | {str(x) for x in out.get("cited_events", [])}
    r["citations_valid"] = cited <= valid_ids and bool(cited)

    pct = lambda t: {re.sub(r"\s+", "", x) for x in re.findall(r"\d+(?:\.\d+)?\s*%", t)}
    r["no_invented_percentages"] = pct(all_text) <= pct(facts_text) and not re.search(
        r"\b(confidence|probabilit\w*|likelihood|chance of)\b", all_text, re.I)

    listed = {c["code"] for c in facts["candidate_faults_not_reported"]}
    review_codes = [str(x.get("code")) for x in review if isinstance(x, dict)]
    r["review_only_listed_candidates"] = set(review_codes) <= listed
    guarded = " ".join(_strings({"summary": out.get("summary"), "explanation": cause.get("explanation")}))
    if facts["cause_result"] != "CODE_REPORTED":
        named = sorted(c for c in listed if c in guarded)
        r["candidates_not_named_as_cause"] = not named
    else:
        r["candidates_not_named_as_cause"] = True
    r["says_what_is_not_known"] = bool(out.get("not_known")) if facts["cause_result"] == "INSUFFICIENT_EVIDENCE" else True
    if asked:
        r["question_handled"] = (answer.get("answer") != "confirmed") if expect_not_confirmed else answer.get("answer") in ANSWERS[:3]
    else:
        r["question_handled"] = answer.get("answer") == "no_question"

    allowed = {str(abs(i["seconds"])) for i in facts["key_intervals_seconds"]} | {str(e["t_seconds"]) for e in facts["events"] if e["t_seconds"] is not None}
    quoted = re.findall(r"\b(\d+)\s*(?:s\b|sec\b|seconds?\b)", all_text)
    soft = {"causal_claims": causal_claims(out), "seconds_not_in_facts": sorted({q for q in quoted if q not in allowed}),
            "invented_code_tokens": invented, "unknown_event_ids": sorted(cited - valid_ids)}
    return {"hard": r, "soft": soft, "parsed": out}

"""Paste logs -> parse -> group into attempts -> look up codes -> plain-language report."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..pipeline.report import analyze_text
from ..pipeline.llm_diagnose import add_llm_reviews, compact_log_preview
from ..db import save_analysis

router = APIRouter(tags=["analyze"])

MAX_CHARS = 1_000_000


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logs: str = Field(min_length=1, max_length=MAX_CHARS)
    use_llm: bool = False


@router.post("/analyze")
def analyze(body: AnalyzeRequest):
    if not body.logs.strip():
        raise HTTPException(400, "Paste at least one log line.")
    try:
        report = analyze_text(body.logs)
        # Make the exact de-identified, compact records visible for review. This is
        # the same representation supplied to the whole-case LLM review.
        report["llm_compact_log"] = compact_log_preview(report)
        result = add_llm_reviews(report) if body.use_llm else report
        return save_analysis(body.logs, result)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

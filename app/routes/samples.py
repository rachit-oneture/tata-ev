"""The supplied example logs, grouped as in the client's before/after-start workbooks."""
import json
import re
from functools import lru_cache

from fastapi import APIRouter, HTTPException

from ..config import ROOT

router = APIRouter(tags=["samples"])
PATH = ROOT / "data" / "samples.json"
GROUPS = [("before_start", "Before start"), ("after_start", "After start")]


@lru_cache(maxsize=1)
def _samples() -> list[dict]:
    try:
        return json.loads(PATH.read_text())["samples"]
    except (OSError, ValueError, KeyError):
        return []


@router.get("/samples")
def list_samples():
    items = _samples()
    return {"groups": [
        {"id": gid, "label": label,
         "samples": [{"id": s["id"], "label": s["label"], "lines": s["lines"]} for s in items if s["group"] == gid]}
        for gid, label in GROUPS
    ]}


@router.get("/samples/{sample_id}")
def get_sample(sample_id: str):
    if not re.fullmatch(r"S\d{2}", sample_id):
        raise HTTPException(400, "Invalid sample id.")
    for s in _samples():
        if s["id"] == sample_id:
            return {"id": s["id"], "label": s["label"], "group": s["group"], "lines": s["lines"], "logs": s["logs"]}
    raise HTTPException(404, "Sample not found.")

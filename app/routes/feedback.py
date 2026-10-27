"""A client rating and comment belong to the immutable response they reviewed."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db import save_feedback

router = APIRouter(tags=["feedback"])


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: UUID
    rating: Literal["up", "down"] | None = None
    feedback: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_feedback(self):
        if self.feedback is not None:
            self.feedback = self.feedback.strip()
        if self.rating is None and not self.feedback:
            raise ValueError("Choose thumbs up/down or write feedback.")
        return self


@router.post("/feedback")
def feedback(body: FeedbackRequest):
    result = save_feedback(str(body.analysis_id), body.rating, body.feedback)
    if result is None:
        raise HTTPException(404, "This analysis could not be found. Analyze the log again before sending feedback.")
    return result

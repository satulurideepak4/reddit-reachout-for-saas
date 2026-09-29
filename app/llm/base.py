from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models import Intent, RecommendedAction


class LLMError(Exception):
    """Provider call failed (network, auth, overload...)."""


class LLMOutputError(LLMError):
    """Provider answered but the output did not match the schema."""


class OpportunityDecision(BaseModel):
    """Strict structured output the LLM must return."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    relevant: bool
    intent: Intent
    relevance_score: float = Field(alias="relevanceScore", ge=0, le=1)
    recommended_action: RecommendedAction = Field(alias="recommendedAction")
    reason: str
    suggested_response: str | None = Field(default=None, alias="suggestedResponse")
    confidence: float = Field(ge=0, le=1)
    # Reddit id (bare or t1_ form) of the comment to reply to / DM the author of
    target_comment_id: str | None = Field(default=None, alias="targetCommentId")


class LLMClient(Protocol):
    model: str

    def decide(self, system: str, user: str) -> dict:
        """Return a JSON object matching OpportunityDecision's schema (by alias)."""


def decision_json_schema() -> dict:
    return OpportunityDecision.model_json_schema(by_alias=True)

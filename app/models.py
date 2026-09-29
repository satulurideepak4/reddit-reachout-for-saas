import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.util import utcnow


class RecommendedAction(str, enum.Enum):
    REPLY_TO_POST = "REPLY_TO_POST"
    REPLY_TO_COMMENT = "REPLY_TO_COMMENT"
    PREPARE_PRIVATE_MESSAGE = "PREPARE_PRIVATE_MESSAGE"
    MONITOR = "MONITOR"
    NO_ACTION = "NO_ACTION"


class Intent(str, enum.Enum):
    USER_LOOKING_FOR_SOLUTION = "USER_LOOKING_FOR_SOLUTION"
    USER_HAS_PROBLEM = "USER_HAS_PROBLEM"
    DISCUSSING_COMPETITOR = "DISCUSSING_COMPETITOR"
    SEEKING_ADVICE = "SEEKING_ADVICE"
    SHARING_INFORMATION = "SHARING_INFORMATION"
    PROMOTIONAL_OR_SPAM = "PROMOTIONAL_OR_SPAM"
    UNRELATED = "UNRELATED"


class OpportunityStatus(str, enum.Enum):
    NEW = "NEW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    POSTING = "POSTING"  # transient lock state while a Reddit call is in flight
    POSTED = "POSTED"
    FAILED = "FAILED"
    MONITORING = "MONITORING"
    IGNORED = "IGNORED"


class MonitoredSubreddit(Base):
    """A watched subreddit plus its processing checkpoint."""

    __tablename__ = "monitored_subreddits"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # checkpoint: newest post already ingested
    last_seen_created_utc: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seen_fullname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RedditItem(Base):
    """A Reddit post or comment we have seen. Unique by fullname (t3_x / t1_x)."""

    __tablename__ = "reddit_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    fullname: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(8))  # POST | COMMENT
    subreddit: Mapped[str] = mapped_column(String(100), index=True)
    post_id: Mapped[str] = mapped_column(String(16), index=True)  # bare id of the thread
    comment_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parent_fullname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    permalink: Mapped[str] = mapped_column(String(512), default="")
    created_utc: Mapped[float] = mapped_column(Float)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # PENDING (awaiting analysis) | ANALYZED | FILTERED  -- posts only; comments are context
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    filter_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Opportunity(Base):
    """One row per Reddit thread that passed pre-filtering. Holds the current decision."""

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("post_id", name="uq_opportunity_post"),
        Index("ix_opp_status_score", "status", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subreddit: Mapped[str] = mapped_column(String(100), index=True)
    post_id: Mapped[str] = mapped_column(String(16))
    post_fullname: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default=OpportunityStatus.NEW.value, index=True)
    recommended_action: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)  # relevance 0..1, sortable
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # target of the proposed action
    target_comment_fullname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_author: Mapped[str | None] = mapped_column(String(64), nullable=True)

    original_response: Mapped[str | None] = mapped_column(Text, nullable=True)  # as generated
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # editable draft

    # re-evaluation state (for MONITOR)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_analyzed_comment_count: Mapped[int] = mapped_column(Integer, default=0)
    analysis_count: Mapped[int] = mapped_column(Integer, default=0)

    # execution result / idempotency
    posted_reddit_fullname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    decisions: Mapped[list["LlmDecision"]] = relationship(
        back_populates="opportunity", order_by="LlmDecision.id"
    )


class LlmDecision(Base):
    """Every LLM analysis run (audit trail); the latest is mirrored on Opportunity."""

    __tablename__ = "llm_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict] = mapped_column(JSON)  # parsed structured output as returned
    final_action: Mapped[str] = mapped_column(String(32))  # after business-rule adjustment
    adjustment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    opportunity: Mapped[Opportunity] = relationship(back_populates="decisions")


class ActionHistory(Base):
    """Status transitions and Reddit actions."""

    __tablename__ = "action_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

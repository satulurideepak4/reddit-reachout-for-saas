import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from app import metrics
from app.actions import ActionError, OpportunityActionService
from app.models import (
    ActionHistory,
    MonitoredSubreddit,
    Opportunity,
    OpportunityStatus,
    RecommendedAction,
    RedditItem,
)

router = APIRouter(prefix="/api/reddit")


def get_session(request: Request):
    with request.app.state.session_factory() as s:
        yield s


def require_token(request: Request) -> None:
    token = request.app.state.settings.api_token
    if token:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given, token):
            raise HTTPException(401, "invalid or missing API token")


SessionDep = Annotated[Session, Depends(get_session)]
Auth = [Depends(require_token)]


class OpportunityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subreddit: str
    post_id: str
    status: str
    recommended_action: str | None
    intent: str | None
    score: float
    confidence: float
    reason: str | None
    target_comment_fullname: str | None
    target_author: str | None
    original_response: str | None
    response_text: str | None
    analysis_count: int
    posted_reddit_fullname: str | None
    last_error: str | None
    post_title: str | None = None
    permalink: str | None = None


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    fullname: str
    kind: str
    author: str | None
    title: str | None
    body: str
    permalink: str
    created_utc: float


class OpportunityDetail(OpportunityOut):
    post: ItemOut | None = None
    target_comment: ItemOut | None = None
    comments: list[ItemOut] = []
    history: list[dict] = []
    decisions: list[dict] = []


class ResponseEdit(BaseModel):
    response_text: str


def _summary(s: Session, o: Opportunity) -> OpportunityOut:
    out = OpportunityOut.model_validate(o)
    post = s.scalar(select(RedditItem).where(RedditItem.fullname == o.post_fullname))
    if post:
        out.post_title, out.permalink = post.title, post.permalink
    return out


def _run(fn, *args):
    try:
        return fn(*args)
    except ActionError as e:
        raise HTTPException(e.code, str(e)) from e


@router.get("/opportunities", dependencies=Auth, response_model=list[OpportunityOut])
def list_opportunities(
    s: SessionDep,
    subreddit: str | None = None,
    action: RecommendedAction | None = None,
    status: OpportunityStatus | None = None,
    sort: str = Query("score", pattern="^(score|created_at|confidence)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    q = select(Opportunity)
    if subreddit:
        q = q.where(Opportunity.subreddit.ilike(subreddit))
    if action:
        q = q.where(Opportunity.recommended_action == action.value)
    if status:
        q = q.where(Opportunity.status == status.value)
    q = q.order_by((desc if order == "desc" else asc)(getattr(Opportunity, sort))).limit(limit).offset(offset)
    return [_summary(s, o) for o in s.scalars(q)]


@router.get("/opportunities/{opp_id}", dependencies=Auth, response_model=OpportunityDetail)
def get_opportunity(opp_id: int, s: SessionDep):
    o = s.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "opportunity not found")
    base = _summary(s, o)
    items = list(s.scalars(select(RedditItem).where(RedditItem.post_id == o.post_id).order_by(RedditItem.created_utc)))
    post = next((i for i in items if i.fullname == o.post_fullname), None)
    comments = [i for i in items if i.kind == "COMMENT"]
    detail = OpportunityDetail(**base.model_dump())
    detail.post = ItemOut.model_validate(post) if post else None
    tc = next((c for c in comments if c.fullname == o.target_comment_fullname), None)
    detail.target_comment = ItemOut.model_validate(tc) if tc else None
    detail.comments = [ItemOut.model_validate(c) for c in comments]
    detail.history = [
        {"action": h.action, "from": h.from_status, "to": h.to_status, "detail": h.detail, "at": h.created_at.isoformat()}
        for h in s.scalars(select(ActionHistory).where(ActionHistory.opportunity_id == o.id).order_by(ActionHistory.id))
    ]
    detail.decisions = [
        {"model": d.model, "raw": d.raw, "final_action": d.final_action, "note": d.adjustment_note, "at": d.created_at.isoformat()}
        for d in o.decisions
    ]
    return detail


def _svc(request: Request, s: Session) -> OpportunityActionService:
    return OpportunityActionService(s, request.app.state.reddit, request.app.state.settings)


@router.patch("/opportunities/{opp_id}/response", dependencies=Auth, response_model=OpportunityOut)
def edit_response(opp_id: int, body: ResponseEdit, request: Request, s: SessionDep):
    return _summary(s, _run(_svc(request, s).edit_response, opp_id, body.response_text))


@router.post("/opportunities/{opp_id}/approve", dependencies=Auth, response_model=OpportunityOut)
def approve(opp_id: int, request: Request, s: SessionDep):
    return _summary(s, _run(_svc(request, s).approve, opp_id))


@router.post("/opportunities/{opp_id}/reject", dependencies=Auth, response_model=OpportunityOut)
def reject(opp_id: int, request: Request, s: SessionDep):
    return _summary(s, _run(_svc(request, s).reject, opp_id))


@router.post("/opportunities/{opp_id}/ignore", dependencies=Auth, response_model=OpportunityOut)
def ignore(opp_id: int, request: Request, s: SessionDep):
    return _summary(s, _run(_svc(request, s).ignore, opp_id))


@router.post("/opportunities/{opp_id}/execute", dependencies=Auth)
def execute(opp_id: int, request: Request, s: SessionDep):
    opp, already = _run(_svc(request, s).execute, opp_id)
    return {"opportunity": _summary(s, opp), "already_posted": already}


# ---- monitoring ------------------------------------------------------------
@router.post("/monitor/run", dependencies=Auth)
def run_monitor(request: Request, subreddit: str | None = None):
    """Trigger one monitoring cycle now (synchronous). Useful for local testing."""
    return request.app.state.monitor.run_once(only=subreddit).to_dict()


@router.get("/monitor/status", dependencies=Auth)
def monitor_status(s: SessionDep):
    subs = [
        {"name": r.name, "enabled": r.enabled, "last_run_at": r.last_run_at, "last_seen_created_utc": r.last_seen_created_utc, "last_error": r.last_error}
        for r in s.scalars(select(MonitoredSubreddit).order_by(MonitoredSubreddit.name))
    ]
    return {"subreddits": subs, "counters": metrics.snapshot()}


class SubredditIn(BaseModel):
    name: str
    enabled: bool = True


@router.post("/subreddits", dependencies=Auth, status_code=201)
def add_subreddit(body: SubredditIn, s: SessionDep):
    name = body.name.strip().removeprefix("r/")
    if not name.replace("_", "").isalnum():
        raise HTTPException(422, "invalid subreddit name")
    if s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name.ilike(name))):
        raise HTTPException(409, "already monitored")
    s.add(MonitoredSubreddit(name=name, enabled=body.enabled))
    s.commit()
    return {"name": name, "enabled": body.enabled}


@router.patch("/subreddits/{name}", dependencies=Auth)
def toggle_subreddit(name: str, body: SubredditIn, s: SessionDep):
    row = s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name.ilike(name)))
    if not row:
        raise HTTPException(404, "not found")
    row.enabled = body.enabled
    s.commit()
    return {"name": row.name, "enabled": row.enabled}


@router.delete("/subreddits/{name}", dependencies=Auth, status_code=204)
def delete_subreddit(name: str, s: SessionDep):
    row = s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name.ilike(name)))
    if not row:
        raise HTTPException(404, "not found")
    s.delete(row)
    s.commit()

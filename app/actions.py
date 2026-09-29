import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import metrics
from app.config import Settings
from app.models import ActionHistory, Opportunity, OpportunityStatus, RecommendedAction
from app.reddit.base import RedditClient, RedditError
from app.util import log_event, utcnow

log = logging.getLogger(__name__)
S = OpportunityStatus


class ActionError(Exception):
    """Invalid operation for the opportunity's current state. `code` maps to an HTTP status."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


class OpportunityActionService:
    """Human approval workflow + the only code path that writes to Reddit."""

    EDITABLE = {S.NEW, S.REVIEW_REQUIRED, S.APPROVED, S.FAILED, S.MONITORING}
    APPROVABLE = {S.NEW, S.REVIEW_REQUIRED, S.FAILED}
    CLOSABLE = {S.NEW, S.REVIEW_REQUIRED, S.APPROVED, S.FAILED, S.MONITORING}

    def __init__(self, session: Session, reddit: RedditClient, settings: Settings):
        self.s = session
        self.reddit = reddit
        self.settings = settings

    def _get(self, opp_id: int) -> Opportunity:
        opp = self.s.get(Opportunity, opp_id)
        if not opp:
            raise ActionError("opportunity not found", 404)
        return opp

    def _log(self, opp: Opportunity, action: str, to: S, detail: str | None = None) -> None:
        self.s.add(ActionHistory(opportunity_id=opp.id, action=action, from_status=opp.status, to_status=to.value, detail=detail))
        opp.status = to.value

    # ---- edit ------------------------------------------------------------
    def edit_response(self, opp_id: int, text: str) -> Opportunity:
        opp = self._get(opp_id)
        if S(opp.status) not in self.EDITABLE:
            raise ActionError(f"cannot edit response while status is {opp.status}")
        if opp.recommended_action not in {a.value for a in RecommendedAction if a.value.startswith(("REPLY", "PREPARE"))}:
            raise ActionError("this opportunity has no draft to edit")
        if not text.strip():
            raise ActionError("response cannot be empty", 422)
        opp.response_text = text
        if S(opp.status) in {S.APPROVED, S.FAILED}:  # edited text must be re-approved
            self._log(opp, "EDITED", S.REVIEW_REQUIRED, "edited after approval; re-approval required")
        else:
            self.s.add(ActionHistory(opportunity_id=opp.id, action="EDITED", from_status=opp.status, to_status=opp.status))
        self.s.commit()
        return opp

    # ---- approval ----------------------------------------------------------
    def approve(self, opp_id: int) -> Opportunity:
        opp = self._get(opp_id)
        if S(opp.status) not in self.APPROVABLE:
            raise ActionError(f"cannot approve from status {opp.status}")
        if opp.recommended_action not in (RecommendedAction.REPLY_TO_POST.value, RecommendedAction.REPLY_TO_COMMENT.value, RecommendedAction.PREPARE_PRIVATE_MESSAGE.value):
            raise ActionError("only opportunities with a proposed reply/message can be approved")
        if not (opp.response_text or "").strip():
            raise ActionError("a non-empty response is required before approval", 422)
        self._log(opp, "APPROVED", S.APPROVED)
        opp.last_error = None
        self.s.commit()
        return opp

    def reject(self, opp_id: int) -> Opportunity:
        return self._close(opp_id, S.REJECTED, "REJECTED")

    def ignore(self, opp_id: int) -> Opportunity:
        return self._close(opp_id, S.IGNORED, "IGNORED")

    def _close(self, opp_id: int, to: S, action: str) -> Opportunity:
        opp = self._get(opp_id)
        if S(opp.status) not in self.CLOSABLE:
            raise ActionError(f"cannot {action.lower()} from status {opp.status}")
        self._log(opp, action, to)
        self.s.commit()
        return opp

    # ---- execution ---------------------------------------------------------
    def execute(self, opp_id: int) -> tuple[Opportunity, bool]:
        """Post the approved reply. Returns (opportunity, already_posted).

        Idempotency: the APPROVED->POSTING transition is a single conditional UPDATE, so only one
        caller can win; a repeat call after success returns the stored result without touching Reddit.
        """
        opp = self._get(opp_id)
        if S(opp.status) == S.POSTED:
            return opp, True
        if opp.recommended_action == RecommendedAction.PREPARE_PRIVATE_MESSAGE.value:
            raise ActionError("private messages are draft-only and are never sent automatically; send it manually on Reddit")
        if S(opp.status) != S.APPROVED:
            raise ActionError(f"opportunity must be APPROVED to execute (currently {opp.status})")
        if not self.settings.reddit_posting_enabled:
            raise ActionError("posting is disabled (set REDDIT_POSTING_ENABLED=true)", 403)
        if self._posted_today() >= self.settings.max_posts_per_day:
            raise ActionError("daily posting limit reached", 429)

        res = self.s.execute(
            update(Opportunity).where(Opportunity.id == opp_id, Opportunity.status == S.APPROVED.value).values(status=S.POSTING.value)
        )
        self.s.commit()
        if res.rowcount != 1:
            self.s.refresh(opp)
            if S(opp.status) == S.POSTED:
                return opp, True
            raise ActionError("execution already in progress or state changed")
        self.s.refresh(opp)
        self.s.add(ActionHistory(opportunity_id=opp.id, action="EXECUTING", from_status=S.APPROVED.value, to_status=S.POSTING.value))

        try:
            if opp.recommended_action == RecommendedAction.REPLY_TO_COMMENT.value:
                reply = self.reddit.reply_to_comment(opp.target_comment_fullname, opp.response_text)  # type: ignore[arg-type]
            else:
                reply = self.reddit.reply_to_post(opp.post_fullname, opp.response_text)  # type: ignore[arg-type]
        except RedditError as exc:
            opp.last_error = str(exc)[:500]
            self._log(opp, "EXECUTION_FAILED", S.FAILED, opp.last_error)
            self.s.commit()
            metrics.inc("actions_failed")
            log_event(log, "action.failed", level=logging.ERROR, opportunity_id=opp.id, error=opp.last_error)
            return opp, False
        opp.posted_reddit_fullname = reply.fullname
        opp.posted_at = utcnow()
        self._log(opp, "POSTED", S.POSTED, reply.fullname)
        self.s.commit()
        metrics.inc("actions_posted")
        log_event(log, "action.posted", opportunity_id=opp.id, action=opp.recommended_action)
        return opp, False

    def _posted_today(self) -> int:
        since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.s.scalar(
            select(func.count()).select_from(ActionHistory).where(ActionHistory.action == "POSTED", ActionHistory.created_at >= since)
        ) or 0

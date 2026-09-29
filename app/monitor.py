import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import metrics
from app.analysis import OpportunityAnalysisService, activity_count
from app.config import MonitoringConfig
from app.filtering import CandidateFilter
from app.llm.base import LLMError
from app.models import (
    ActionHistory,
    MonitoredSubreddit,
    Opportunity,
    OpportunityStatus,
    RedditItem,
)
from app.reddit.base import (
    Conversation,
    RedditClient,
    RedditTransientError,
    RedditUnavailableError,
)
from app.util import log_event, utcnow

log = logging.getLogger(__name__)


@dataclass
class SubredditStats:
    subreddit: str
    fetched: int = 0
    duplicates: int = 0
    filtered: int = 0
    queued: int = 0
    sent_to_llm: int = 0
    opportunities: int = 0  # actionable: reply/DM
    monitoring: int = 0
    no_action: int = 0
    reevaluated: int = 0
    llm_failures: int = 0
    reddit_failures: int = 0
    error: str | None = None


@dataclass
class RunStats:
    skipped: bool = False
    subreddits: list[SubredditStats] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class _Budget:
    def __init__(self, n: int):
        self.left = n


class RedditMonitoringService:
    def __init__(
        self,
        session_factory: sessionmaker,
        reddit: RedditClient,
        analysis: OpportunityAnalysisService,
        candidate_filter: CandidateFilter,
        cfg: MonitoringConfig,
    ):
        self.sf = session_factory
        self.reddit = reddit
        self.analysis = analysis
        self.filter = candidate_filter
        self.cfg = cfg
        self._lock = threading.Lock()

    # ---- subreddit registry -------------------------------------------------
    def seed_subreddits(self) -> None:
        with self.sf() as s:
            existing = {r.name.lower() for r in s.scalars(select(MonitoredSubreddit))}
            for name in self.cfg.subreddits:
                if name.lower() not in existing:
                    s.add(MonitoredSubreddit(name=name))
            s.commit()

    # ---- one monitoring cycle -----------------------------------------------
    def run_once(self, only: str | None = None) -> RunStats:
        if not self._lock.acquire(blocking=False):
            log_event(log, "monitor.skipped_already_running", level=logging.WARNING)
            return RunStats(skipped=True)
        try:
            stats = RunStats()
            log_event(log, "monitor.start")
            with self.sf() as s:
                q = select(MonitoredSubreddit.name).where(MonitoredSubreddit.enabled.is_(True))
                if only:
                    q = q.where(MonitoredSubreddit.name.ilike(only))
                names = list(s.scalars(q))
            budget = _Budget(self.cfg.fetch.max_llm_calls_per_run)
            for name in names:
                st = SubredditStats(subreddit=name)
                stats.subreddits.append(st)
                try:  # one failing subreddit must not stop the others
                    self._process_subreddit(name, st, budget)
                except Exception as exc:  # noqa: BLE001
                    st.error = f"{type(exc).__name__}: {exc}"[:300]
                    metrics.inc("subreddit_failures")
                    log_event(log, "monitor.subreddit_failed", level=logging.ERROR, subreddit=name, error=st.error)
                    with self.sf() as s:
                        row = s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name == name))
                        if row:
                            row.last_error = st.error
                            s.commit()
                log_event(log, "monitor.subreddit_done", **asdict(st))
            metrics.inc("monitor_runs")
            log_event(log, "monitor.end", subreddits=len(names))
            return stats
        finally:
            self._lock.release()

    def _process_subreddit(self, name: str, st: SubredditStats, budget: _Budget) -> None:
        with self.sf() as s:
            sub = s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name == name))
            self._ingest(s, sub, st)
            s.commit()
            try:
                self._analyze_pending(s, name, st, budget)
                self._reevaluate_monitoring(s, name, st, budget)
            except RedditTransientError as exc:
                st.reddit_failures += 1
                metrics.inc("reddit_failures")
                log_event(log, "monitor.reddit_failure", level=logging.ERROR, subreddit=name, error=str(exc)[:200])
            sub.last_run_at = utcnow()
            sub.last_error = None
            s.commit()

    # ---- stage 1: fetch + dedupe + prefilter --------------------------------
    def _ingest(self, s: Session, sub: MonitoredSubreddit, st: SubredditStats) -> None:
        since = sub.last_seen_created_utc
        if since is None:
            since = time.time() - self.cfg.fetch.initial_lookback_hours * 3600
        posts = self.reddit.get_new_posts(sub.name, since, self.cfg.fetch.max_posts_per_run)
        st.fetched = len(posts)
        metrics.inc("posts_fetched", len(posts))
        for post in reversed(posts):  # oldest first
            if s.scalar(select(RedditItem.id).where(RedditItem.fullname == post.fullname)):
                st.duplicates += 1
                continue
            reason = self.filter.reject_reason(post)
            s.add(
                RedditItem(
                    fullname=post.fullname, kind="POST", subreddit=sub.name, post_id=post.id, author=post.author,
                    title=post.title, body=post.body, permalink=post.permalink, created_utc=post.created_utc,
                    state="FILTERED" if reason else "PENDING", filter_reason=reason,
                )
            )
            if reason:
                st.filtered += 1
                metrics.inc("posts_filtered")
            else:
                st.queued += 1
                if not s.scalar(select(Opportunity.id).where(Opportunity.post_id == post.id)):
                    s.add(Opportunity(subreddit=sub.name, post_id=post.id, post_fullname=post.fullname))
            if sub.last_seen_created_utc is None or post.created_utc > sub.last_seen_created_utc:
                sub.last_seen_created_utc = post.created_utc
                sub.last_seen_fullname = post.fullname
        # first run with no posts: still advance so we don't re-scan the lookback window forever
        if sub.last_seen_created_utc is None:
            sub.last_seen_created_utc = since
        s.flush()

    # ---- stage 2: LLM analysis of queued (NEW) opportunities -----------------
    def _analyze_pending(self, s: Session, name: str, st: SubredditStats, budget: _Budget) -> None:
        opps = list(
            s.scalars(
                select(Opportunity)
                .where(Opportunity.subreddit == name, Opportunity.status == OpportunityStatus.NEW.value)
                .order_by(Opportunity.id)
            )
        )
        for opp in opps:
            if budget.left <= 0:
                log_event(log, "monitor.llm_budget_exhausted", level=logging.WARNING, subreddit=name)
                return
            convo = self._fetch_convo(s, opp, st)
            if convo is None:
                continue
            self._run_analysis(s, opp, convo, st, budget)

    # ---- stage 3: reconsider MONITORING conversations ------------------------
    def _reevaluate_monitoring(self, s: Session, name: str, st: SubredditStats, budget: _Budget) -> None:
        rc = self.cfg.reevaluation
        now = utcnow()
        opps = list(
            s.scalars(
                select(Opportunity).where(
                    Opportunity.subreddit == name, Opportunity.status == OpportunityStatus.MONITORING.value
                ).order_by(Opportunity.last_analyzed_at)
            )
        )
        for opp in opps:
            if now - opp.created_at > timedelta(days=rc.max_age_days):
                self._transition(s, opp, OpportunityStatus.IGNORED, "monitoring expired")
                continue
            if opp.last_analyzed_at and now - opp.last_analyzed_at < timedelta(hours=rc.cooldown_hours):
                continue
            if budget.left <= 0:
                return
            convo = self._fetch_convo(s, opp, st)
            if convo is None:
                continue
            if activity_count(convo) - opp.last_analyzed_comment_count < rc.min_new_comments:
                continue  # unchanged conversation: do not spend an LLM call
            st.reevaluated += 1
            self._run_analysis(s, opp, convo, st, budget)

    # ---- helpers -------------------------------------------------------------
    def _fetch_convo(self, s: Session, opp: Opportunity, st: SubredditStats) -> Conversation | None:
        try:
            convo = self.reddit.get_conversation(opp.post_id, self.cfg.fetch.max_comments_per_thread)
        except RedditUnavailableError:
            self._transition(s, opp, OpportunityStatus.IGNORED, "thread unavailable")
            s.commit()
            return None
        if convo.post.removed:
            self._transition(s, opp, OpportunityStatus.IGNORED, "post deleted/removed")
            s.commit()
            return None
        return convo

    def _run_analysis(self, s: Session, opp: Opportunity, convo: Conversation, st: SubredditStats, budget: _Budget) -> None:
        budget.left -= 1
        st.sent_to_llm += 1
        metrics.inc("llm_calls")
        try:
            outcome = self.analysis.analyze(s, opp, convo)
        except LLMError as exc:
            # Reddit item is already persisted; opportunity stays NEW/MONITORING and is retried next cycle.
            s.rollback()
            st.llm_failures += 1
            metrics.inc("llm_failures")
            log_event(log, "monitor.llm_failure", level=logging.ERROR, subreddit=opp.subreddit, post_id=opp.post_id, error=str(exc)[:200])
            row = s.get(Opportunity, opp.id)
            if row:
                row.last_error = f"LLM: {exc}"[:500]
            s.commit()
            return
        self._store_comments(s, convo)
        item = s.scalar(select(RedditItem).where(RedditItem.fullname == opp.post_fullname))
        if item:
            item.state = "ANALYZED"
        if outcome.status == OpportunityStatus.REVIEW_REQUIRED:
            st.opportunities += 1
            metrics.inc("opportunities_found")
        elif outcome.status == OpportunityStatus.MONITORING:
            st.monitoring += 1
        else:
            st.no_action += 1
        s.commit()

    def _store_comments(self, s: Session, convo: Conversation) -> None:
        have = set(
            s.scalars(select(RedditItem.fullname).where(RedditItem.post_id == convo.post.id, RedditItem.kind == "COMMENT"))
        )
        for c in convo.comments:
            if c.removed or c.fullname in have:
                continue
            s.add(
                RedditItem(
                    fullname=c.fullname, kind="COMMENT", subreddit=c.subreddit, post_id=c.post_id, comment_id=c.id,
                    parent_fullname=c.parent_fullname, author=c.author, body=c.body, permalink=c.permalink,
                    created_utc=c.created_utc, state="ANALYZED",
                )
            )

    def _transition(self, s: Session, opp: Opportunity, to: OpportunityStatus, detail: str) -> None:
        s.add(ActionHistory(opportunity_id=opp.id, action="SYSTEM", from_status=opp.status, to_status=to.value, detail=detail))
        opp.status = to.value

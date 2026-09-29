import time

import pytest
from fastapi.testclient import TestClient

from app.analysis import OpportunityAnalysisService
from app.config import MonitoringConfig, Settings
from app.db import init_db, make_engine, make_session_factory
from app.dev.fakes import FakeLLMClient, FakeRedditClient
from app.filtering import KeywordCandidateFilter
from app.main import create_app
from app.monitor import RedditMonitoringService
from app.reddit.base import RedditComment, RedditPost


def make_post(pid="p1", sub="SaaS", title="Looking for a tool to track customer feedback?", body="Any recommendations for a small team? We are drowning in email.", age=600, author="alice", **kw):
    return RedditPost(id=pid, subreddit=sub, author=author, title=title, body=body,
                      permalink=f"https://www.reddit.com/r/{sub}/comments/{pid}/", created_utc=time.time() - age, **kw)


def make_comment(cid, post_id="p1", sub="SaaS", author="bob", body="I use spreadsheets, works ok for us.", parent=None):
    return RedditComment(id=cid, post_id=post_id, subreddit=sub, author=author, body=body,
                         parent_fullname=parent or f"t3_{post_id}", permalink=f"https://www.reddit.com/r/{sub}/comments/{post_id}/x/{cid}/",
                         created_utc=time.time() - 300)


@pytest.fixture
def cfg():
    c = MonitoringConfig(subreddits=["SaaS", "startups"], keywords=["customer feedback"])
    c.reevaluation.cooldown_hours = 0
    return c


@pytest.fixture
def settings():
    return Settings(database_url="sqlite://", reddit_posting_enabled=True, reddit_username="ourbrand", monitor_enabled=False, _env_file=None)


@pytest.fixture
def sf():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def reddit():
    return FakeRedditClient()


@pytest.fixture
def llm():
    return FakeLLMClient()


@pytest.fixture
def monitor(sf, reddit, llm, cfg):
    analysis = OpportunityAnalysisService(reddit, llm, cfg, "ourbrand")
    m = RedditMonitoringService(sf, reddit, analysis, KeywordCandidateFilter(cfg, "ourbrand"), cfg)
    m.seed_subreddits()
    return m


@pytest.fixture
def client(settings, reddit, llm, cfg, tmp_path):
    cfg_file = tmp_path / "m.yaml"
    cfg_file.write_text("subreddits: [SaaS]\nreevaluation: {cooldown_hours: 0}\n")
    settings.monitor_config_path = str(cfg_file)
    app = create_app(settings, reddit=reddit, llm=llm, start_scheduler=False)
    with TestClient(app) as c:
        c.app_ref = app
        yield c

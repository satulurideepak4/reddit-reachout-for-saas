import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.analysis import OpportunityAnalysisService
from app.api import router
from app.config import Settings, get_settings, load_monitoring_config
from app.db import init_db, make_engine, make_session_factory
from app.filtering import KeywordCandidateFilter
from app.llm.factory import build_llm_client
from app.monitor import RedditMonitoringService
from app.reddit.http_client import HttpRedditClient, PublicRedditClient
from app.scheduler import IntervalScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def build_reddit_client(settings: Settings, subreddits: list[str]):
    if settings.reddit_client == "fake":
        from app.dev.fakes import demo_reddit_client

        return demo_reddit_client(subreddits)
    if settings.reddit_client == "public":
        return PublicRedditClient(settings)
    return HttpRedditClient(settings)


def create_app(settings: Settings | None = None, reddit=None, llm=None, start_scheduler: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    cfg = load_monitoring_config(settings.monitor_config_path)
    engine = make_engine(settings.database_url)
    init_db(engine)
    sf = make_session_factory(engine)
    reddit = reddit or build_reddit_client(settings, cfg.subreddits)
    llm = llm or build_llm_client(settings)
    analysis = OpportunityAnalysisService(reddit, llm, cfg, settings.reddit_username)
    monitor = RedditMonitoringService(sf, reddit, analysis, KeywordCandidateFilter(cfg, settings.reddit_username), cfg)
    monitor.seed_subreddits()
    scheduler = IntervalScheduler(monitor.run_once, settings.monitor_interval_seconds, settings.monitor_initial_delay_seconds)
    run_sched = settings.monitor_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if run_sched:
            scheduler.start()
        yield
        if run_sched:
            scheduler.stop()

    app = FastAPI(title="Reddit Reachout Assistant", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = sf
    app.state.reddit = reddit
    app.state.monitor = monitor
    app.include_router(router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()

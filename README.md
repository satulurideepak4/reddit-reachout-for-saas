# Reddit Reachout Assistant

Monitors configured subreddits hourly, asks an LLM whether a conversation is a genuine opportunity for a
helpful reply, and stores proposals for **human review**. Nothing is posted without explicit
approve → execute, and private messages are **never** sent (draft-only).

Stack: Python 3.11, FastAPI, SQLAlchemy (SQLite by default), httpx, pytest.

## Pipeline

```
IntervalScheduler (app/scheduler.py)          <- swap for Quartz/Celery/Kafka; job = monitor.run_once
  -> RedditMonitoringService (app/monitor.py)  per-subreddit isolation, checkpoints, LLM budget
     -> RedditClient (app/reddit/)             OAuth, pagination, retries/backoff, 429 handling
     -> CandidateFilter (app/filtering.py)     cheap, lenient pre-filter (replaceable)
     -> OpportunityAnalysisService (app/analysis.py)
        -> LLMClient (app/llm/)                forced-tool structured output -> typed OpportunityDecision
        + deterministic guard rails (thresholds, unknown target comment, already-commented => NO_ACTION)
  -> Opportunity rows (app/models.py)
  -> REST API (app/api.py) -> OpportunityActionService (app/actions.py): edit / approve / reject / ignore / execute
```

Statuses: `NEW` (queued, awaiting LLM; retried each cycle) → `REVIEW_REQUIRED` / `MONITORING` / `IGNORED`
→ `APPROVED` → `POSTING` (transient lock) → `POSTED` | `FAILED`; also `REJECTED`.

Actions → status: `REPLY_TO_POST`, `REPLY_TO_COMMENT`, `PREPARE_PRIVATE_MESSAGE` → `REVIEW_REQUIRED`;
`MONITOR` → `MONITORING` (re-analysed only when ≥ `min_new_comments` new comments appear, after a cooldown,
expiring after `max_age_days`); `NO_ACTION` → `IGNORED`.
Score = the LLM's `relevanceScore` (0–1), stored with `confidence`; sort with `?sort=score`.

## Database (SQLite via SQLAlchemy; tables created on startup — no migration tool in this repo yet)

| table | purpose |
|---|---|
| `monitored_subreddits` | watched subreddits + checkpoint (`last_seen_created_utc`, last run/error) |
| `reddit_items` | posts/comments seen (unique `fullname` = dedupe); post `state` PENDING/ANALYZED/FILTERED |
| `opportunities` | one per thread: current decision, score, editable `response_text`, re-analysis state, post result |
| `llm_decisions` | every LLM run (raw structured output, final action after guard rails) |
| `action_history` | every status transition / Reddit action (audit) |

## Configuration

Copy `.env.example` to `.env` (never commit it).

* **Reddit**: create a *script* app at https://www.reddit.com/prefs/apps → `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`,
  plus `REDDIT_USERNAME`, `REDDIT_PASSWORD` (the account that will reply) and a descriptive `REDDIT_USER_AGENT`.
* **LLM**: `LLM_PROVIDER=anthropic`, `LLM_API_KEY`, `LLM_MODEL`. Add providers by implementing `LLMClient.decide` (app/llm/base.py) and registering in `app/llm/factory.py`.
* **What to watch**: edit `config/monitoring.yaml` (subreddits, product description/guidelines, keywords, thresholds, limits).
  YAML subreddits are *seeded* into the DB at startup; afterwards manage them at runtime with
  `POST /api/reddit/subreddits {"name": "indiehackers"}`, `PATCH /api/reddit/subreddits/{name} {"name":"..","enabled":false}`, `DELETE ...`.
* **Safety**: `REDDIT_POSTING_ENABLED=false` by default (execute returns 403); `MAX_POSTS_PER_DAY` caps posts; optional `API_TOKEN` (Bearer) protects the API.

## Run

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
uvicorn --factory app.main:app_factory --port 8000     # scheduler starts with the app (MONITOR_ENABLED=true)
pytest                                                 # all external calls mocked
```

Read real posts with **no Reddit credentials** (read-only, unauthenticated public JSON; Reddit may rate-limit or block some IPs):
`REDDIT_CLIENT=public LLM_PROVIDER=fake python -m app.cli run-once --subreddit SaaS`

Try it with **no credentials** (canned offline Reddit data + fake LLM):
`REDDIT_CLIENT=fake LLM_PROVIDER=fake uvicorn --factory app.main:app_factory`.

### Trigger one cycle now

```bash
curl -X POST localhost:8000/api/reddit/monitor/run                   # all subreddits
curl -X POST "localhost:8000/api/reddit/monitor/run?subreddit=SaaS"  # one
python -m app.cli run-once [--subreddit SaaS]                        # without the server
```
Returns per-subreddit counters (fetched / duplicates / filtered / sent_to_llm / opportunities / failures).
`GET /api/reddit/monitor/status` shows checkpoints, last errors and process counters. Logs are JSON lines (`monitor.*`, `reddit.*`, `action.*`); secrets are never logged.

### Review workflow

```bash
curl "localhost:8000/api/reddit/opportunities?status=REVIEW_REQUIRED&action=REPLY_TO_POST&subreddit=SaaS&sort=score"
curl localhost:8000/api/reddit/opportunities/1          # original post/comments, permalink, LLM reason, decisions, history
curl -X PATCH localhost:8000/api/reddit/opportunities/1/response -H 'content-type: application/json' -d '{"response_text":"..."}'
curl -X POST localhost:8000/api/reddit/opportunities/1/approve   # -> APPROVED  (editing afterwards requires re-approval)
curl -X POST localhost:8000/api/reddit/opportunities/1/execute   # -> POSTED (idempotent; needs REDDIT_POSTING_ENABLED=true)
curl -X POST localhost:8000/api/reddit/opportunities/1/reject    # or /ignore
```
`PREPARE_PRIVATE_MESSAGE` opportunities show `target_author` + draft; `execute` always refuses — send it yourself on Reddit.

## Known limitations

* Discovery is by **new posts** per subreddit plus the comment threads of those posts; new comments on older
  posts are only picked up for `MONITORING` opportunities (no subreddit-wide comment stream yet).
* Reddit comment fetch is capped (`max_comments_per_thread`); activity uses Reddit's `num_comments`.
* If the process dies mid-post, the opportunity stays `POSTING`; check Reddit manually before resetting it (deliberately not auto-retried to avoid duplicates). Write calls are never retried on 5xx/network errors for the same reason.
* Reddit’s API terms/commercial-use rules and subreddit self-promotion rules apply; keep human review in the loop.
* Single Reddit account, in-process scheduler, SQLite; no UI beyond the REST API.

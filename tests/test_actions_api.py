from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app.models import ActionHistory
from tests.conftest import make_comment, make_post
from tests.test_pipeline import decision


def seed(client, reddit, llm, action="REPLY_TO_POST", **kw):
    reddit.posts.setdefault("SaaS", []).append(make_post("p1"))
    reddit.comments["p1"] = [make_comment("c1", "p1", author="carol")]
    llm.responses = [decision(action, **kw)]
    r = client.post("/api/reddit/monitor/run", params={"subreddit": "SaaS"})
    assert r.status_code == 200
    return client.get("/api/reddit/opportunities").json()[0]


def test_seed_pipeline_and_listing_filters(client, reddit, llm):
    o = seed(client, reddit, llm)
    assert o["status"] == "REVIEW_REQUIRED" and o["permalink"].startswith("https://www.reddit.com")
    assert client.get("/api/reddit/opportunities", params={"subreddit": "saas", "action": "REPLY_TO_POST", "status": "REVIEW_REQUIRED"}).json()
    assert client.get("/api/reddit/opportunities", params={"status": "POSTED"}).json() == []
    d = client.get(f"/api/reddit/opportunities/{o['id']}").json()
    assert d["post"]["title"] and d["decisions"][0]["raw"]["recommendedAction"] == "REPLY_TO_POST" and d["comments"]


def test_unapproved_cannot_execute(client, reddit, llm):
    o = seed(client, reddit, llm)
    r = client.post(f"/api/reddit/opportunities/{o['id']}/execute")
    assert r.status_code == 409 and reddit.replies == []


def test_approve_edit_execute_flow(client, reddit, llm):
    o = seed(client, reddit, llm)
    i = o["id"]
    assert client.patch(f"/api/reddit/opportunities/{i}/response", json={"response_text": "My edited reply"}).status_code == 200
    assert client.post(f"/api/reddit/opportunities/{i}/approve").json()["status"] == "APPROVED"
    # editing after approval forces re-approval
    client.patch(f"/api/reddit/opportunities/{i}/response", json={"response_text": "Edited again"})
    assert client.post(f"/api/reddit/opportunities/{i}/execute").status_code == 409
    client.post(f"/api/reddit/opportunities/{i}/approve")
    r = client.post(f"/api/reddit/opportunities/{i}/execute")
    assert r.status_code == 200 and r.json()["opportunity"]["status"] == "POSTED"
    assert reddit.replies == [("t3_p1", "Edited again")]


def test_double_execute_posts_once(client, reddit, llm):
    o = seed(client, reddit, llm)
    i = o["id"]
    client.post(f"/api/reddit/opportunities/{i}/approve")
    r1 = client.post(f"/api/reddit/opportunities/{i}/execute").json()
    r2 = client.post(f"/api/reddit/opportunities/{i}/execute").json()
    assert (r1["already_posted"], r2["already_posted"]) == (False, True)
    assert len(reddit.replies) == 1


def test_concurrent_execute_posts_once(client, reddit, llm):
    o = seed(client, reddit, llm)
    i = o["id"]
    client.post(f"/api/reddit/opportunities/{i}/approve")
    with ThreadPoolExecutor(4) as ex:
        codes = list(ex.map(lambda _: client.post(f"/api/reddit/opportunities/{i}/execute").status_code, range(4)))
    assert len(reddit.replies) == 1 and 200 in codes


def test_reply_to_comment_targets_comment(client, reddit, llm):
    o = seed(client, reddit, llm, action="REPLY_TO_COMMENT", targetCommentId="c1")
    client.post(f"/api/reddit/opportunities/{o['id']}/approve")
    client.post(f"/api/reddit/opportunities/{o['id']}/execute")
    assert reddit.replies[0][0] == "t1_c1"


def test_private_message_never_sent(client, reddit, llm):
    o = seed(client, reddit, llm, action="PREPARE_PRIVATE_MESSAGE", targetCommentId="c1")
    assert o["target_author"] == "carol" and o["response_text"]
    i = o["id"]
    client.post(f"/api/reddit/opportunities/{i}/approve")
    r = client.post(f"/api/reddit/opportunities/{i}/execute")
    assert r.status_code == 409 and "manually" in r.json()["detail"]
    assert reddit.replies == []


def test_reject_and_ignore(client, reddit, llm):
    o = seed(client, reddit, llm)
    assert client.post(f"/api/reddit/opportunities/{o['id']}/reject").json()["status"] == "REJECTED"
    assert client.post(f"/api/reddit/opportunities/{o['id']}/approve").status_code == 409
    assert client.post(f"/api/reddit/opportunities/{o['id']}/execute").status_code == 409


def test_reddit_failure_marks_failed_then_retry_needs_reapproval(client, reddit, llm):
    from app.reddit.base import RedditError

    o = seed(client, reddit, llm)
    i = o["id"]
    client.post(f"/api/reddit/opportunities/{i}/approve")
    reddit.reply_error = RedditError("boom")
    r = client.post(f"/api/reddit/opportunities/{i}/execute")
    assert r.json()["opportunity"]["status"] == "FAILED"
    reddit.reply_error = None
    assert client.post(f"/api/reddit/opportunities/{i}/execute").status_code == 409
    client.post(f"/api/reddit/opportunities/{i}/approve")
    assert client.post(f"/api/reddit/opportunities/{i}/execute").json()["opportunity"]["status"] == "POSTED"


def test_posting_disabled_and_daily_cap(client, reddit, llm):
    o = seed(client, reddit, llm)
    i = o["id"]
    client.post(f"/api/reddit/opportunities/{i}/approve")
    st = client.app_ref.state.settings
    st.reddit_posting_enabled = False
    assert client.post(f"/api/reddit/opportunities/{i}/execute").status_code == 403
    st.reddit_posting_enabled, st.max_posts_per_day = True, 0
    assert client.post(f"/api/reddit/opportunities/{i}/execute").status_code == 429
    assert reddit.replies == []


def test_subreddit_management_and_auth(client, settings):
    assert client.post("/api/reddit/subreddits", json={"name": "r/indiehackers"}).status_code == 201
    assert client.post("/api/reddit/subreddits", json={"name": "indiehackers"}).status_code == 409
    assert client.patch("/api/reddit/subreddits/indiehackers", json={"name": "indiehackers", "enabled": False}).json()["enabled"] is False
    assert client.delete("/api/reddit/subreddits/indiehackers").status_code == 204
    settings.api_token = "s3cret"
    assert client.get("/api/reddit/opportunities").status_code == 401
    assert client.get("/api/reddit/opportunities", headers={"Authorization": "Bearer s3cret"}).status_code == 200

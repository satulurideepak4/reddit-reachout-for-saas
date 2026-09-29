import httpx
import pytest

from app.config import Settings
from app.reddit.base import RedditError, RedditTransientError, RedditUnavailableError
from app.reddit.http_client import HttpRedditClient


def listing(ids, after=None, base=1_700_000_000):
    return {"data": {"after": after, "children": [
        {"kind": "t3", "data": {"id": i, "subreddit": "SaaS", "author": "a", "title": "t", "selftext": "b", "permalink": f"/r/SaaS/{i}/",
                                "created_utc": base - n, "num_comments": 0}} for n, i in enumerate(ids)]}}


def make_client(handler, **kw):
    sleeps = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    s = Settings(reddit_client_id="id", reddit_client_secret="sec", reddit_username="u", reddit_password="p", _env_file=None)
    c = HttpRedditClient(s, http=http, sleep=sleeps.append, **kw)
    return c, sleeps


def token_or(handler):
    def h(req):
        if "access_token" in str(req.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(req)
    return h


def test_429_honors_retry_after_then_succeeds():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=listing(["a"]))

    c, sleeps = make_client(token_or(h))
    posts = c.get_new_posts("SaaS", None, 10)
    assert [p.id for p in posts] == ["a"] and sleeps == [7.0, 7.0]


def test_retries_exhausted_raises_transient():
    c, sleeps = make_client(token_or(lambda r: httpx.Response(503)), max_retries=2)
    with pytest.raises(RedditTransientError):
        c.get_new_posts("SaaS", None, 10)
    assert len(sleeps) == 2 and sleeps == [1.0, 2.0]  # exponential backoff


def test_pagination_stops_at_checkpoint():
    base = 1_700_000_000

    def h(req):
        if req.url.params.get("after"):
            return httpx.Response(200, json=listing(["c", "d"], base=base - 10))
        return httpx.Response(200, json=listing(["a", "b"], after="t3_b"))

    c, _ = make_client(token_or(h))
    assert [p.id for p in c.get_new_posts("SaaS", None, 10)] == ["a", "b", "c", "d"]
    # checkpoint between b and c: page 2 items are older, so stop
    assert [p.id for p in c.get_new_posts("SaaS", base - 5, 10)] == ["a", "b"]


def test_unavailable_not_retried():
    c, sleeps = make_client(token_or(lambda r: httpx.Response(404)))
    with pytest.raises(RedditUnavailableError):
        c.get_conversation("x", 10)
    assert sleeps == []


def test_write_not_retried_on_5xx_but_retried_on_429():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(500)

    c, sleeps = make_client(token_or(h))
    with pytest.raises(RedditError):
        c.reply_to_post("t3_a", "hi")
    assert len(calls) == 1 and sleeps == []

    seq = iter([httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_new", "permalink": "/x"}}]}}})])
    c, sleeps = make_client(token_or(lambda r: next(seq)))
    assert c.reply_to_post("t3_a", "hi").fullname == "t1_new" and sleeps == [2.0]


def test_conversation_parsing_flattens_and_flags_deleted():
    post = listing(["a"])["data"]["children"]
    cm = lambda i, body, author="x", replies="": {"kind": "t1", "data": {"id": i, "author": author, "body": body, "parent_id": "t3_a", "permalink": f"/c/{i}", "created_utc": 1.0, "replies": replies}}
    child = cm("c2", "nested")
    tree = [cm("c1", "top", replies={"data": {"children": [child]}}), cm("c3", "[deleted]", author="[deleted]"), {"kind": "more", "data": {}}]
    payload = [{"data": {"children": post}}, {"data": {"children": tree}}]
    c, _ = make_client(token_or(lambda r: httpx.Response(200, json=payload)))
    convo = c.get_conversation("a", 50)
    assert [(x.id, x.removed) for x in convo.comments] == [("c1", False), ("c2", False), ("c3", True)]

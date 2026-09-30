import logging
import time
from typing import Any, Callable

import httpx

from app.config import Settings
from app.reddit.base import (
    Conversation,
    PostedReply,
    RedditComment,
    RedditError,
    RedditPost,
    RedditTransientError,
    RedditUnavailableError,
)
from app.util import log_event

log = logging.getLogger(__name__)

OAUTH_BASE = "https://oauth.reddit.com"
PUBLIC_BASE = "https://www.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REMOVED_MARKERS = {"[deleted]", "[removed]"}


class HttpRedditClient:
    """Reddit OAuth (script app / password grant) client with retry, backoff and 429 handling."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        max_wait: float = 120.0,
    ):
        self.s = settings
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": settings.reddit_user_agent})
        self.sleep = sleep
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_wait = max_wait
        self._token: str | None = None
        self._token_expiry = 0.0

    # ---- auth -------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.http.post(
            TOKEN_URL,
            auth=(self.s.reddit_client_id, self.s.reddit_client_secret),
            data={"grant_type": "password", "username": self.s.reddit_username, "password": self.s.reddit_password},
            headers={"User-Agent": self.s.reddit_user_agent},
        )
        if resp.status_code != 200 or "access_token" not in resp.json():
            raise RedditError(f"Reddit auth failed (status {resp.status_code})")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3600))
        return self._token

    _base = OAUTH_BASE
    _suffix = ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self._get_token()}", "User-Agent": self.s.reddit_user_agent}

    # ---- low-level request with retries -----------------------------------
    def _request(self, method: str, path: str, *, idempotent: bool = True, **kwargs: Any) -> dict | list:
        """idempotent=False (writes): retry only on 429, where Reddit did not process the request."""
        attempt = 0
        while True:
            attempt += 1
            retry_reason: str | None = None
            wait = min(self.backoff_base * 2 ** (attempt - 1), self.max_wait)
            try:
                resp = self.http.request(method, f"{self._base}{path}{self._suffix}", headers=self._headers(), **kwargs)
            except httpx.TransportError as exc:
                if not idempotent:
                    raise RedditError(f"network error during write (outcome unknown): {type(exc).__name__}") from exc
                retry_reason = f"network:{type(exc).__name__}"
            else:
                code = resp.status_code
                if code == 429:
                    wait = min(_retry_after(resp, wait), self.max_wait)
                    retry_reason = "rate_limited"
                    log_event(log, "reddit.rate_limited", level=logging.WARNING, path=path, wait_s=wait)
                elif code >= 500:
                    if not idempotent:
                        raise RedditError(f"Reddit {code} during write (outcome unknown)")
                    retry_reason = f"http_{code}"
                elif code in (403, 404):
                    raise RedditUnavailableError(f"Reddit {code} for {path}")
                elif code >= 400:
                    raise RedditError(f"Reddit {code} for {path}")
                else:
                    self._respect_rate_headers(resp)
                    return resp.json()
            if attempt > self.max_retries:
                raise RedditTransientError(f"giving up on {path} after {attempt} attempts ({retry_reason})")
            log_event(log, "reddit.retry", level=logging.WARNING, path=path, attempt=attempt, reason=retry_reason, wait_s=wait)
            self.sleep(wait)

    def _respect_rate_headers(self, resp: httpx.Response) -> None:
        try:
            remaining = float(resp.headers.get("x-ratelimit-remaining", "100"))
            reset = float(resp.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        if remaining < 2 and reset > 0:
            wait = min(reset, self.max_wait)
            log_event(log, "reddit.rate_limit_pause", level=logging.WARNING, wait_s=wait)
            self.sleep(wait)

    # ---- RedditClient API -------------------------------------------------
    def get_new_posts(self, subreddit: str, since_utc: float | None, limit: int) -> list[RedditPost]:
        posts: list[RedditPost] = []
        after: str | None = None
        while len(posts) < limit:
            params: dict[str, Any] = {"limit": min(100, limit - len(posts)), "raw_json": 1}
            if after:
                params["after"] = after
            data = self._request("GET", f"/r/{subreddit}/new", params=params)
            children = data["data"]["children"]  # type: ignore[index]
            reached_checkpoint = False
            for child in children:
                post = _parse_post(child["data"])
                if since_utc is not None and post.created_utc <= since_utc:
                    reached_checkpoint = True
                    break
                posts.append(post)
            after = data["data"].get("after")  # type: ignore[index]
            if reached_checkpoint or not after or not children:
                break
        return posts

    def get_conversation(self, post_id: str, limit: int) -> Conversation:
        data = self._request("GET", f"/comments/{post_id}", params={"limit": limit, "depth": 4, "sort": "top", "raw_json": 1})
        post = _parse_post(data[0]["data"]["children"][0]["data"])  # type: ignore[index]
        comments: list[RedditComment] = []
        _flatten(data[1]["data"]["children"], post, comments, limit)  # type: ignore[index]
        return Conversation(post=post, comments=comments)

    def get_comments(self, post_id: str, limit: int) -> list[RedditComment]:
        return self.get_conversation(post_id, limit).comments

    def reply_to_post(self, post_fullname: str, text: str) -> PostedReply:
        return self._comment(post_fullname, text)

    def reply_to_comment(self, comment_fullname: str, text: str) -> PostedReply:
        return self._comment(comment_fullname, text)

    def _comment(self, parent: str, text: str) -> PostedReply:
        data = self._request("POST", "/api/comment", idempotent=False, data={"api_type": "json", "thing_id": parent, "text": text})
        j = data["json"]  # type: ignore[index]
        if j.get("errors"):
            raise RedditError(f"Reddit rejected comment: {j['errors']}")
        thing = j["data"]["things"][0]["data"]
        return PostedReply(fullname=thing["name"], permalink=thing.get("permalink", ""))


def _retry_after(resp: httpx.Response, default: float) -> float:
    for h in ("retry-after", "x-ratelimit-reset"):
        try:
            return max(float(resp.headers[h]), 0.0)
        except (KeyError, ValueError):
            continue
    return default


def _is_removed(author: str | None, text: str) -> bool:
    return author in (None, "[deleted]") or text.strip() in REMOVED_MARKERS


def _parse_post(d: dict) -> RedditPost:
    body = d.get("selftext") or ""
    author = d.get("author")
    removed = bool(d.get("removed_by_category")) or body.strip() in REMOVED_MARKERS or author == "[deleted]"
    return RedditPost(
        id=d["id"], subreddit=d["subreddit"], author=author, title=d.get("title", ""), body=body,
        permalink="https://www.reddit.com" + d.get("permalink", ""), created_utc=float(d["created_utc"]),
        num_comments=int(d.get("num_comments", 0)), removed=removed,
    )


def _flatten(children: list, post: RedditPost, out: list[RedditComment], limit: int) -> None:
    for child in children:
        if len(out) >= limit:
            return
        if child.get("kind") != "t1":
            continue  # skip "more" stubs
        d = child["data"]
        body = d.get("body", "")
        author = d.get("author")
        out.append(
            RedditComment(
                id=d["id"], post_id=post.id, subreddit=post.subreddit, author=author, body=body,
                parent_fullname=d.get("parent_id", post.fullname),
                permalink="https://www.reddit.com" + d.get("permalink", ""),
                created_utc=float(d["created_utc"]), removed=_is_removed(author, body),
            )
        )
        replies = d.get("replies")
        if isinstance(replies, dict):
            _flatten(replies["data"]["children"], post, out, limit)


class PublicRedditClient(HttpRedditClient):
    """Read-only client for Reddit's unauthenticated public JSON (no credentials; REDDIT_CLIENT=public).

    Stricter rate limits than OAuth and Reddit may block some IPs (403/429). Cannot post.
    """

    _base = PUBLIC_BASE
    _suffix = ".json"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.s.reddit_user_agent}

    def _comment(self, parent: str, text: str) -> PostedReply:
        raise RedditError("public (unauthenticated) Reddit client is read-only; use REDDIT_CLIENT=real to post")

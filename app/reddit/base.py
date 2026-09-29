from dataclasses import dataclass, field
from typing import Protocol


class RedditError(Exception):
    """Generic Reddit failure."""


class RedditTransientError(RedditError):
    """Retries exhausted on a retryable failure (429/5xx/network)."""


class RedditUnavailableError(RedditError):
    """Thread/user gone, private, banned or forbidden (404/403)."""


@dataclass
class RedditPost:
    id: str
    subreddit: str
    author: str | None
    title: str
    body: str
    permalink: str
    created_utc: float
    num_comments: int = 0
    removed: bool = False

    @property
    def fullname(self) -> str:
        return f"t3_{self.id}"


@dataclass
class RedditComment:
    id: str
    post_id: str
    subreddit: str
    author: str | None
    body: str
    parent_fullname: str
    permalink: str
    created_utc: float
    removed: bool = False

    @property
    def fullname(self) -> str:
        return f"t1_{self.id}"


@dataclass
class Conversation:
    post: RedditPost
    comments: list[RedditComment] = field(default_factory=list)


@dataclass
class PostedReply:
    fullname: str
    permalink: str = ""


class RedditClient(Protocol):
    def get_new_posts(self, subreddit: str, since_utc: float | None, limit: int) -> list[RedditPost]:
        """Posts newer than since_utc, newest first, paginating as needed."""

    def get_comments(self, post_id: str, limit: int) -> list[RedditComment]: ...

    def get_conversation(self, post_id: str, limit: int) -> Conversation: ...

    def reply_to_post(self, post_fullname: str, text: str) -> PostedReply: ...

    def reply_to_comment(self, comment_fullname: str, text: str) -> PostedReply: ...

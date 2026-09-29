import re
from typing import Protocol

from app.config import MonitoringConfig
from app.reddit.base import RedditPost

HELP_SEEKING = re.compile(
    r"\?|looking for|recommend|alternative|any (tool|app|software|service)|how do (i|you|we)|"
    r"struggling|need (help|a|an)|anyone (use|know|tried)|suggestions?|what do you use|frustrat",
    re.IGNORECASE,
)


class CandidateFilter(Protocol):
    def reject_reason(self, post: RedditPost) -> str | None:
        """Return a short reason to skip the post, or None to send it on to the LLM."""


class KeywordCandidateFilter:
    """Cheap first-pass filter. Deliberately lenient: a help-seeking post passes even
    without a keyword match, so semantic opportunities are not lost."""

    def __init__(self, cfg: MonitoringConfig, own_username: str = ""):
        self.cfg = cfg
        self.own = own_username.lower()
        self.ignored = {a.lower() for a in cfg.ignored_authors}
        self.keywords = [k.lower() for k in cfg.keywords]

    def reject_reason(self, post: RedditPost) -> str | None:
        if post.removed or not post.author or post.author == "[deleted]":
            return "deleted_or_removed"
        author = post.author.lower()
        if author in self.ignored:
            return "ignored_author"
        if self.own and author == self.own:
            return "own_post"
        text = f"{post.title} {post.body}".strip()
        if len(text) < self.cfg.filter.min_text_length:
            return "low_information"
        if self.cfg.filter.require_signal:
            low = text.lower()
            if not (any(k in low for k in self.keywords) or HELP_SEEKING.search(text)):
                return "no_signal"
        return None

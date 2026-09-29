"""Offline fakes for tests and local demo mode (REDDIT_CLIENT=fake / LLM_PROVIDER=fake)."""
import time

from app.reddit.base import Conversation, PostedReply, RedditComment, RedditPost, RedditUnavailableError


class FakeRedditClient:
    def __init__(self, posts: dict[str, list[RedditPost]] | None = None, comments: dict[str, list[RedditComment]] | None = None):
        self.posts = posts or {}
        self.comments = comments or {}  # post_id -> comments
        self.fail_subreddits: set[str] = set()
        self.unavailable_posts: set[str] = set()
        self.replies: list[tuple[str, str]] = []
        self.reply_error: Exception | None = None
        self.new_post_calls: list[tuple[str, float | None]] = []

    def get_new_posts(self, subreddit, since_utc, limit):
        self.new_post_calls.append((subreddit, since_utc))
        if subreddit in self.fail_subreddits:
            from app.reddit.base import RedditTransientError

            raise RedditTransientError(f"simulated failure for r/{subreddit}")
        posts = [p for p in self.posts.get(subreddit, []) if since_utc is None or p.created_utc > since_utc]
        return sorted(posts, key=lambda p: -p.created_utc)[:limit]

    def _find(self, post_id):
        for plist in self.posts.values():
            for p in plist:
                if p.id == post_id:
                    return p
        raise RedditUnavailableError("gone")

    def get_conversation(self, post_id, limit):
        if post_id in self.unavailable_posts:
            raise RedditUnavailableError("gone")
        post = self._find(post_id)
        post.num_comments = max(post.num_comments, len(self.comments.get(post_id, [])))
        return Conversation(post=post, comments=list(self.comments.get(post_id, []))[:limit])

    def get_comments(self, post_id, limit):
        return self.get_conversation(post_id, limit).comments

    def reply_to_post(self, post_fullname, text):
        return self._reply(post_fullname, text)

    def reply_to_comment(self, comment_fullname, text):
        return self._reply(comment_fullname, text)

    def _reply(self, parent, text):
        if self.reply_error:
            raise self.reply_error
        self.replies.append((parent, text))
        return PostedReply(fullname=f"t1_fake{len(self.replies)}")


class FakeLLMClient:
    """Canned decisions: 'looking for' posts -> REPLY_TO_POST; 'maybe'/'thinking' -> MONITOR; else NO_ACTION."""

    model = "fake-llm"

    def __init__(self):
        self.calls: list[str] = []
        self.responses: list[dict | Exception] = []  # scripted, consumed first

    def decide(self, system, user):
        self.calls.append(user)
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        low = user.lower()
        if "looking for" in low or "recommend" in low:
            return {"relevant": True, "intent": "USER_LOOKING_FOR_SOLUTION", "relevanceScore": 0.9,
                    "recommendedAction": "REPLY_TO_POST", "confidence": 0.85,
                    "reason": "Author explicitly asks for a tool for this problem.",
                    "suggestedResponse": "We had the same issue; a shared board plus a weekly triage habit helped a lot. Happy to share details."}
        if "maybe" in low or "thinking about" in low:
            return {"relevant": True, "intent": "SEEKING_ADVICE", "relevanceScore": 0.5, "recommendedAction": "MONITOR",
                    "confidence": 0.6, "reason": "Early-stage discussion; too soon to engage.", "suggestedResponse": None}
        return {"relevant": False, "intent": "UNRELATED", "relevanceScore": 0.05, "recommendedAction": "NO_ACTION",
                "confidence": 0.9, "reason": "Unrelated.", "suggestedResponse": None}


def demo_reddit_client(subreddits: list[str]) -> FakeRedditClient:
    now = time.time()
    posts = {
        sub: [
            RedditPost(id=f"demo{i}{sub[:3].lower()}", subreddit=sub, author=f"demo_user{i}", title=t, body=b,
                       permalink=f"https://www.reddit.com/r/{sub}/comments/demo{i}{sub[:3].lower()}/",
                       created_utc=now - 600 * (i + 1))
            for i, (t, b) in enumerate([
                ("Looking for a tool to organise customer feedback?", "We collect feedback in email and Slack and it is a mess. Any recommendations?"),
                ("Maybe thinking about a roadmap tool", "Not sure we need one yet, thoughts on when a small team should start?"),
                ("Weekend show-off thread", "Post your cat photos here for the weekend!! (unrelated to work at all)"),
            ])
        ]
        for sub in subreddits
    }
    return FakeRedditClient(posts=posts)

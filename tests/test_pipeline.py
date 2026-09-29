from sqlalchemy import select

from app.llm.base import LLMError
from app.models import LlmDecision, MonitoredSubreddit, Opportunity, RedditItem
from tests.conftest import make_comment, make_post


def opps(sf):
    with sf() as s:
        return list(s.scalars(select(Opportunity).order_by(Opportunity.id)))


def decision(action, rel=0.9, conf=0.9, resp="Helpful reply", **kw):
    return {"relevant": True, "intent": "USER_LOOKING_FOR_SOLUTION", "relevanceScore": rel, "recommendedAction": action,
            "confidence": conf, "reason": "because", "suggestedResponse": resp, **kw}


def test_end_to_end_reply_to_post(monitor, reddit, sf):
    reddit.posts["SaaS"] = [make_post("p1"), make_post("p2", title="Cat photos", body="unrelated weekend thread about cats and dogs")]
    stats = monitor.run_once(only="SaaS")
    st = stats.subreddits[0]
    assert (st.fetched, st.filtered, st.sent_to_llm, st.opportunities) == (2, 1, 1, 1)
    o = opps(sf)[0]
    assert o.status == "REVIEW_REQUIRED" and o.recommended_action == "REPLY_TO_POST"
    assert o.score == 0.9 and o.response_text and o.original_response == o.response_text
    with sf() as s:
        assert s.scalar(select(LlmDecision)).final_action == "REPLY_TO_POST"
        assert s.scalar(select(RedditItem).where(RedditItem.fullname == "t3_p1")).state == "ANALYZED"


def test_duplicates_not_analyzed_twice(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    monitor.run_once(only="SaaS")
    # simulate lost checkpoint (e.g. crash before commit): same post is fetched again
    with sf() as s:
        sub = s.scalar(select(MonitoredSubreddit).where(MonitoredSubreddit.name == "SaaS"))
        sub.last_seen_created_utc = None
        s.commit()
    st = monitor.run_once(only="SaaS").subreddits[0]
    assert st.duplicates == 1 and st.sent_to_llm == 0
    assert len(llm.calls) == 1 and len(opps(sf)) == 1


def test_checkpoint_used_on_next_run(monitor, reddit):
    reddit.posts["SaaS"] = [make_post("p1")]
    monitor.run_once(only="SaaS")
    monitor.run_once(only="SaaS")
    assert reddit.new_post_calls[0][1] != reddit.new_post_calls[1][1]
    assert reddit.new_post_calls[1][1] == reddit.posts["SaaS"][0].created_utc


def test_low_relevance_becomes_no_action(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    llm.responses = [decision("REPLY_TO_POST", rel=0.2)]
    monitor.run_once(only="SaaS")
    o = opps(sf)[0]
    assert o.recommended_action == "NO_ACTION" and o.status == "IGNORED" and o.response_text is None


def test_not_relevant_flag_forces_no_action(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    d = decision("REPLY_TO_POST"); d["relevant"] = False
    llm.responses = [d]
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].recommended_action == "NO_ACTION"


def test_reply_to_comment_and_private_message_mapping(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1"), make_post("p2", age=500), make_post("p3", age=400)]
    reddit.comments["p1"] = [make_comment("c1", "p1", author="carol")]
    reddit.comments["p2"] = [make_comment("c2", "p2", author="dave")]
    llm.responses = [
        decision("REPLY_TO_COMMENT", targetCommentId="c1"),
        decision("PREPARE_PRIVATE_MESSAGE", targetCommentId="c2"),
        decision("MONITOR", resp=None),
    ]
    monitor.run_once(only="SaaS")
    o1, o2, o3 = opps(sf)
    assert (o1.recommended_action, o1.status, o1.target_comment_fullname, o1.target_author) == ("REPLY_TO_COMMENT", "REVIEW_REQUIRED", "t1_c1", "carol")
    assert (o2.recommended_action, o2.status, o2.target_author) == ("PREPARE_PRIVATE_MESSAGE", "REVIEW_REQUIRED", "dave")
    assert (o3.recommended_action, o3.status, o3.response_text) == ("MONITOR", "MONITORING", None)


def test_reply_to_unknown_comment_is_no_action(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    llm.responses = [decision("REPLY_TO_COMMENT", targetCommentId="nope")]
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].recommended_action == "NO_ACTION"


def test_already_commented_by_us_is_no_action(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    reddit.comments["p1"] = [make_comment("c1", "p1", author="OurBrand")]
    llm.responses = [decision("REPLY_TO_POST")]
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].recommended_action == "NO_ACTION"


def test_invalid_llm_output_does_not_lose_item_and_is_retried(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    llm.responses = [{"garbage": True}]
    st = monitor.run_once(only="SaaS").subreddits[0]
    assert st.llm_failures == 1
    o = opps(sf)[0]
    assert o.status == "NEW" and "LLM" in o.last_error
    with sf() as s:
        assert s.scalar(select(RedditItem).where(RedditItem.fullname == "t3_p1")).state == "PENDING"
    st = monitor.run_once(only="SaaS").subreddits[0]  # retry next cycle, no re-fetch needed
    assert st.fetched == 0 and st.sent_to_llm == 1
    assert opps(sf)[0].status == "REVIEW_REQUIRED"


def test_llm_provider_failure_does_not_lose_item(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    llm.responses = [LLMError("boom")]
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].status == "NEW"
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].status == "REVIEW_REQUIRED"


def test_monitor_reconsidered_only_after_meaningful_activity(monitor, reddit, llm, sf):
    reddit.posts["SaaS"] = [make_post("p1")]
    llm.responses = [decision("MONITOR", rel=0.5, resp=None)]
    monitor.run_once(only="SaaS")
    assert opps(sf)[0].status == "MONITORING" and len(llm.calls) == 1
    monitor.run_once(only="SaaS")  # unchanged -> no LLM call
    assert len(llm.calls) == 1
    reddit.comments["p1"] = [make_comment("c1", "p1"), make_comment("c2", "p1", author="eve")]
    llm.responses = [decision("REPLY_TO_POST")]
    st = monitor.run_once(only="SaaS").subreddits[0]
    assert st.reevaluated == 1 and len(llm.calls) == 2
    o = opps(sf)[0]
    assert o.status == "REVIEW_REQUIRED" and o.analysis_count == 2
    monitor.run_once(only="SaaS")  # no longer monitoring
    assert len(llm.calls) == 2


def test_failed_subreddit_does_not_stop_others(monitor, reddit, sf):
    reddit.fail_subreddits.add("SaaS")
    reddit.posts["startups"] = [make_post("p9", sub="startups")]
    stats = monitor.run_once()
    by = {s.subreddit: s for s in stats.subreddits}
    assert by["SaaS"].error and by["startups"].error is None and by["startups"].opportunities == 1
    with sf() as s:
        assert s.scalar(select(MonitoredSubreddit.last_error).where(MonitoredSubreddit.name == "SaaS"))


def test_deleted_and_unavailable_handled(monitor, reddit, sf):
    reddit.posts["SaaS"] = [make_post("p1", removed=True), make_post("p2", author="[deleted]"), make_post("p3", age=100)]
    reddit.unavailable_posts.add("p3")
    monitor.run_once(only="SaaS")
    o = opps(sf)
    assert len(o) == 1 and o[0].status == "IGNORED"


def test_llm_budget_caps_calls(monitor, reddit, llm, cfg):
    cfg.fetch.max_llm_calls_per_run = 1
    reddit.posts["SaaS"] = [make_post("p1"), make_post("p2", age=500)]
    monitor.run_once(only="SaaS")
    assert len(llm.calls) == 1
    monitor.run_once(only="SaaS")
    assert len(llm.calls) == 2

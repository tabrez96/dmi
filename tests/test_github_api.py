"""The GitHub client's reading of CodeRabbit: when its review is held up, and when it has finished a commit."""

from hq import github as github_api


def test_review_stalled_reads_coderabbits_summary_and_replies():
    client = github_api.GitHub("token")
    bot = "coderabbitai[bot]"

    def said(*bodies):
        client._list = lambda path: [{"user": {"login": bot}, "body": body, "created_at": f"2026-11-0{i + 1}T00:00:00Z",
                                      "updated_at": f"2026-11-0{i + 1}T01:00:00Z"} for i, body in enumerate(bodies)]
    summary = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    said(summary + "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->")
    assert client.review_stalled("me/app", 1, bot) == "2026-11-01T01:00:00Z"
    said(summary + "Walkthrough", "<summary>Action not completed</summary>\nReview rate limited.")
    assert client.review_stalled("me/app", 1, bot) == "2026-11-02T01:00:00Z"
    said(summary + "Walkthrough", "<summary>Action performed</summary>\nReview finished.")
    assert client.review_stalled("me/app", 1, bot) is None
    said(summary + "<!-- auto-generated comment: rate limited by coderabbit.ai -->",
         "<summary>Action not completed</summary>\nReview rate limited.")
    assert client.review_stalled("me/app", 1, bot) == "2026-11-02T01:00:00Z"


def test_a_review_that_got_through_after_a_refusal_is_not_held_up():
    client = github_api.GitHub("token")
    summary = {"user": {"login": "coderabbitai[bot]"}, "created_at": "2026-11-01T00:00:00Z",
               "updated_at": "2026-11-03T00:00:00Z",                        # edited after the refusal: reviewed
               "body": "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\nWalkthrough"}
    refusal = {"user": {"login": "coderabbitai[bot]"}, "created_at": "2026-11-02T00:00:00Z",
               "updated_at": "2026-11-02T00:00:00Z", "body": "<summary>Action not completed</summary>"}
    client._list = lambda path: [summary, refusal]
    assert client.review_stalled("me/app", 1, "coderabbitai[bot]") is None


def test_review_finished_reads_every_status_page():
    client = github_api.GitHub("token")
    others = [{"context": f"ci-{i}", "state": "success"} for i in range(100)]
    pages = {1: others, 2: [{"context": "CodeRabbit", "state": "success", "description": "Review completed"}]}
    client._call = lambda method, path: {"statuses": pages[int(path.rsplit("page=", 1)[1])]}
    assert client.review_finished("me/app", "abc")
    pages[2] = [{"context": "CodeRabbit", "state": "success", "description": "Review rate limited"}]
    assert not client.review_finished("me/app", "abc")


def test_head_runs_reads_every_page():
    client = github_api.GitHub("token")
    lint = [{"id": i, "workflow_id": 1, "name": "Lint", "status": "completed", "conclusion": "success"} for i in range(100)]
    pages = {1: lint, 2: [{"id": 200, "workflow_id": 2, "name": "Tests", "status": "completed", "conclusion": "failure"}]}
    client._call = lambda method, path: {"total_count": 101, "workflow_runs": pages[int(path.rsplit("page=", 1)[1])]}
    assert sorted((run["name"], run["conclusion"]) for run in client.head_runs("me/app", "abc")) == \
        [("Lint", "success"), ("Tests", "failure")]

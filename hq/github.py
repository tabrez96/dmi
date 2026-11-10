"""GitHub for product repos: push branches, open and merge pull requests.

Only HQ uses this. The token (GITHUB_TOKEN in .env) never goes into an agent's environment, a
remote URL, or a repo's git config: pushes pass it through git's environment for that one command.
"""
import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
CONNECT_TRIES = 4       # a refused or dropped connection is tried again, a second longer each time
CONNECT_PAUSE = 1


class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Follows a redirect, but the token only goes along when it stays on the same host (a renamed repo does);
    urllib's own handler would hand it to whatever host the redirect names."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_SameHostAuth)


def remote_url(slug):
    return f"https://github.com/{slug}.git"


HOOK_EVENTS = ["workflow_run", "issue_comment", "pull_request_review", "pull_request_review_comment"]


class GitHub:
    def __init__(self, token, api=API):
        self.token, self.api = token, api

    @classmethod
    def from_env(cls):
        token = os.environ.get("GITHUB_TOKEN")
        return cls(token) if token else None

    def git_env(self):
        """Environment that lets one git command authenticate to github.com over HTTPS."""
        auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}", "GIT_TERMINAL_PROMPT": "0"}

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dunder-mifflin-hq",
                     "Content-Type": "application/json"})
        for attempt in range(CONNECT_TRIES):
            try:
                with _OPENER.open(req, timeout=30) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as e:
                try:
                    error = json.loads(e.read())
                    msg = error.get("message", "") if isinstance(error, dict) else ""
                except (ValueError, OSError):           # an error page that isn't JSON
                    msg = ""
                raise GitHubError(f"GitHub {method} {path}: {e.code} {msg}".strip(), e.code) from None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:   # incl. dropped connections
                # Some connections to GitHub are refused outright (about one in ten from the mini PC's network).
                # Nothing was sent, so trying again is safe for any method; a drop after sending is only retried
                # for a read.
                refused = isinstance(getattr(e, "reason", e), ConnectionRefusedError)
                if attempt + 1 < CONNECT_TRIES and (refused or method == "GET"):
                    time.sleep(CONNECT_PAUSE * (attempt + 1))
                    continue
                raise GitHubError(f"GitHub unreachable: {e!r}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:                      # a 200 that isn't GitHub's JSON (a proxy or captive portal page)
            raise GitHubError(f"GitHub {method} {path}: the reply isn't JSON") from None

    PAGE = 100

    def _list(self, path):
        """Every item of a list endpoint (`path` ends in `per_page=100`), following the pages: GitHub lists oldest
        first, so on a long PR the newest comments are on the later pages."""
        items, page = [], 1
        while True:
            batch = self._call("GET", path if page == 1 else f"{path}&page={page}")
            if not isinstance(batch, list):
                raise GitHubError(f"GitHub GET {path}: expected a list")
            items += batch
            if len(batch) < self.PAGE:
                return items
            page += 1

    def repo(self, slug):
        return self._call("GET", f"/repos/{slug}")

    def set_default_branch(self, slug, branch):
        """PRs target the default branch, and `Closes #n` only closes issues when merged into it."""
        self._call("PATCH", f"/repos/{slug}", {"default_branch": branch})

    def open_pr(self, slug, head, base, title, body):
        """Open a PR (or return the one already open for this branch). -> (number, url)"""
        try:
            pr = self._call("POST", f"/repos/{slug}/pulls",
                            {"title": title, "head": head, "base": base, "body": body[:60000]})
        except GitHubError as e:
            if e.status != 422:
                raise
            owner = slug.split("/")[0]
            found = self._call("GET", f"/repos/{slug}/pulls?head={owner}:{head}&base={base}&state=open")
            pr = next((each for each in found or [] if (each.get("head") or {}).get("ref") == head
                       and (each.get("base") or {}).get("ref") == base), None)
            if not pr:
                raise
        return pr["number"], pr["html_url"]

    def create_issue(self, slug, title, body):
        """-> (number, url)"""
        i = self._call("POST", f"/repos/{slug}/issues", {"title": title, "body": body[:60000]})
        return i["number"], i["html_url"]

    def close_issue(self, slug, number, reason="completed"):
        self._call("PATCH", f"/repos/{slug}/issues/{number}", {"state": "closed", "state_reason": reason})

    def pr_state(self, slug, number):
        """-> 'open' | 'merged' | 'closed'"""
        pr = self._call("GET", f"/repos/{slug}/pulls/{number}")
        return "merged" if pr.get("merged") else pr["state"]

    def merge_pr(self, slug, number, title, method="rebase"):
        """Rebase (default) or squash: the branch is already one commit, so either leaves a linear history with
        no merge commit. 'rebase' keeps the engineer as the commit's author."""
        self._call("PUT", f"/repos/{slug}/pulls/{number}/merge", {"merge_method": method, "commit_title": title})

    def comment_pr(self, slug, number, body):
        self._call("POST", f"/repos/{slug}/issues/{number}/comments", {"body": body[:59000] + "\n\n" + HQ_MARK})

    def reply_to_review_comment(self, slug, number, comment_id, body):
        """Reply inside an inline review comment's thread."""
        self._call("POST", f"/repos/{slug}/pulls/{number}/comments/{comment_id}/replies", {"body": body[:59000] + "\n\n" + HQ_MARK})

    def latest_run(self, slug, branch):
        """The newest workflow run on a branch: {"id", "status", "conclusion", "sha", "url", "name", "message"} or None."""
        runs = self._call("GET", f"/repos/{slug}/actions/runs?branch={branch}&per_page=1").get("workflow_runs") or []
        if not runs:
            return None
        run = runs[0]
        return {"id": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "sha": run["head_sha"],
                "url": run["html_url"], "name": run.get("name") or "CI",
                "message": ((run.get("head_commit") or {}).get("message") or "").split("\n")[0]}

    def failure_excerpt(self, slug, run_id):
        """What broke in a failed run: the error lines of each failed job's log. The log download link must not
        get our token, so the redirect is followed by hand."""
        jobs, page = [], 1
        while True:          # every page: on a big matrix the failed job can be past the first hundred
            batch = self._call("GET", f"/repos/{slug}/actions/runs/{run_id}/jobs?per_page={self.PAGE}&page={page}")
            batch = (batch.get("jobs") or []) if isinstance(batch, dict) else []
            jobs += batch
            if len(batch) < self.PAGE:
                break
            page += 1
        parts = []
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            step = next((each["name"] for each in job.get("steps") or [] if each.get("conclusion") == "failure"), None)
            heading = f"### {job['name']}" + (f" (step: {step})" if step else "")
            try:
                text = log_excerpt(self._download_log(f"/repos/{slug}/actions/jobs/{job['id']}/logs"))
            except GitHubError as e:
                text = f"(the log could not be downloaded: {e})"
            parts.append(f"{heading}\n{text}")
        return "\n\n".join(parts) or "(the run failed but no job reports a failure; open the run to see why)"

    def ensure_webhook(self, slug, url, secret):
        """Make sure the repo sends `workflow_run` events to `url`. Returns "created" or "updated"."""
        cfg = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
        for hook in self._call("GET", f"/repos/{slug}/hooks?per_page=100"):
            if (hook.get("config") or {}).get("url") == url:
                self._call("PATCH", f"/repos/{slug}/hooks/{hook['id']}", {"config": cfg, "events": HOOK_EVENTS, "active": True})
                return "updated"
        self._call("POST", f"/repos/{slug}/hooks", {"name": "web", "active": True, "events": HOOK_EVENTS, "config": cfg})
        return "created"

    def _download_log(self, path):
        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        req = urllib.request.Request(self.api + path, headers={"Authorization": f"Bearer {self.token}",
                                     "Accept": "application/vnd.github+json", "User-Agent": "dunder-mifflin-hq"})
        try:
            urllib.request.build_opener(Stay).open(req, timeout=30)
            raise GitHubError("no log link")
        except urllib.error.HTTPError as e:
            link = e.headers.get("Location")
            if e.code not in (301, 302, 303, 307, 308) or not link:
                raise GitHubError(f"GitHub GET {path}: {e.code}", e.code) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:   # after HTTPError, its subclass
            raise GitHubError(f"log link request failed: {e}") from None
        try:       # the signed download link: no Authorization header
            with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": "dunder-mifflin-hq"}),
                                        timeout=60) as response:
                return _tail(response, LOG_BYTES).decode("utf-8", "replace")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise GitHubError(f"log download failed: {e}") from None

    def owner_feedback(self, slug, number, logins, after="", bots=()):
        """What `logins` (the owner) wrote on a PR after the ISO time `after`: (newest time, [{"where", "body"}]) or None.
        Inline comments, review bodies and ordinary comments count; approvals, a bare "LGTM" and anything HQ wrote
        itself (HQ_MARK) don't, and neither does a comment that starts by addressing a bot (`@coderabbitai review`):
        that is an instruction to the bot, not feedback for the crew. Nobody else's words ever reach an agent."""
        who = {login.lower() for login in logins}

        def is_owner(user):
            return ((user or {}).get("login") or "").lower() in who
        items = []
        for comment in self._list(f"/repos/{slug}/pulls/{number}/comments?per_page=100"):
            where = f"{comment['path']}:{comment.get('line') or comment.get('original_line') or '?'}"
            items.append((comment["created_at"], where, comment))
        for review in self._list(f"/repos/{slug}/pulls/{number}/reviews?per_page=100"):
            if review.get("state") not in ("APPROVED", "DISMISSED") and review.get("submitted_at"):
                items.append((review["submitted_at"], "review", review))
        for comment in self._list(f"/repos/{slug}/issues/{number}/comments?per_page=100"):
            items.append((comment["created_at"], "comment", comment))
        new = sorted(((when, where, item) for when, where, item in items if when > after and is_owner(item.get("user"))),
                     key=lambda entry: entry[0])
        if not new:
            return None
        found = [{"where": where, "body": (item.get("body") or "").replace(HQ_MARK, "").strip()[:2500],
                  **({"comment_id": item["id"]} if where not in ("review", "comment") and item.get("id") else {})}
                 for _when, where, item in new if HQ_MARK not in (item.get("body") or "")]
        bot_names = "|".join(re.escape(bot.removesuffix("[bot]")) for bot in bots)
        to_bot = re.compile(rf"^\s*@({bot_names})\b", re.IGNORECASE) if bots else None
        found = [entry for entry in found if entry["body"] and not _OK_ONLY.match(entry["body"])
                 and not (to_bot and to_bot.match(entry["body"]))]
        return new[-1][0], found

    def bot_seen(self, slug, number, bot):
        """Has `bot` (CodeRabbit) reviewed or commented on this PR yet?"""
        reviews = self._list(f"/repos/{slug}/pulls/{number}/reviews?per_page=100")
        comments = self._list(f"/repos/{slug}/issues/{number}/comments?per_page=100")
        inline = self._list(f"/repos/{slug}/pulls/{number}/comments?per_page=100")
        return any((item.get("user") or {}).get("login") == bot for item in reviews + comments + inline)

    def review_after(self, slug, number, bot, after=0):
        """The newest review of a PR by `bot` with an id above `after`: (id, body, [inline comments]) or None.
        Comments are the review's own inline comments, each {"comment_id", "where", "body"}, cleaned of the bot's markup."""
        reviews = [review for review in self._list(f"/repos/{slug}/pulls/{number}/reviews?per_page=100")
                   if (review.get("user") or {}).get("login") == bot and review["id"] > after]
        if not reviews:
            return None
        review = max(reviews, key=lambda each: each["id"])
        inline = self._list(f"/repos/{slug}/pulls/{number}/comments?per_page=100")
        comments = [{"comment_id": comment["id"], "where": _where(comment),
                     "body": clean_review_text(comment["body"], drop_prompt=True)}
                    for comment in inline
                    if comment.get("pull_request_review_id") == review["id"] and not comment.get("in_reply_to_id")]
        return review["id"], clean_review_text(review.get("body") or "", drop_prompt=True), comments

    def kody_comments(self, slug, number, bot, after=""):
        """What `bot` (Kody) wrote on a PR after the ISO time `after`: (newest time, [comment]) or None, each comment
        {"where", "body"} plus "comment_id" when it is an inline one. Kody's inline comments are its findings. Its
        summary comment only counts when it carries findings and no inline comment does (an "all clear" summary, or
        one that restates the inline comments, adds nothing). The newest time covers everything Kody wrote, so an
        all-clear is read once too."""
        def mine(comment):
            return ((comment.get("user") or {}).get("login") or "") == bot and comment["created_at"] > after
        inline = [comment for comment in self._list(f"/repos/{slug}/pulls/{number}/comments?per_page=100")
                  if mine(comment)]
        summaries = [comment for comment in self._list(f"/repos/{slug}/issues/{number}/comments?per_page=100")
                     if mine(comment)]
        if not inline and not summaries:
            return None
        newest = max(comment["created_at"] for comment in inline + summaries)
        found = [{"comment_id": comment["id"], "where": _where(comment), "body": clean_review_text(comment["body"])}
                 for comment in inline if not comment.get("in_reply_to_id") and HQ_MARK not in (comment.get("body") or "")]
        if not found:
            findings = [clean_kody_summary(comment.get("body") or "") for comment in summaries]
            found = [{"where": "Kody's summary", "body": text} for text in findings if text]
        return newest, found


_ERR = re.compile(r"error[:\s]|\bFAILED\b|\bfailed\b|\bfatal\b|Traceback|panic:|✗|✘|Assertion|XCTAssert", re.IGNORECASE)


LOG_BYTES = 2_000_000      # how much of a job's log is read: its end, where the failure is


def _tail(response, limit):
    """The last `limit` bytes of a response, read in chunks (a failure is at the end of a long log)."""
    kept = b""
    while chunk := response.read(65536):
        kept = (kept + chunk)[-limit:]
    return kept


def _where(comment):
    """Where an inline review comment points: "path:line"."""
    return f"{comment['path']}:{comment.get('line') or comment.get('original_line') or '?'}"


def log_excerpt(text, limit=3500):
    """The lines of a CI log that say what broke (compiler errors, failed tests), without timestamps or repeats.
    A log with none of those keeps its last lines instead."""
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?", "", line).rstrip() for line in text.splitlines()]
    hits, seen = [], set()
    for line in lines:
        key = line.strip().lstrip("|`- ")
        if _ERR.search(line) and key not in seen and not line.lstrip().startswith("##[endgroup]"):
            seen.add(key)
            hits.append(line[:300])
    out = "\n".join(hits[:40]) if hits else "\n".join(line[:300] for line in lines[-40:] if line.strip())
    return out[:limit]


HQ_MARK = "<!-- dmi -->"       # on everything HQ writes to a PR, so it never reads its own words as yours
_OK_ONLY = re.compile(r"^\W*(lgtm|looks good( to me)?|ship it|merge( it)?|approved?|thanks?|thank you|👍|✅|:\+1:)\W*$", re.IGNORECASE)


def clean_kody_summary(text):
    """Kody's summary comment without its markup and usage guide: what is left is the findings, or "" when the
    summary only says it found no issues."""
    text = re.sub(r"<details>\s*<summary>Kody Guide:.*?</details>\s*</details>", "", text, flags=re.DOTALL)
    text = clean_review_text(text.replace("&#8203;", "").replace("\u200b", ""))
    if re.search(r"no issues were found", text, re.IGNORECASE) or re.match(r"\s*@kody\b", text, re.IGNORECASE):   # all clear, or a command
        return ""
    return text


def clean_review_text(text, limit=2500, drop_prompt=False):
    """A bot review comment without its HTML comments, icons and (on the review body) the duplicated
    'prompt for AI agents' block, so an agent reads the finding and not the markup."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<picture>.*?</picture>", "", text, flags=re.DOTALL)
    if drop_prompt:
        text = re.sub(r"<details>\s*<summary>[^<]*Prompt (?:to fix review comments|for AI Agents).*?</details>", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"

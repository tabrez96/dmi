"""Product repos in git (real repos in a temp dir) and reading GitHub's replies (the API stubbed)."""

import io
import subprocess
import urllib.request

import pytest

from hq import github as github_client
from hq import gitops
from hq.github import GitHub, GitHubError, _tail, clean_kody_summary, log_excerpt

ENGINEER = "Darryl (Engineer) <engineer@dundermifflin.local>"


def subjects(repo, ref=gitops.DEV):
    return gitops.git(repo, "log", "--format=%s", ref).stdout.split("\n")[:-1]


@pytest.fixture
def repo(tmp_path):
    gitops.init_product(tmp_path / "product", "Pingr")
    return tmp_path / "product"


@pytest.fixture
def github(tmp_path, repo):
    """A bare repo standing in for GitHub, holding what the product has so far."""
    bare = tmp_path / "github.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", gitops.DEV, str(bare)], check=True)
    gitops.git(repo, "push", "-q", str(bare), f"{gitops.DEV}:refs/heads/{gitops.DEV}")
    return bare


def test_a_new_repo_starts_with_the_ignore_file_then_the_readme(repo):
    assert subjects(repo) == ["docs: README", "chore: .gitignore"]
    assert "design/**/*.html" in (repo / ".gitignore").read_text()


def test_a_ticket_lands_as_one_commit_by_whoever_did_the_work(repo, tmp_path):
    _, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 7)
    for step in ("one", "two"):
        (worktree / f"{step}.txt").write_text(step)
        gitops.commit_all(worktree, f"wip {step}", ENGINEER)
    gitops.squash_branch(worktree, "Add export", ENGINEER)
    assert gitops.commits_ahead(worktree) == 1
    gitops.merge(repo, 7, "Add export", ENGINEER)
    assert subjects(repo)[0] == "Add export"
    assert gitops.git(repo, "log", "-1", "--format=%an", gitops.DEV).stdout.strip() == "Darryl (Engineer)"


def test_develop_only_follows_github(repo, github):
    clone = github.parent / "someone-else"
    subprocess.run(["git", "clone", "-q", "-b", gitops.DEV, str(github), str(clone)], check=True)
    (clone / "merged.txt").write_text("a merged PR")
    gitops.commit_all(clone, "T-8: merged on GitHub", ENGINEER)
    gitops.git(clone, "push", "-q", "origin", gitops.DEV)
    gitops.sync_main(repo, str(github), {})
    assert subjects(repo)[0] == "T-8: merged on GitHub"                   # a fast-forward
    (repo / "local.txt").write_text("x")
    gitops.commit_main(repo, "a commit made here")                        # never allowed to reach develop unseen
    with pytest.raises(gitops.GitError, match="need a pull request"):
        gitops.sync_main(repo, str(github), {})


def test_develop_adopts_githubs_copy_of_the_same_commits(repo, github):
    (repo / "notes.md").write_text("notes\n")
    gitops.commit_main(repo, "docs: notes")                                # what a pull request carried
    tree = gitops.git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent = gitops.git(repo, "rev-parse", "HEAD~1").stdout.strip()
    gitops.git(repo, "push", "-q", str(github), "HEAD:refs/heads/dmi/setup-notes")    # the pull request's branch
    github_identity = {"GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@github.com",
                       "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}
    rewritten = gitops.git(github, "commit-tree", tree, "-p", parent, "-m", "docs: notes (rebase-merged)",
                           env=github_identity).stdout.strip()
    gitops.git(github, "update-ref", f"refs/heads/{gitops.DEV}", rewritten)          # same files, a new hash
    gitops.sync_main(repo, str(github), {})
    assert gitops.git(repo, "rev-parse", "HEAD").stdout.strip() == rewritten



def test_develop_adopts_github_when_it_has_every_local_commit_and_more(repo, github):
    github_identity = {"GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@github.com",
                       "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}
    (repo / "notes.md").write_text("notes\n")
    gitops.commit_main(repo, "docs: notes")                                # the first pull request
    parent = gitops.git(repo, "rev-parse", "HEAD~1").stdout.strip()
    (repo / "CLAUDE.md").write_text("# For the crew\n")                  # a later one, not merged locally
    gitops.git(repo, "add", "CLAUDE.md")
    later_commit = gitops.git(repo, "commit-tree", gitops.git(repo, "write-tree").stdout.strip(), "-p", "HEAD",
                              "-m", "docs: CLAUDE.md", env=github_identity).stdout.strip()
    gitops.git(repo, "reset", "-q", "--hard", "HEAD")
    gitops.git(repo, "push", "-q", str(github), "HEAD:refs/heads/dmi/setup-notes",
               f"{later_commit}:refs/heads/dmi/setup-hq")
    tree_of = {ref: gitops.git(github, "rev-parse", f"{ref}^{{tree}}").stdout.strip()
               for ref in ("dmi/setup-notes", "dmi/setup-hq")}
    rewritten = gitops.git(github, "commit-tree", tree_of["dmi/setup-notes"], "-p", parent, "-m", "docs: notes",
                           env=github_identity).stdout.strip()                   # rebase merges: new hashes
    later = gitops.git(github, "commit-tree", tree_of["dmi/setup-hq"], "-p", rewritten, "-m", "docs: CLAUDE.md",
                       env=github_identity).stdout.strip()
    gitops.git(github, "update-ref", f"refs/heads/{gitops.DEV}", later)
    gitops.sync_main(repo, str(github), {})
    assert gitops.git(repo, "rev-parse", "HEAD").stdout.strip() == later and (repo / "CLAUDE.md").exists()
    (repo / "local.md").write_text("only here\n")
    gitops.commit_main(repo, "docs: only here")
    with pytest.raises(gitops.GitError):                                 # a commit GitHub never got still stops it
        gitops.sync_main(repo, str(github), {})
    gitops.git(repo, "reset", "-q", "--hard", "HEAD~1")                  # back to what GitHub has, then
    (repo / "merged.md").write_text("only in a local merge\n")          # a merge commit of two commits GitHub
    gitops.git(repo, "add", "merged.md")                                 # already has, with files it doesn't:
    tree = gitops.git(repo, "write-tree").stdout.strip()                 # git cherry lists nothing for it
    merge = gitops.git(repo, "commit-tree", tree, "-p", "HEAD", "-p", "HEAD~1", "-m", "Merge",
                       env={"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}).stdout.strip()
    gitops.git(repo, "reset", "-q", "--hard", merge)
    with pytest.raises(gitops.GitError):
        gitops.sync_main(repo, str(github), {})

def test_hq_changes_are_taken_out_of_the_checkout_and_applied_on_a_branch(repo, tmp_path):
    (repo / "README.md").write_text("# Pingr\n\nNew words.\n")
    (repo / "CLAUDE.md").write_text("# For the crew\n")
    patch = gitops.take_changes(repo)
    assert patch and not gitops.git(repo, "status", "--porcelain").stdout       # the checkout is back to develop
    assert not (repo / "CLAUDE.md").exists()
    branch = tmp_path / "hq"
    gitops.git(repo, "worktree", "add", "-q", "--detach", str(branch), gitops.DEV)
    assert gitops.commit_patch(branch, patch, "docs: CLAUDE.md for the crew")
    assert (branch / "CLAUDE.md").exists() and "New words" in (branch / "README.md").read_text()
    assert gitops.take_changes(repo) == b""                                     # nothing left to take


def test_a_forced_push_is_leased_to_what_github_has(repo, github, tmp_path):
    _, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 9)
    (worktree / "a.txt").write_text("a")
    gitops.commit_all(worktree, "T-9: first", ENGINEER)
    gitops.push(repo, str(github), "dmi/t-9", {}, force=True)
    gitops.git(worktree, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "--amend", "-qm", "T-9: amended")
    gitops.push(repo, str(github), "dmi/t-9", {}, force=True)
    assert subjects(github, "dmi/t-9")[0] == "T-9: amended"


def test_each_launch_is_tagged_in_turn(repo):
    assert gitops.release(repo) == "v1" and gitops.release(repo) == "v2"
    assert gitops.git(repo, "rev-parse", gitops.RELEASE).stdout == gitops.git(repo, "rev-parse", gitops.DEV).stdout


def test_a_ci_log_is_cut_to_the_lines_that_say_what_broke():
    log = ("2026-11-10T10:00:00.1Z Compiling...\n"
           "2026-11-10T10:00:01.2Z error: no such module 'X'\n"
           "2026-11-10T10:00:01.3Z error: no such module 'X'\n"
           "done")
    assert log_excerpt(log) == "error: no such module 'X'"
    assert log_excerpt("just\nnoise") == "just\nnoise"                  # nothing matched: the last lines


def test_kody_saying_all_clear_is_not_a_finding():
    assert clean_kody_summary("# Kody Review Complete\nNo issues were found that match your configuration.") == ""
    assert "unchecked index" in clean_kody_summary("Found: an unchecked index in parse()")


def test_only_the_owners_own_words_count_as_feedback(monkeypatch):
    replies = {
        "/repos/me/pingr/pulls/3/comments?per_page=100": [
            {"id": 1, "created_at": "2026-11-10T10:00:00Z", "path": "a.py", "line": 4, "body": "Rename this",
             "user": {"login": "tabrez96"}},
            {"id": 2, "created_at": "2026-11-10T10:01:00Z", "path": "a.py", "line": 5, "body": "nit",
             "user": {"login": "coderabbitai[bot]"}}],
        "/repos/me/pingr/pulls/3/reviews?per_page=100": [
            {"id": 3, "submitted_at": "2026-11-10T10:02:00Z", "state": "APPROVED", "body": "",
             "user": {"login": "tabrez96"}}],
        "/repos/me/pingr/issues/3/comments?per_page=100": [
            {"id": 4, "created_at": "2026-11-10T10:03:00Z", "body": "@coderabbitai review", "user": {"login": "tabrez96"}},
            {"id": 5, "created_at": "2026-11-10T10:04:00Z", "body": "LGTM", "user": {"login": "tabrez96"}}]}
    client = GitHub("token")
    monkeypatch.setattr(client, "_call", lambda method, path, body=None: replies[path])
    newest, found = client.owner_feedback("me/pingr", 3, ["tabrez96"], bots=["coderabbitai[bot]"])
    assert newest == "2026-11-10T10:04:00Z"
    assert found == [{"where": "a.py:4", "body": "Rename this", "comment_id": 1}]


def test_a_patch_keeps_crlf_and_odd_file_names(repo, tmp_path):
    (repo / "windows.txt").write_bytes(b"one\r\ntwo\r\n")
    (repo / "café.md").write_text("accents\n")
    patch = gitops.take_changes(repo)
    assert not (repo / "café.md").exists() and not (repo / "windows.txt").exists()
    branch = tmp_path / "hq"
    gitops.git(repo, "worktree", "add", "-q", "--detach", str(branch), gitops.DEV)
    gitops.commit_patch(branch, patch, "docs: notes")
    assert (branch / "windows.txt").read_bytes() == b"one\r\ntwo\r\n" and (branch / "café.md").exists()


def test_a_merge_refuses_a_dirty_develop(repo, tmp_path):
    _, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 4)
    (worktree / "x.txt").write_text("x")
    gitops.commit_all(worktree, "Add x", ENGINEER)
    (repo / "README.md").write_text("an edit nobody committed")
    with pytest.raises(gitops.GitError, match="uncommitted"):
        gitops.merge(repo, 4, "Add x", ENGINEER)
    assert (repo / "README.md").read_text() == "an edit nobody committed"        # not thrown away


def test_a_release_tag_skips_past_gaps(repo):
    gitops.git(repo, "tag", "v3")
    assert gitops.release(repo) == "v4"


def test_a_failed_release_push_leaves_nothing_behind(repo, tmp_path):
    with pytest.raises(gitops.GitError):
        gitops.release(repo, url=str(tmp_path / "nowhere.git"), env={})
    assert gitops.git(repo, "tag", "--list").stdout.strip() == ""


def test_git_location_variables_dont_leak_into_other_repos(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "somewhere-else"))
    assert subjects(repo)[0] == "docs: README"


def test_long_pr_lists_are_read_to_the_last_page(monkeypatch):
    first = [{"id": n} for n in range(100)]
    pages = {"/x?per_page=100": first, "/x?per_page=100&page=2": [{"id": 100}]}
    client = GitHub("token")
    monkeypatch.setattr(client, "_call", lambda method, path, body=None: pages[path])
    assert len(client._list("/x?per_page=100")) == 101


def test_a_reply_that_isnt_json_is_a_github_error(monkeypatch):
    class Page(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    monkeypatch.setattr(github_client._OPENER, "open", lambda req, timeout: Page(b"<html>captive portal</html>"))
    with pytest.raises(GitHubError, match="isn't JSON"):
        GitHub("token")._call("GET", "/repos/me/pingr")


def test_a_release_never_moves_master_backwards(repo):
    gitops.release(repo)
    gitops.git(repo, "switch", "-q", gitops.RELEASE)
    (repo / "hotfix.txt").write_text("fix")
    gitops.commit_all(repo, "Hotfix", ENGINEER)                       # on master only
    gitops.git(repo, "switch", "-q", gitops.DEV)
    with pytest.raises(gitops.GitError, match="would drop"):
        gitops.release(repo)
    assert subjects(repo, gitops.RELEASE)[0] == "Hotfix"


def test_a_pr_branch_is_pushed_while_a_worktree_has_it_checked_out(repo, github, tmp_path):
    branch, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 5)
    (worktree / "a.txt").write_text("a")
    gitops.commit_all(worktree, "Add a", ENGINEER)
    commit = gitops.git(worktree, "rev-parse", "HEAD").stdout.strip()
    gitops.push_branch(repo, str(github), branch, commit, {})
    assert subjects(github, branch)[0] == "Add a"


def test_a_folder_that_was_already_there_keeps_its_files(tmp_path):
    folder = tmp_path / "product"
    folder.mkdir()
    (folder / ".gitignore").write_text("build/\n")
    (folder / "README.md").write_text("# Mine\n")
    gitops.init_product(folder, "Pingr")
    assert (folder / ".gitignore").read_text().startswith("build/\n") and ".dmi/" in (folder / ".gitignore").read_text()
    assert (folder / "README.md").read_text() == "# Mine\n"


def test_a_patch_is_not_applied_over_uncommitted_edits(repo, tmp_path):
    (repo / "notes.md").write_text("notes\n")
    patch = gitops.take_changes(repo)
    branch = tmp_path / "hq"
    gitops.git(repo, "worktree", "add", "-q", "--detach", str(branch), gitops.DEV)
    (branch / "README.md").write_text("an edit in progress")
    with pytest.raises(gitops.GitError, match="uncommitted"):
        gitops.commit_patch(branch, patch, "docs: notes")
    assert (branch / "README.md").read_text() == "an edit in progress"


def test_an_existing_pr_is_only_reused_for_the_same_base(monkeypatch):
    def reply(method, path, body=None):
        if method == "POST":
            raise GitHubError("exists", 422)
        return [{"number": 4, "html_url": "u4", "head": {"ref": "dmi/hq"}, "base": {"ref": "master"}},
                {"number": 5, "html_url": "u5", "head": {"ref": "dmi/hq"}, "base": {"ref": "develop"}}]
    client = GitHub("token")
    monkeypatch.setattr(client, "_call", reply)
    assert client.open_pr("me/pingr", "dmi/hq", "develop", "title", "body") == (5, "u5")


def test_the_token_stays_behind_on_a_redirect_to_another_host():
    request = urllib.request.Request("https://api.github.com/repos/me/pingr", headers={"Authorization": "Bearer t"})
    def follow(url):
        return github_client._SameHostAuth().redirect_request(request, None, 301, "Moved", {}, url)
    assert follow("https://api.github.com/repositories/42").get_header("Authorization") == "Bearer t"
    assert follow("https://elsewhere.example/x").get_header("Authorization") is None


def test_a_patch_commits_only_its_own_files(repo, tmp_path):
    (repo / "notes.md").write_text("notes\n")
    patch = gitops.take_changes(repo)
    branch = tmp_path / "hq"
    gitops.git(repo, "worktree", "add", "-q", "--detach", str(branch), gitops.DEV)
    (branch / ".env").write_text("SECRET=1")                               # a stray file in the worktree
    assert gitops.commit_patch(branch, patch, "docs: notes")
    assert gitops.git(branch, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["notes.md"]


def test_a_kept_ignore_rule_doesnt_stop_the_readme(tmp_path):
    folder = tmp_path / "product"
    folder.mkdir()
    (folder / ".gitignore").write_text("*.md\n")
    assert gitops.init_product(folder, "Pingr")
    assert subjects(folder)[0] == "docs: README"


def test_a_log_keeps_its_end():
    assert _tail(io.BytesIO(b"start " * 50_000 + b"error: at the end"), 100).endswith(b"error: at the end")


def test_a_waiting_tickets_empty_branch_catches_up_with_develop(repo, tmp_path):
    _branch, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 70)   # cut early, while its dependency was open
    (repo / "upload.py").write_text("print('upload')\n")
    gitops.git(repo, "add", "upload.py")
    gitops.commit_staged(repo, "Statement upload", "Darryl (Engineer) <engineer@dundermifflin.local>")
    gitops.ensure_worktree(repo, tmp_path / "work", 70)
    assert (worktree / "upload.py").exists()                            # the dependency's code is there now
    (worktree / "headers.py").write_text("x = 1\n")
    gitops.commit_all(worktree, "Security headers")
    (repo / "later.py").write_text("y = 2\n")
    gitops.git(repo, "add", "later.py")
    gitops.commit_staged(repo, "Something later", "Darryl (Engineer) <engineer@dundermifflin.local>")
    gitops.ensure_worktree(repo, tmp_path / "work", 70)
    assert not (worktree / "later.py").exists()                         # a branch with its own work is left alone


def test_an_empty_ticket_branch_catches_up_after_a_rebase_merge_rewrote_develop(repo, tmp_path):
    _branch, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 71)
    rewritten = gitops.git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD~1", "-m", "same files, new hash",
                           env={"GIT_AUTHOR_NAME": "GitHub", "GIT_AUTHOR_EMAIL": "noreply@github.com",
                                "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}).stdout.strip()
    gitops.git(repo, "reset", "-q", "--hard", rewritten)                # what sync_main does after a rebase merge
    (repo / "upload.py").write_text("print('upload')\n")
    gitops.git(repo, "add", "upload.py")
    gitops.commit_staged(repo, "Statement upload", "Darryl (Engineer) <engineer@dundermifflin.local>")
    gitops.ensure_worktree(repo, tmp_path / "work", 71)
    assert (worktree / "upload.py").exists()                            # its old base was develop's, not its own work


def test_a_ticket_commit_that_differs_only_in_whitespace_is_kept(repo, tmp_path):
    _branch, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 72)
    (worktree / "rule.py").write_text("def ok(x):\n    if x:\n        pass\n    return True\n")   # the ticket's work
    gitops.commit_all(worktree, "Return True always")
    (repo / "rule.py").write_text("def ok(x):\n    if x:\n        pass\n        return True\n")  # develop: inside the if
    gitops.git(repo, "add", "rule.py")
    gitops.commit_staged(repo, "Return True when x", "Darryl (Engineer) <engineer@dundermifflin.local>")
    gitops.ensure_worktree(repo, tmp_path / "work", 72)
    assert (worktree / "rule.py").read_text().endswith("\n    return True\n")             # not reset away


def test_a_ticket_that_reverts_a_develop_commit_keeps_its_revert(repo, tmp_path):
    (repo / "feature.py").write_text("on = True\n")
    gitops.git(repo, "add", "feature.py")
    gitops.commit_staged(repo, "Turn the feature on", "Darryl (Engineer) <engineer@dundermifflin.local>")
    _branch, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 73)
    gitops.git(worktree, "-c", "user.name=Darryl", "-c", "user.email=engineer@dundermifflin.local", "revert", "--no-edit",
               "HEAD")                                                  # its files now equal develop's older commit
    gitops.ensure_worktree(repo, tmp_path / "work", 73)
    assert not (worktree / "feature.py").exists()                       # the revert is the ticket's work: kept


def test_a_push_that_couldnt_connect_is_tried_again(tmp_path, monkeypatch):
    import subprocess as sp
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return sp.CompletedProcess(args, 128, "", "fatal: unable to access 'https://github.com/x/y.git/': "
                                       "Failed to connect to github.com port 443: Couldn't connect to server")
        return sp.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(gitops.subprocess, "run", flaky)
    monkeypatch.setattr(gitops, "CONNECT_PAUSE", 0)
    gitops.git(tmp_path, "push", "https://github.com/x/y.git", "a:b")
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(gitops.GitError):            # a real refusal fails at once
        monkeypatch.setattr(gitops.subprocess, "run", lambda args, **kwargs: calls.append(args) or
                            sp.CompletedProcess(args, 1, "", "! [rejected] (non-fast-forward)"))
        gitops.git(tmp_path, "push", "https://github.com/x/y.git", "a:b")
    assert len(calls) == 1


def test_git_gives_up_after_four_tries_waiting_longer_each_time(tmp_path, monkeypatch):
    import subprocess as sp
    calls, pauses = [], []
    monkeypatch.setattr(gitops.subprocess, "run", lambda args, **kwargs: calls.append(args) or
                        sp.CompletedProcess(args, 128, "", "fatal: Could not resolve host: github.com"))
    monkeypatch.setattr(gitops.time, "sleep", pauses.append)
    with pytest.raises(gitops.GitError):
        gitops.git(tmp_path, "fetch", "https://github.com/x/y.git")
    assert len(calls) == 4 and pauses == [5, 10, 15]
    calls.clear()
    assert gitops.git(tmp_path, "fetch", "https://github.com/x/y.git", check=False).returncode == 128
    assert len(calls) == 4


def test_a_push_whose_connection_broke_midway_is_not_repeated(tmp_path, monkeypatch):
    import subprocess as sp
    calls = []
    monkeypatch.setattr(gitops.subprocess, "run", lambda args, **kwargs: calls.append(args) or
                        sp.CompletedProcess(args, 128, "", "fatal: Connection reset by peer"))
    monkeypatch.setattr(gitops.time, "sleep", lambda seconds: None)
    with pytest.raises(gitops.GitError):
        gitops.git(tmp_path, "push", "https://github.com/x/y.git", "a:b")      # it may already have landed
    assert len(calls) == 1
    calls.clear()
    gitops.git(tmp_path, "fetch", "https://github.com/x/y.git", check=False)    # reading again is harmless
    assert len(calls) == 4


def test_a_patch_over_an_older_version_of_the_same_lines_wins(tmp_path):
    repo = tmp_path / "app"
    gitops.init_product(repo, "App")
    middle = "".join(f"line {number}\n" for number in range(6))
    names = ("[s] design.md", "s design.md")                  # a name that is also a pattern matching the other
    for name in names:
        (repo / name).write_text(f"{name}\nPython 3.12\n{middle}timeout 30\n")
    gitops.commit_all(repo, "design")
    worktree = tmp_path / "wt"
    gitops.git(repo, "worktree", "add", "-q", "-b", "hq", str(worktree))
    for name in names:
        (worktree / name).write_text(f"{name}\nPython 3.12 and Postgres 16\n{middle}timeout 60\n")     # the open PR
    gitops.commit_all(worktree, "first")
    for name in names:
        (repo / name).write_text(f"{name}\nPython 3.14 and Postgres 18\n{middle}timeout 30\n")         # the revision
    patch = gitops.take_changes(repo)
    assert gitops.commit_patch(worktree, patch, "revised")
    # the clashing line takes the revision; the branch's own edit lower down stays; each file keeps its own text
    for name in names:
        assert (worktree / name).read_text() == f"{name}\nPython 3.14 and Postgres 18\n{middle}timeout 60\n"
    assert not gitops.git(worktree, "status", "--porcelain").stdout


def test_a_file_both_sides_added_takes_the_patchs_version(tmp_path):
    repo = tmp_path / "app"
    gitops.init_product(repo, "App")
    worktree = tmp_path / "wt"
    gitops.git(repo, "worktree", "add", "-q", "-b", "hq", str(worktree))
    (worktree / "system.md").write_text("Python 3.12\n")                       # the open PR adds it
    gitops.commit_all(worktree, "first")
    (repo / "system.md").write_text("Python 3.14\n")                           # so does the revision
    assert gitops.commit_patch(worktree, gitops.take_changes(repo), "revised")
    assert (worktree / "system.md").read_text() == "Python 3.14\n"
    assert not gitops.git(worktree, "status", "--porcelain").stdout


def _branch_off(tmp_path, start_text):
    repo = tmp_path / "app"
    gitops.init_product(repo, "App")
    (repo / "jobs.py").write_text(start_text)
    gitops.commit_all(repo, "start")
    _, worktree = gitops.ensure_worktree(repo, tmp_path / "work", 1)
    return repo, worktree


def test_hq_replays_a_ticket_on_a_develop_that_moved(tmp_path):
    repo, worktree = _branch_off(tmp_path, "a\nb\nc\n")
    (worktree / "jobs.py").write_text("a\nb\nc\nlock\n")
    gitops.commit_all(worktree, "Hold the lock")
    (repo / "other.py").write_text("x\n")
    gitops.commit_all(repo, "elsewhere")                                    # develop moved on
    assert gitops.replay_on_develop(worktree) == []
    assert gitops.commits_ahead(worktree) == 1 and (worktree / "other.py").exists()
    assert gitops.git(worktree, "log", "-1", "--format=%s").stdout.strip() == "Hold the lock"


def test_hq_leaves_clashing_lines_marked_for_the_engineer(tmp_path):
    repo, worktree = _branch_off(tmp_path, "a\n")
    (worktree / "jobs.py").write_text("mine\n")
    gitops.commit_all(worktree, "mine")
    (repo / "jobs.py").write_text("theirs\n")
    gitops.commit_all(repo, "theirs")
    mine = gitops.git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert gitops.replay_on_develop(worktree) == ["jobs.py"]
    assert "<<<<<<<" in (worktree / "jobs.py").read_text()
    branch = gitops.git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert gitops.git(worktree, "rev-parse", f"refs/dmi/before-rebase/{branch}").stdout.strip() == mine   # kept


def test_a_replay_that_git_fails_on_leaves_the_branch_as_it_was(tmp_path, monkeypatch):
    repo, worktree = _branch_off(tmp_path, "a\n")
    (worktree / "jobs.py").write_text("mine\n")
    gitops.commit_all(worktree, "mine")
    (repo / "other.py").write_text("x\n")
    gitops.commit_all(repo, "elsewhere")
    mine = gitops.git(worktree, "rev-parse", "HEAD").stdout.strip()

    def broken(*args, **kwargs):
        raise gitops.GitError("disk full")
    monkeypatch.setattr(gitops, "commit_staged", broken)
    with pytest.raises(gitops.GitError):
        gitops.replay_on_develop(worktree)
    assert gitops.git(worktree, "rev-parse", "HEAD").stdout.strip() == mine
    assert (worktree / "jobs.py").read_text() == "mine\n"

def test_only_whats_new_since_a_merged_version_is_replayed(tmp_path):
    repo, worktree = _branch_off(tmp_path, "a\n")
    (worktree / "jobs.py").write_text("feature\n")
    gitops.commit_all(worktree, "feature")
    merged = gitops.git(worktree, "rev-parse", "HEAD").stdout.strip()
    (repo / "jobs.py").write_text("feature\n")
    gitops.commit_all(repo, "feature, squash-merged")                     # develop has the first version
    (worktree / "jobs.py").write_text("feature\nfix\n")
    gitops.commit_all(worktree, "fix")
    assert gitops.replay_on_develop(worktree, since=merged) == []
    assert gitops.git(worktree, "diff", "--stat", gitops.DEV, "HEAD").stdout.count("|") == 1

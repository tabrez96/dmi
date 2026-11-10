"""Git plumbing: the product repo, one worktree + branch per ticket, merging on ship."""

import os
import subprocess
import threading
from pathlib import Path

DEV = "develop"      # where the work happens: every ticket lands here
RELEASE = "master"   # only a launch moves this; it is what actually ships

# Worktree add/remove and merges touch shared repo metadata; serialize them.
_lock = threading.Lock()


class GitError(Exception):
    pass


# Set by git for hooks (and by some tools); left in place they would point every command at one repo, whatever `cwd` is.
_GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")


def child_env(env=None):
    """The environment for a git (or uv) process run in some repo's `cwd`: ours, minus git's location variables."""
    return {**{name: value for name, value in os.environ.items() if name not in _GIT_LOCATION}, **(env or {})}


GIT_TIMEOUT = 600     # seconds; a stalled fetch or push must not hold `_lock` forever


def git(cwd, *args, check=True, env=None, binary=False, stdin=None, timeout=GIT_TIMEOUT):
    """Run git in `cwd`. `binary=True` keeps output (and `stdin`) as bytes: patches must not be decoded."""
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=not binary, check=False,
                              input=stdin, env=child_env(env), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)}: no answer after {timeout}s") from None
    if check and done.returncode != 0:
        err = done.stderr.decode(errors="replace") if binary else done.stderr
        out = done.stdout.decode(errors="replace") if binary else done.stdout
        raise GitError(f"git {' '.join(args)}: {err.strip() or out.strip()}")
    return done


# .dmi/ is agent scratch. The designer's HTML is her working source: it stays on disk (so she can edit a screen in
# place) but the repo, and GitHub, only carry the screenshots, the specs and the written design.
IGNORE = ".dmi/\ndesign/**/*.html\n"


def ignore_design_html(repo):
    """Make an existing product repo ignore (and stop tracking) the designer's HTML. Returns True if it changed."""
    repo = Path(repo)
    ignore = repo / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    changed = False
    if "design/**/*.html" not in text:
        ignore.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "design/**/*.html\n")
        changed = True
    tracked = git(repo, "ls-files", "-z", "--", ":(glob)design/**/*.html", check=False).stdout.split("\0")
    tracked = [path for path in tracked if path]
    if tracked:
        git(repo, "rm", "-r", "--cached", "-q", "--", *tracked)
        changed = True
    return changed


def squash_history(repo, message, author):
    """Replace everything after the initial commit with ONE commit holding the current tree (history rewrite;
    the caller must make sure no branch depends on the old commits). Returns the old commit count."""
    repo = Path(repo)
    with _lock:
        git(repo, "add", "-A")
        old_count = int(git(repo, "rev-list", "--count", "HEAD").stdout.strip())
        root = git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
        tree = git(repo, "write-tree").stdout.strip()
        name, email = author.rsplit(" <", 1)
        env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email.rstrip(">"),
               "GIT_COMMITTER_NAME": "Dunder Mifflin HQ", "GIT_COMMITTER_EMAIL": "hq@dundermifflin.local"}
        new = git(repo, "commit-tree", tree, "-p", root, "-m", message, env=env).stdout.strip()
        git(repo, "reset", "--soft", new)
        git(repo, "reflog", "expire", "--expire=now", "--all")
        git(repo, "gc", "--prune=now", "--quiet", check=False)
    return old_count


def init_product(repo, title="Dunder Mifflin product", readme=None):
    """Create a product repo. The first commit is `.gitignore` and nothing else: that is all that reaches GitHub
    directly. The README is the second commit and goes to GitHub as a pull request the owner reviews."""
    repo = Path(repo)
    if (repo / ".git").exists():
        return False
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", DEV)
    hq = ("-c", "user.name=Dunder Mifflin HQ", "-c", "user.email=hq@dundermifflin.local")
    ignore, readme_file = repo / ".gitignore", repo / "README.md"
    kept = ignore.read_text() if ignore.exists() else ""      # a folder that was already there keeps its own files
    ignore.write_text(kept + ("" if kept.endswith("\n") or not kept else "\n") + IGNORE)
    git(repo, "add", ".gitignore")
    git(repo, *hq, "commit", "-m", "chore: .gitignore")
    if not readme_file.exists():
        readme_file.write_text(readme or f"# {title}\n\nBuilt by Dunder Mifflin agents.\n")
    git(repo, "add", "--force", "README.md")          # even if a kept .gitignore says `*.md`
    git(repo, *hq, "commit", "-m", "docs: README")
    return True


def publish_root(repo, url, env):
    """First push to a new GitHub repo: only the first commit, as develop and master. Everything after it arrives
    by pull request. -> the commits of develop, oldest first."""
    with _lock:
        commits = git(repo, "rev-list", "--reverse", DEV).stdout.split()
        git(repo, "push", url, f"{commits[0]}:refs/heads/{DEV}", f"{commits[0]}:refs/heads/{RELEASE}", env=env)
    return commits


def push_branch(repo, url, branch, commit, env, force=True):
    """Push `commit` as `branch` on GitHub (a branch for a pull request). No local branch is moved, so it works even
    while a worktree has that branch checked out. `force=False` when `commit` builds on the branch's current tip
    there: then GitHub refuses the push instead of losing commits someone else added."""
    with _lock:
        git(repo, "push", *(["--force"] if force else []), url, f"{commit}:refs/heads/{branch}", env=env)


def branch_name(tid):
    return f"dmi/t-{tid}"


def ensure_worktree(repo, workdir, tid):
    branch = branch_name(tid)
    path = Path(workdir).resolve() / f"T-{tid}"
    with _lock:
        if not path.exists():
            exists = git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
            if exists:
                git(repo, "worktree", "add", str(path), branch)
            else:
                git(repo, "worktree", "add", "-b", branch, str(path), DEV)
        elif not (path / ".git").exists():     # e.g. left by an interrupted setup: not a checkout git knows about
            raise GitError(f"{path} exists but isn't a git worktree; remove it and the ticket gets a fresh one")
    (path / ".dmi").mkdir(exist_ok=True)
    return branch, path


def commit_all(path, message, author="Dunder Mifflin Engineer <engineer@dundermifflin.local>"):
    """Commit whatever the agent left in the working tree. Returns True if it committed."""
    git(path, "add", "-A")
    return commit_staged(path, message, author)


def commit_staged(path, message, author="Dunder Mifflin Engineer <engineer@dundermifflin.local>"):
    """Commit what is in the index and nothing else. Returns True if it committed."""
    if git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    name, email = author.rsplit(" <", 1)
    git(path, "-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>')}",
        "commit", "-m", message)
    return True


HQ_AUTHOR = "Dunder Mifflin HQ <hq@dundermifflin.local>"


def commit_main(repo, message, author=HQ_AUTHOR):
    """Commit whatever changed in a repo's main working tree. Serialised with merges, so an HQ commit
    never lands in the middle of a ticket's merge."""
    with _lock:
        return commit_all(repo, message, author)


HQ_BRANCH = "dmi/hq"   # HQ's own changes to a product on GitHub (setup, design, renames): a pull request like any other


def take_changes(repo):
    """The uncommitted changes in a repo's main checkout, as a binary patch (bytes, never decoded: line endings and
    encodings survive), and the checkout put back to HEAD (so local develop stays exactly what GitHub has). b"" if
    nothing changed. Ignored files, like the designer's HTML, stay."""
    with _lock:
        git(repo, "add", "-A")
        patch = git(repo, "diff", "--cached", "--binary", "HEAD", binary=True).stdout
        if not patch:
            return b""
        added = git(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=A", "HEAD").stdout.split("\0")
        git(repo, "reset", "--quiet")
        git(repo, "checkout", "--", ".")
        for name in added:
            if name:
                (Path(repo) / name).unlink(missing_ok=True)
        return patch


def commit_patch(wt, patch, message, author=HQ_AUTHOR):
    """Apply a patch from take_changes in a worktree (three-way, if the branch already moved those lines) and commit
    it. Returns True if it committed; raises GitError if it doesn't apply."""
    if git(wt, "status", "--porcelain", "--untracked-files=no").stdout.strip():   # the reset below would lose them
        raise GitError(f"{wt} has uncommitted changes, so the patch wasn't applied")
    applied = git(wt, "apply", "--3way", "--whitespace=nowarn", check=False, binary=True, stdin=patch)
    if applied.returncode != 0:
        git(wt, "reset", "--hard", "--quiet", check=False)
        raise GitError(f"git apply: {applied.stderr.decode(errors='replace').strip()[-400:]}")
    return commit_staged(wt, message, author)    # --3way staged exactly the patch; stray files (a .env) stay out


def discard_changes(path):
    """Drop uncommitted edits (QA must not change code). Ignored files like .dmi/ survive."""
    git(path, "reset", "--hard", "--quiet")
    git(path, "clean", "-fd", "--quiet")


def commits_ahead(path, base=DEV):
    return int(git(path, "rev-list", "--count", f"{base}..HEAD").stdout.strip())


def diff_stat(path, base=DEV):
    return git(path, "diff", "--stat", f"{base}...HEAD").stdout.strip()


def squash_branch(wt, message, author, base=DEV):
    """Collapse the ticket branch (every engineer and fix-up commit) into ONE commit on top of develop, so each
    ticket lands as a single commit. No-op if the branch has no commits of its own."""
    if commits_ahead(wt, base) == 0:
        return False
    mb = git(wt, "merge-base", base, "HEAD").stdout.strip()
    git(wt, "reset", "--soft", mb)
    name, email = author.rsplit(" <", 1)
    git(wt, "-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>')}", "commit", "-m", message)
    return True


def refresh_lock(wt, timeout=300):
    """Regenerate `uv.lock` in a ticket's worktree and fold it into the ticket's commit (amend, so the ticket stays ONE
    commit). Runs on the host, where the project's Python exists; `--no-build` keeps it from running any package's
    build scripts. -> True if the lockfile changed, False if it didn't. Raises GitError if uv fails or can't run."""
    try:
        done = subprocess.run(["uv", "lock", "--no-build"], cwd=wt, capture_output=True, text=True, timeout=timeout,
                              check=False, env=child_env())
    except (subprocess.TimeoutExpired, OSError) as e:          # too slow, or uv isn't installed
        raise GitError(f"uv lock: {e}") from e
    if done.returncode != 0:
        raise GitError(f"uv lock: {(done.stderr or done.stdout).strip()[-400:]}")
    git(wt, "add", "uv.lock")
    if git(wt, "diff", "--cached", "--quiet", "--", "uv.lock", check=False).returncode == 0:
        return False
    hq = ("-c", "user.name=Dunder Mifflin HQ", "-c", "user.email=hq@dundermifflin.local")
    if commits_ahead(wt) == 0:          # no ticket commit yet: amending would rewrite develop's own commit
        git(wt, *hq, "commit", "-m", "Update uv.lock", "--", "uv.lock")
    else:
        git(wt, *hq, "commit", "--amend", "--no-edit", "--", "uv.lock")   # only the lockfile joins the ticket's commit
    return True


def merge(repo, tid, title, author=HQ_AUTHOR):
    """Land the ticket branch on develop as one commit (squash, never a merge commit), authored by the engineer.
    Raises GitError (and resets) on conflict."""
    branch = branch_name(tid)
    name, email = author.rsplit(" <", 1)
    with _lock:
        dirty = git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if dirty:                    # the reset below would throw those edits away
            raise GitError(f"develop has uncommitted changes, so T-{tid} wasn't merged:\n{dirty}")
        done = git(repo, "merge", "--squash", branch, check=False)
        if done.returncode == 0:
            done = git(repo, "-c", f"user.name={name}", "-c", f"user.email={email.rstrip('>')}", "commit",
                       "--allow-empty", "-m", title, check=False)
        if done.returncode != 0:
            git(repo, "reset", "--hard", "HEAD", check=False)
            raise GitError(done.stdout.strip() or done.stderr.strip())


def push(repo, url, ref, env, force=False):
    """Push `ref` to `url`. `force` is for a ticket branch HQ itself rebased (with a lease)."""
    lease = []
    if force:   # a push to a bare URL has no remote-tracking ref, so name the remote's current commit explicitly
        out = git(repo, "ls-remote", url, f"refs/heads/{ref}", env=env).stdout.split()
        if out:
            lease = [f"--force-with-lease=refs/heads/{ref}:{out[0]}"]
    git(repo, "push", *lease, url, f"{ref}:refs/heads/{ref}", env=env)


def migrate_branch(repo):
    """Older repos used `main` for the working branch; it's `develop` now. Rename it in place. Returns True if renamed."""
    def has(branch):
        return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if has("main") and not has(DEV):
        git(repo, "branch", "-m", "main", DEV)
        return True
    return False


def release(repo, url=None, env=None):
    """Point master at develop (always a fast-forward: master only ever moves here) and tag the release v<n>.
    Pushes master and the tag when `url` is given. Returns the tag."""
    with _lock:
        released = [int(tag[1:]) for tag in git(repo, "tag", "--list", "v[0-9]*").stdout.split() if tag[1:].isdigit()]
        tag = f"v{max(released, default=0) + 1}"         # the highest plus one, so a gap (v1, v3) can't collide
        before = git(repo, "rev-parse", "--verify", "--quiet", RELEASE, check=False).stdout.strip()
        if before and git(repo, "merge-base", "--is-ancestor", before, DEV, check=False).returncode != 0:
            raise GitError(f"{RELEASE} has commits that {DEV} doesn't, so releasing would drop them")
        git(repo, "branch", "-f", RELEASE, DEV)
        git(repo, "tag", tag, DEV)
        if url:
            try:     # atomic: GitHub takes master and the tag together, or neither
                git(repo, "push", "--atomic", url, f"{RELEASE}:refs/heads/{RELEASE}", f"refs/tags/{tag}:refs/tags/{tag}", env=env)
            except GitError:          # not released after all: leave master and the tags as they were
                git(repo, "tag", "-d", tag, check=False)
                if before:
                    git(repo, "branch", "-f", RELEASE, before, check=False)
                raise
    return tag


def publish_branches(repo, url, env):
    """First push to a GitHub repo: develop (the work) and master (the release branch, same commit to start)."""
    with _lock:
        git(repo, "branch", "-f", RELEASE, DEV) if git(repo, "rev-parse", "--verify", "--quiet",
                                                       f"refs/heads/{RELEASE}", check=False).returncode else None
        git(repo, "push", url, f"{DEV}:refs/heads/{DEV}", f"{RELEASE}:refs/heads/{RELEASE}", env=env)


def sync_main(repo, url, env):
    """Bring local develop up to GitHub's develop (a fast-forward). Everything reaches GitHub's develop through a pull
    request, so local develop never has commits of its own; if it does, say so instead of pushing or replaying them."""
    with _lock:
        git(repo, "fetch", url, DEV, env=env)
        if git(repo, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD", check=False).returncode == 0:
            git(repo, "merge", "--ff-only", "FETCH_HEAD")
            return
        local = git(repo, "log", "--format=%h %s", "FETCH_HEAD..HEAD").stdout.strip()
        raise GitError(f"local develop has commits that aren't on GitHub (they need a pull request):\n{local}")


def remove_worktree(repo, path, tid):
    with _lock:
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        git(repo, "branch", "-D", branch_name(tid), check=False)

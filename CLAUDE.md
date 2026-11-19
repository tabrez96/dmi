# Dunder Mifflin (dmi)

An autonomous company of AI agents that finds, designs and builds software products. A human runs it (in
conversation and in these docs they are "the PM": a role name for the operator, not for the products' users).
This repo is the framework: HQ, the Discord crew, the agent charters and the sandbox. The products it builds live
in `products/<name>/` as **separate git repos** (git-ignored here); never commit them to this repo.

## Where things are
- `hq/`: the Python framework. `orchestrator.py` is the pipeline, `policy.py` the state machines, `board.py`
  the SQLite board, `discord_bot.py` the crew's bots, `gitops.py` / `github.py` the product repos and GitHub,
  `library.py` the design library and screenshots, `claudemd.py` / `readme.py` what every product repo gets.
- `agents/<role>/charter.md`, `company/handbook.md`: instructions the agents get. `config/dmi.yaml`: all settings.
- `sandbox/`, `deploy/`: Kata microVM setup and the systemd service. `tests/`: `uv run pytest`.
- `README.md` explains the pipeline, Discord setup, running it and the GitHub flow.

## Rules for working on this repo
- Ask before every commit, push, PR or history rewrite, showing what goes in; a yes covers that step only.
- Changes reach `master` through a PR on its own plainly named branch (`readme-and-coderabbit`) that the PM reviews
  and merges; don't merge PRs yourself. Push to `master` directly only when the PM explicitly asks.
- Each PR changes under 500 lines (code and tests; lockfiles and generated files don't count), here as in the
  products: anything bigger can't be reviewed. Split a big change into a stack of PRs that each stand on their own,
  and a big file into modules rather than one PR that adds it whole.
- One commit per feature. Review fixes are folded into that feature's commit (then force-push the PR branch with a
  lease), never added as a separate "fix" commit.
- Commit messages and PR titles are plain sentences ("Add the README"), no `feat:` / `docs:` / `chore:` prefixes.
- Commits are authored as `Tabrez Basha <abtabres@gmail.com>` (set in the local git config). No
  `Co-Authored-By: Claude ...`, `Claude-Session:` or other AI-attribution lines in commit messages or PR descriptions.
- Secrets live only in `.env` (never in chat, config or commits). Agents never see them; the gate holds the keys.
- Readable names: no one-letter or cryptic variable names (`m`, `k`, `v`, `t`, `p`, `x`, `cfg2`). Name things for what
  they hold: `member`, `ticket`, `product`, `character`, `message`. Loop variables too (`for key, member in team.items()`).
  Short names are fine only where the meaning is plain, such as `i` as an index or `e` in `except ... as e`.
- Tests must pass (`uv run pytest`); the service is `dmi` (`sudo systemctl restart dmi` only when
  `sudo nerdctl ps -q` shows no agent running).
- Products are referred to by name (`dmi show nudge`); tickets are `T-<n>`; unapproved ideas are `I-<n>`.
- Product repos: work lands on `develop` (one commit per ticket, no merge commits, one PR per ticket); `master` is
  only moved by a launch. Commits are authored as the crew member who did the work (`Pam (Designer)`). Commit messages
  and PR titles say only what the change does (`design: S01 'Now' section and Join buttons on S01-S03`): no ticket
  number and no product name, since the repo is that product's own. Existing commits are left as they are. Each pull
  request aims for under 500 lines (code and tests; lockfiles and generated files don't count) so it's easy to review,
  but in the products that's a target, not a hard limit: the architect sizes tickets towards it, the planner splits a
  ticket only when it would be far bigger (over about twice that), the engineer finishes a ticket that comes out a
  little over and says so, and nobody fails or blocks work for its size alone.
- **Language in anything a product contains** (briefs, specs, screen copy, README, product CLAUDE.md, code,
  commits, issues): no "PM". Products are for a broad audience. Say "you", "users", "the owner" or "the team".
  The charters and `company/handbook.md` tell the agents the same; keep them in step when you change either.

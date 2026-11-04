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
- Never commit or push to `master` directly. Every change goes on its own branch (`feat/...`, `fix/...`,
  `chore/...`) and reaches `master` through a PR that the PM reviews and merges; don't merge PRs yourself.
- Commits are authored as `Tabrez Basha <abtabers@gmail.com>` (set in the local git config). No
  `Co-Authored-By: Claude ...`, `Claude-Session:` or other AI-attribution lines in commit messages or PR descriptions.
- Secrets live only in `.env` (never in chat, config or commits). Agents never see them; the gate holds the keys.
- Tests must pass (`uv run pytest`); the service is `dmi` (`sudo systemctl restart dmi` only when
  `sudo nerdctl ps -q` shows no agent running).
- Products are referred to by name (`dmi show nudge`); tickets are `T-<n>`; unapproved ideas are `I-<n>`.
- Product repos: work lands on `develop` (one commit per ticket, no merge commits, one PR per ticket); `master` is
  only moved by a launch. Commits are authored as the crew member who did the work (`Pam (Designer)`).
- **Language in anything a product contains** (briefs, specs, screen copy, README, product CLAUDE.md, code,
  commits, issues): no "PM". Products are for a broad audience. Say "you", "users", "the owner" or "the team".
  The charters and `company/handbook.md` tell the agents the same; keep them in step when you change either.

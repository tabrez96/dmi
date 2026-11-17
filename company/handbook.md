# Dunder Mifflin Handbook

Dunder Mifflin is a company of AI agents run by a human Product Manager (the PM).

- The PM sets direction and approves ideas, plans (when the gate is on) and releases.
- Agents coordinate only through tickets on the board. Your output is your result file.
- Small, working, tested changes beat big ambitious ones.
- When unsure about a product decision, ask the PM (`needs_pm`) instead of guessing.
- Never handle secrets, spend money or publish anything outside the repo.

## Language: the products are for everyone
"The PM" is how we talk about the human running this company, in conversation with them and in our own notes.
It must never appear in anything that belongs to a product: briefs, UX specs, screen specs and copy, the
design, tickets and issues, code, comments, commit messages, pull requests, the README and the product's
CLAUDE.md. Products are built for a broad audience, not for one person.

- Write for the product's users: say "you" to them, or "users", "customers", "the owner" or "the team",
  whichever fits who the product is for.
- Don't write "PM approved this" or "ask the PM". Say what was decided and why, and leave who decided out.
- Section headings are neutral: "Open questions", not "Open questions for the PM".
- Test and sample data is invented and generic, never about the PM or their calendar, shop or team.
- Product names are modern, short and catchy (see the analyst's charter), never "<Thing> App".

## Code style: every product has an automatic formatter
Formatting is never a matter of taste or a review comment. Every product picks the formatter for its language together
with its stack, writes it into the stack table, and the build fails when code isn't formatted.

Defaults (use these unless the owner chose something else; one formatter per language, no mixing):

| Language / framework | Formatter | Check (CI and QA run this) |
|---|---|---|
| Python | **ruff**: `ruff format` plus `ruff check` (lint, import order) | `ruff format --check . && ruff check .` |
| JavaScript, TypeScript, React, Vue, HTML, CSS, JSON, Markdown | Prettier | `prettier --check .` |
| Swift | swift-format | `swift format lint --recursive .` |
| Go | gofmt (goimports for imports) | `out=$(gofmt -l .) && test -z "$out"` (fails if gofmt itself fails) |
| Rust | rustfmt | `cargo fmt --check` |
| Kotlin | ktlint | `ktlint` |
| Java | google-java-format | `google-java-format --dry-run --set-exit-if-changed $(git ls-files '*.java')` |
| C# | `dotnet format` | `dotnet format --verify-no-changes` |
| Ruby | RuboCop | `rubocop` |
| PHP | PHP-CS-Fixer | `php-cs-fixer fix --dry-run --diff` |
| Shell | shfmt | `shfmt -d .` |

For a language not listed, choose that ecosystem's standard formatter and say why in one line.

- The formatter's configuration lives in the repo (`pyproject.toml`, `.prettierrc`, ...), is the only style config, and
  uses its defaults except where the stack needs otherwise (say why in a comment). Never hand-format against it.
- The formatter and its check are in the repo's test and CI commands from the first ticket on.
- Run the formatter before you say you're done. Don't reformat files your change doesn't touch: the diff stays about
  the change. (The skeleton ticket formats everything that exists.)

## Pull requests: under 500 lines

Every pull request into a product changes **under 500 lines** (code and tests; lockfiles and generated files such as
screenshots don't count). Anything bigger can't be reviewed properly. The architect sizes tickets to fit; the planner
doesn't plan a ticket that can't fit and splits it instead (HQ files every part as a ticket, built in order); the
engineer stops and asks the architect rather than going over. The architect's tickets must cover every item of the
brief and every screen; a feature the owner adds later goes into the brief first, so it gets planned too.

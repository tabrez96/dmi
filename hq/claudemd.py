"""CLAUDE.md for every product repo: what the product is, where things are, how the crew works in it.

Created with the repo and refreshed in two places only: the `<!-- stack -->` block is filled from the architect's
design when it's approved. Everything else is left alone once it exists, so edits by the maintainer stick.
"""
import re

from .pchannels import summary_from_brief
from .refs import split_title

STACK_OPEN, STACK_CLOSE = "<!-- stack -->", "<!-- /stack -->"
PENDING = "_Not decided yet. The architect picks it in the technical design; this fills in when you approve it._"


def stack_section(design):
    """The text of the design's `## Stack` section, or None."""
    found = re.search(r"^##\s+Stack[^\n]*\n(.*?)(?=^##\s|\Z)", design or "", re.MULTILINE | re.DOTALL)
    return found.group(1).strip() if found and found.group(1).strip() else None


CANVAS_OPEN, CANVAS_CLOSE = "<!-- canvas -->", "<!-- /canvas -->"


def fill_canvas(text, url):
    """Put the design canvas link into an existing CLAUDE.md. Returns the new text, or None if no change."""
    if not url or CANVAS_OPEN not in text or CANVAS_CLOSE not in text:
        return None
    head, rest = text.split(CANVAS_OPEN, 1)
    _, tail = rest.split(CANVAS_CLOSE, 1)
    new = f"{head}{CANVAS_OPEN}{url}{CANVAS_CLOSE}{tail}"
    return new if new != text else None


def render(title, brief="", design="", canvas=""):
    name, tagline = split_title(title)
    about = summary_from_brief(brief, 600) if brief else ""
    return f"""# {name}

{tagline or "(tagline to come)"}
{f"{chr(10)}{about}{chr(10)}" if about else ""}
## Where things are
- `design/` is the design library: `brief.md`, `ux.md`, and `screens/S01-name/` (`screen.png` + `spec.md`, plus
  `states/*.png` for empty, error and other states). Screens have permanent IDs (S01, S02, ...). Tickets name the
  screens they build by ID. **Read the spec and look at the screenshot of every screen you build**; `ux.md` has the
  colors, type and spacing. Don't edit `design/`; the designer owns it.
- Design canvas (private and behind a login, so you can't open it; it's where the design gets reviewed): {CANVAS_OPEN}{canvas or "not published yet"}{CANVAS_CLOSE}
- Source code lives outside `design/`. Put it where the stack below says.

## Stack
{STACK_OPEN}
{stack_section(design) or PENDING}
{STACK_CLOSE}

## How we work here
- Two long-lived branches: `develop` is where the work lands (every ticket's PR goes there); `master` is the
  release branch and only moves when the maintainer launches a release. Never commit to `master`.
- One ticket at a time, on its own branch (`dmi/t-<n>`), cut from `develop`. Small, focused changes.
- Each ticket becomes a pull request into `develop` that the maintainer reviews. A pull request changes **under 500
  lines** (code and tests; lockfiles and generated files don't count); anything bigger can't be reviewed, so split it
  into more tickets.
- Commit messages and pull request titles say only what the change does (`Show the running meeting under Now`): no
  ticket number and no product name.
- Prefer boring, proven tools. Don't add a dependency unless the ticket needs it.
- Keep platform-specific code thin and put logic in pure code with unit tests (aim for about 90% coverage). Code
  that can't run in the sandbox is verified by review plus those tests, and then by hand by the maintainer.
- Format with the formatter in the stack table before you say you're done, and keep its check in CI and next to the
  test commands. Never format files your change doesn't touch.
- Write tests for what you build, and run them before you say you're done. Document the build and test commands in the README under "Development".
- Never commit secrets, tokens or personal data. Don't put real credentials in examples.
- If the ticket is unclear or the design is missing something, ask the architect or designer instead of guessing.
"""


def fill_stack(text, design):
    """Put the design's Stack section into an existing CLAUDE.md. Returns the new text, or None if no change."""
    stack = stack_section(design)
    if not stack or STACK_OPEN not in text or STACK_CLOSE not in text:
        return None
    head, rest = text.split(STACK_OPEN, 1)
    _, tail = rest.split(STACK_CLOSE, 1)
    new = f"{head}{STACK_OPEN}\n{stack}\n{STACK_CLOSE}{tail}"
    return new if new != text else None


CODERABBIT = """# yaml-language-server: $schema=https://coderabbit.ai/integrations/schema.v2.json
language: en-US
reviews:
  profile: chill
  high_level_summary: true
  poem: false
  auto_review:
    enabled: true
    drafts: false
    base_branches:
      - develop
  path_filters:
    - "!design/**"
"""

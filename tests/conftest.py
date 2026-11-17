"""What the HQ tests share: an HQ on the scripted agents in a scratch folder, and helpers to run it until it settles."""

import shutil
from pathlib import Path

import pytest

from hq import gitops
from hq.orchestrator import HQ


def scratch_product(h):
    """Most tests file tickets without naming a product. HQ refuses that now (every ticket needs a repo), so for
    the tests those tickets build in a throwaway repo, products/scratch, that isn't a product on the board."""
    scratch = h.products_dir / "scratch"
    gitops.init_product(scratch)
    real = h.repo_for
    h.repo_for = lambda t: real(t) if t.get("product_id") else scratch


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hq(tmp_path, monkeypatch):
    for d in ("config", "agents", "company", "tools"):
        shutil.copytree(ROOT / d, tmp_path / d)
    monkeypatch.setenv("DMI_HARNESS", "fake")
    h = HQ(tmp_path)
    h.cfg["discovery"]["enabled"] = False
    h.cfg["ask_stack"] = False          # most tests want the design straight away; one covers the question
    h.cfg["validate_ideas"] = False     # tests start designs directly; one covers the analyst check
    h.cfg["require_repo"] = False
    h.cfg["roles"]["designer"]["logo_review_rounds"] = 0     # an extra designer turn; its own tests turn it on
    h.cfg["dashboard"] = {**h.cfg.get("dashboard", {}), "enabled": False}  # no web server in tests
    scratch_product(h)
    return h


def drain(hq, max_ticks=40):
    for _ in range(max_ticks):
        if hq.tick() == 0:
            return
    raise AssertionError("pipeline did not settle")


def roles(hq):
    return [r["role"] for r in hq.board.runs_since(0)]


def thread(hq, kind, rid):
    return [(m["author"], m["verdict"]) for m in hq.board.messages(kind, rid) if m["kind"] == "say"]

"""The sandbox's Gradle home: kept per product, and nothing left in it runs in a later run."""

from hq.sandbox import Sandbox


def test_each_product_gets_its_own_gradle_home_cleared_of_init_scripts(tmp_path):
    sandbox = Sandbox.__new__(Sandbox)
    sandbox.gradle_cache = tmp_path / "gradle"
    home = sandbox.gradle_home(tmp_path / "products" / "learnmate" / ".git")       # a ticket's worktree
    assert home == sandbox.gradle_home(tmp_path / "products" / "learnmate")         # the product's own checkout
    assert home != sandbox.gradle_home(tmp_path / "products" / "ferret" / ".git")
    (home / "init.d").mkdir()
    (home / "init.d" / "steal.gradle").write_text("println 'hi'")
    (home / "init.gradle").write_text("println 'hi'")
    (home / "caches").mkdir()
    sandbox.gradle_home(tmp_path / "products" / "learnmate")
    assert sorted(path.name for path in home.iterdir()) == ["caches"]               # downloads stay, scripts go

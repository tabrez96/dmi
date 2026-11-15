"""Every product's logo: required, plain SVG only, copied in with the design, rendered to fill its frame."""

from pathlib import Path

from hq import brand, library


def test_logo_is_required_and_must_be_plain_svg(tmp_path):
    tree = tmp_path / "design"
    (tree / "screens").mkdir(parents=True)
    (tree / "ux.md").write_text("# UX\n")
    errors = library.validate_tree(tree)
    assert any("brand/logo.svg is missing" in error for error in errors)
    assert any("brand/mark.svg is missing" in error for error in errors)
    (tree / "brand").mkdir()
    (tree / "brand" / "logo.svg").write_text("<svg><script>alert(1)</script></svg>")
    (tree / "brand" / "mark.svg").write_text("<svg viewBox='0 0 8 8'><rect width='8' height='8'/></svg>")
    errors = library.validate_tree(tree)
    assert errors == ["brand/logo.svg must be plain SVG shapes and text: no scripts, handlers or images"]
    (tree / "brand" / "logo.svg").write_text("<svg viewBox='0 0 8 8'><rect width='8' height='8'/></svg>")
    assert library.validate_tree(tree) == []


def test_ingest_copies_the_logo(tmp_path):
    src, repo = tmp_path / "src", tmp_path / "repo"
    (src / "brand").mkdir(parents=True)
    (src / "brand" / "logo.svg").write_text("<svg/>")
    (repo / "design").mkdir(parents=True)
    assert library.ingest(src, repo)["brand_changed"] is True
    assert (repo / "design" / "brand" / "logo.svg").exists()
    assert library.ingest(src, repo)["brand_changed"] is False


def test_the_logo_render_fills_its_frame_whatever_size_the_svg_declares(tmp_path, monkeypatch):
    logo_folder = tmp_path / "design" / "brand"
    logo_folder.mkdir(parents=True)
    (logo_folder / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 8 8'/>")
    pages = []
    monkeypatch.setattr(library, "render_png", lambda page, png, size: pages.append(Path(page).read_text()) or True)
    assert brand.render_brand(tmp_path)
    assert "svg{width:100%!important;height:100%!important}" in pages[0]            # a 24 px SVG is not a 24 px logo


def test_a_design_ticket_checks_only_the_logo_and_ux_not_screens_without_html(tmp_path):
    (tmp_path / "ux.md").write_text("# UX\n")
    (tmp_path / "brand").mkdir()
    for name in library.BRAND_FILES:
        (tmp_path / "brand" / name).write_text("<svg viewBox='0 0 8 8'><rect width='8' height='8'/></svg>")
    shot = tmp_path / "screens" / "S01-menu"
    shot.mkdir(parents=True)
    (shot / "spec.md").write_text("---\nid: S01\n---\n# Menu\n")      # as in a git checkout: no screen.html
    assert any("screen.html" in error for error in library.validate_tree(tmp_path))
    assert library.validate_tree(tmp_path, screens=False) == []


def test_a_changed_logo_drops_its_old_png(tmp_path):
    src, repo = tmp_path / "src", tmp_path / "repo"
    (src / "brand").mkdir(parents=True)
    (repo / "design" / "brand").mkdir(parents=True)
    (src / "brand" / "logo.svg").write_text("<svg>new</svg>")
    (repo / "design" / "brand" / "logo.svg").write_text("<svg>old</svg>")
    (repo / "design" / "brand" / "logo.png").write_bytes(b"old picture")   # newer than the new SVG's copied time
    library.ingest(src, repo)
    assert not (repo / "design" / "brand" / "logo.png").exists()          # so it is drawn again from the new SVG

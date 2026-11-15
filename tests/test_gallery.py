"""The design library's generated pages: the README index and the read-only gallery, built from list_screens()."""

from hq import gallery


def screen(tmp_path, screen_id="S01", name="Board", retired=False, states=()):
    folder = tmp_path / f"{screen_id}-x"
    (folder / "states").mkdir(parents=True, exist_ok=True)
    return {"id": screen_id, "dir": folder.name, "path": folder, "file": "screen.html", "name": name, "retired": retired,
            "purpose": "See the orders", "states": list(states), "status": "built", "tickets": [4], "updated": "2026-11-14"}


def test_the_readme_lists_live_screens_and_the_retired_apart(tmp_path):
    text = gallery.render_readme("Ferret", [screen(tmp_path), screen(tmp_path, "S02", "Old", retired=True)])
    assert "| S01 | [Board](screens/S01-x/screen.html) · [spec](screens/S01-x/spec.md) | built |" in text
    assert "T-4" in text and "## Retired screens\n\n- S02 Old (`screens/_retired/S02-x`)" in text
    assert "No screens yet." in gallery.render_readme("Ferret", [])


def test_a_screens_slides_are_its_design_then_the_states_that_have_a_picture(tmp_path):
    board = screen(tmp_path, states=["empty.html", "no-picture.html"])
    (board["path"] / "states" / "empty.png").write_bytes(b"png")
    assert gallery.screen_slides(board) == [("Main design", "screen.png", "screen.html"),
                                            ("Empty", "states/empty.png", "states/empty.html")]


def test_gallery_pages_escape_what_the_crew_wrote(tmp_path):
    product = {"slug": "ferret", "title": "Ferret <b>", "state": "building", "repo": tmp_path,
               "screens": [screen(tmp_path, name="<script>x</script>")]}
    page = gallery.gallery_product(product)
    assert "&lt;script&gt;x&lt;/script&gt;" in page and "<script>x</script>" not in page
    assert "Ferret &lt;b&gt;" in gallery.gallery_index([product])
    assert "S01" in gallery.gallery_screen(product, product["screens"][0])


def test_crew_text_cant_break_the_readme_table_or_the_screen_page(tmp_path):
    piped = {**screen(tmp_path), "purpose": "Review A | B"}
    row = next(line for line in gallery.render_readme("Ferret", [piped]).splitlines() if line.startswith("| S01"))
    assert "Review A \\| B" in row and row.endswith("| T-4 | 2026-11-14 |")
    sneaky = screen(tmp_path, states=["x' onerror='alert(1)' data-x='.html"])
    (sneaky["path"] / "states" / "x' onerror='alert(1)' data-x='.png").write_bytes(b"png")
    page = gallery.gallery_screen({"title": "Ferret"}, sneaky)
    assert "states/x' onerror" not in page and "<img src='../screens/S01-x/states/x%27%20onerror" in page
    assert ".card[hidden]{display:none}" in page                                 # the filter really hides cards


def test_an_svg_screen_shows_its_svg(tmp_path):
    drawn = {**screen(tmp_path), "file": "screen.svg"}
    assert gallery.screen_slides(drawn)[0][1] == "screen.svg"
    assert "/screen.svg'" in gallery.gallery_product({"slug": "f", "title": "F", "state": "building", "repo": tmp_path,
                                                      "screens": [drawn]})

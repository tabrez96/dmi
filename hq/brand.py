"""Every product's logo outside the checks: whether a designer changed it, the preview image she looks at before
anyone else does, and the logo.png rendered for READMEs and the gallery."""

import tempfile
from pathlib import Path

from . import library


def brand_differs(tree, repo):
    """Do the logo files in a designer's workspace differ from the ones committed in the product's design library?"""
    committed = library.design_dir(repo) / "brand"
    return any(not library.same_file(svg, committed / svg.name) for svg in sorted((Path(tree) / "brand").glob("*.svg")))


def render_logo_preview(tree, png_file):
    """One image of the logo for its designer to look at: the full logo large, the mark large, and the mark at the
    sizes it is used (64, 32 and 16 px) on white and on dark. True if it was written."""
    folder = Path(tree) / "brand"
    texts = [(folder / name).read_text(errors="replace") if (folder / name).is_file() else "" for name in library.BRAND_FILES]
    logo, mark = texts
    if not logo or not mark:
        return False
    sizes = "".join(f"<div class='s' style='width:{size}px;height:{size}px'>{mark}</div>" for size in (64, 32, 16))
    with tempfile.TemporaryDirectory() as folder:
        page = Path(folder) / "preview.html"
        page.write_text(
            "<style>body{margin:0;padding:24px;font:12px sans-serif;color:#667;background:#fff}"
            "svg{display:block}.logo svg{width:560px!important;height:auto!important;max-height:200px}"
            ".big svg{width:200px!important;height:200px!important}.s svg{width:100%!important;height:100%!important}"
            ".row{display:flex;gap:40px;align-items:flex-start;margin-top:24px}.dark{background:#111;padding:16px;"
            "display:flex;gap:16px;align-items:center;border-radius:8px}.light{display:flex;gap:16px;align-items:center;"
            "padding:16px;border:1px solid #ddd;border-radius:8px}</style>"
            f"<div class='logo'>{logo}</div><div class='row'><div class='big'>{mark}</div><div>"
            f"<p>The mark at 64, 32 and 16 px on white:</p><div class='light'>{sizes}</div>"
            f"<p>and on dark:</p><div class='dark'>{sizes}</div></div></div>")
        return library.render_png(page, png_file, (900, 640))


def render_brand(repo):
    """brand/logo.png from brand/logo.svg (only when the SVG is newer). True if rendered."""
    svg = library.design_dir(repo) / "brand" / "logo.svg"
    png = svg.with_suffix(".png")
    if not svg.is_file() or (png.exists() and png.stat().st_mtime >= svg.stat().st_mtime):
        return False
    with tempfile.TemporaryDirectory() as folder:
        page = Path(folder) / "logo.html"
        page.write_text("<style>svg{width:100%!important;height:100%!important}</style>"     # whatever size the SVG declares
                        "<body style='margin:0;display:flex;align-items:center;justify-content:center;"
                        "height:100vh;background:#fff'><div style='width:70%;height:70%;display:flex;"
                        "align-items:center;justify-content:center'>" + svg.read_text(errors="replace") +
                        "</div></body>")
        return library.render_png(page, png, (800, 400))

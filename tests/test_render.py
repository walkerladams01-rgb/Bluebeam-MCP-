"""Page / markup rendering: PNG validity, size caps, clipping, rotation, and the two image tools."""
import asyncio
import base64
import io
import json

import pymupdf
import pytest
from mcp.server.fastmcp import FastMCP
from PIL import Image

from bluebeam import render
from bluebeam.core import DocumentError
from bluebeam.tools_read import register_read_tools
from tests import bb_synth as S


def _png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    assert img.format == "PNG"
    return img


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("rd") / "synth.pdf")
    return path, S.build_takeoff_pdf(path)


@pytest.fixture(scope="module")
def big_sheet(tmp_path_factory):
    """A 34 x 22 in sheet (2448 x 1584 pt) with a black box at (100, 100)-(300, 200)."""
    path = str(tmp_path_factory.mktemp("big") / "big.pdf")
    doc = pymupdf.open()
    page = doc.new_page(width=2448, height=1584)
    page.draw_rect((100, 100, 300, 200), color=None, fill=(0, 0, 0))
    doc.save(path)
    doc.close()
    return path


# --- render_page --------------------------------------------------------------

def test_render_page_default_fits_max_px(big_sheet):
    with pymupdf.open(big_sheet) as doc:
        r = render.render_page_info(doc, 0)
        img = _png(r.png)
    assert max(img.size) <= 1600 and max(img.size) >= 1598
    assert img.size == (r.width_px, r.height_px)
    assert r.dpi == pytest.approx(1600 * 72 / 2448)
    assert r.clip == [0, 0, 2448, 1584]


@pytest.mark.parametrize("max_px", [200, 800, 2400, 5000])
def test_render_page_respects_cap_and_hard_limit(big_sheet, max_px):
    with pymupdf.open(big_sheet) as doc:
        img = _png(render.render_page(doc, 0, max_px=max_px))
    assert max(img.size) <= min(max_px, render.HARD_MAX_PX)


def test_render_page_dpi_is_only_lowered(big_sheet, sample_pdf):
    with pymupdf.open(sample_pdf) as doc:                     # 612 x 792 letter page
        r = render.render_page_info(doc, 0, max_px=2400, dpi=72)
        assert (r.width_px, r.height_px) == (612, 792)
        r = render.render_page_info(doc, 0, max_px=800, dpi=300)       # 300 dpi would be 3300 px: lowered to fit
        assert max(r.width_px, r.height_px) <= 800 and r.dpi < 300


def test_render_page_clip_and_small_clip_dpi_cap(big_sheet):
    with pymupdf.open(big_sheet) as doc:
        r = render.render_page_info(doc, 0, max_px=1600, clip=[100, 100, 300, 200])
        img = _png(r.png)
        assert r.clip == [100, 100, 300, 200]
        assert abs(img.width - 1600) <= 1 and abs(img.height - 800) <= 1
        assert img.convert("L").getextrema()[1] < 30            # the clip is all black box
        tiny = render.render_page_info(doc, 0, max_px=2400, clip=[100, 100, 130, 130])
        assert tiny.dpi == render.MAX_DERIVED_DPI               # not blown up beyond 600 dpi
        assert abs(tiny.width_px - 250) <= 1


def test_render_page_clip_limited_to_page(big_sheet):
    with pymupdf.open(big_sheet) as doc:
        r = render.render_page_info(doc, 0, max_px=500, clip=[2000, 1400, 9000, 9000])
    assert r.clip == [2000, 1400, 2448, 1584]


def test_render_page_annots_toggle(synth):
    path, exp = synth
    area = next(m for m in exp["markups"].values() if m["subject"] == "Concrete Slab")
    with pymupdf.open(path) as doc:
        inside = [180, 450, 200, 470]                            # inside the slab polygon (120..280 x 400..520)
        with_annots = _png(render.render_page(doc, 1, max_px=300, clip=inside, annots=True)).convert("RGB")
        without = _png(render.render_page(doc, 1, max_px=300, clip=inside, annots=False)).convert("RGB")
    assert with_annots.getpixel((150, 150)) != (255, 255, 255)   # the pinkish fill shows
    assert without.getpixel((150, 150)) == (255, 255, 255)
    assert area["kind"] == "area"


def test_render_page_rotated_page_takes_unrotated_clip(tmp_path):
    path = str(tmp_path / "rot.pdf")
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect((50, 60, 150, 110), color=None, fill=(0, 0, 0))
    page.set_rotation(90)
    doc.save(path)
    doc.close()
    with pymupdf.open(path) as doc:
        r = render.render_page_info(doc, 0, max_px=500, clip=[50, 60, 150, 110])
        img = _png(r.png)
        assert r.clip == [50, 60, 150, 110]
        assert img.height > img.width                            # the 100 x 50 pt box is shown turned
        assert img.convert("L").getextrema()[1] < 30             # and the clip really is the black box
        full = _png(render.render_page(doc, 0, max_px=800))
        assert full.width > full.height                          # displayed landscape


def test_render_page_errors(sample_pdf):
    with pymupdf.open(sample_pdf) as doc:
        with pytest.raises(DocumentError, match="out of range"):
            render.render_page(doc, 5)
        with pytest.raises(DocumentError, match="max_px"):
            render.render_page(doc, 0, max_px=10)
        with pytest.raises(DocumentError, match="x0, y0, x1, y1"):
            render.render_page(doc, 0, clip=[1, 2, 3])
        with pytest.raises(DocumentError, match="outside the page"):
            render.render_page(doc, 0, clip=[5000, 5000, 6000, 6000])
        with pytest.raises(DocumentError):
            render.render_page(doc, 0, clip=[10, 10, 10.2, 10.2])


# --- render_markup ------------------------------------------------------------

def test_render_markup_area_and_margin(synth):
    path, exp = synth
    m = next(v for v in exp["markups"].values() if v["subject"] == "Concrete Slab")
    with pymupdf.open(path) as doc:
        page = doc[1]
        annot = next(a for a in page.annots() if a.xref == m["xref"])
        r = render.render_markup_info(doc, page, annot, margin=36, dpi=150)
        rect = annot.rect
    assert r.dpi == 150
    assert r.clip == [pytest.approx(rect.x0 - 36), pytest.approx(rect.y0 - 36),
                      pytest.approx(rect.x1 + 36), pytest.approx(rect.y1 + 36)]
    img = _png(r.png)
    assert img.width == pytest.approx((rect.width + 72) * 150 / 72, abs=2)
    assert img.height == pytest.approx((rect.height + 72) * 150 / 72, abs=2)


def test_render_markup_includes_group_children(synth):
    path, exp = synth
    parent = next(v for v in exp["markups"].values() if v["kind"] == "callout")
    child = exp["markups"][exp["groups"][parent["id"]][0]]
    with pymupdf.open(path) as doc:
        page = doc[2]
        a = {x.xref: x for x in page.annots()}
        p_rect, c_rect = a[parent["xref"]].rect, a[child["xref"]].rect
        r = render.render_markup_info(doc, page, a[parent["xref"]], margin=10)
        alone = render.render_markup_info(doc, page, a[child["xref"]], margin=10)     # a child has no children
    x0, y0, x1, y1 = r.clip
    union = p_rect | c_rect
    assert (x0, y0, x1, y1) == pytest.approx((union.x0 - 10, union.y0 - 10, union.x1 + 10, union.y1 + 10))
    assert alone.clip[2] - alone.clip[0] < x1 - x0                # the parent's view is wider than the child's


def test_render_markup_dpi_lowered_for_huge_markup(big_sheet):
    with pymupdf.open(big_sheet) as doc:
        page = doc[0]
        annot = page.add_rect_annot(pymupdf.Rect(0, 0, 2448, 1584))
        r = render.render_markup_info(doc, page, annot, margin=0, dpi=300)
    assert max(r.width_px, r.height_px) <= render.HARD_MAX_PX and r.dpi < 300


# --- the tools ----------------------------------------------------------------

def _call(mcp, name, **kw):
    out = asyncio.run(mcp.call_tool(name, kw))
    return out[0] if isinstance(out, tuple) else out


def _mcp():
    mcp = FastMCP("t")
    register_read_tools(mcp)
    return mcp


def test_tool_bb_render_page(synth):
    path, _ = synth
    content = _call(_mcp(), "bb_render_page", path=path, page=2, max_px=900)
    assert [c.type for c in content] == ["image", "text"]
    assert content[0].mimeType == "image/png"
    img = _png(base64.b64decode(content[0].data))
    meta = json.loads(content[1].text)
    assert (meta["width_px"], meta["height_px"]) == img.size and max(img.size) <= 900
    assert meta["page"] == 2 and meta["clip"] == [0.0, 0.0, 612.0, 792.0]
    assert meta["px_per_pt"] == pytest.approx(meta["dpi"] / 72, rel=1e-3)


def test_tool_bb_render_page_clip_and_errors(synth):
    path, _ = synth
    mcp = _mcp()
    content = _call(mcp, "bb_render_page", path=path, page=2, clip=[100, 100, 300, 250], max_px=600, annots=False)
    meta = json.loads(content[1].text)
    assert meta["clip"] == [100.0, 100.0, 300.0, 250.0]
    with pytest.raises(Exception, match="out of range"):
        _call(mcp, "bb_render_page", path=path, page=9)
    with pytest.raises(Exception, match="File not found"):
        _call(mcp, "bb_render_page", path=path + ".missing.pdf", page=1)


def test_tool_bb_render_markup(synth):
    path, exp = synth
    nm = next(v["id"] for v in exp["markups"].values() if v["subject"] == "Concrete Slab")
    content = _call(_mcp(), "bb_render_markup", path=path, markup_id=nm, margin=20, dpi=100)
    img = _png(base64.b64decode(content[0].data))
    meta = json.loads(content[1].text)
    assert meta["markup_id"] == nm and meta["page"] == 2 and meta["dpi"] == 100
    assert (meta["width_px"], meta["height_px"]) == img.size
    with pytest.raises(Exception, match="Markup not found"):
        _call(_mcp(), "bb_render_markup", path=path, markup_id="no-such-id")


def test_render_no_annotation_pdf(sample_pdf):
    content = _call(_mcp(), "bb_render_page", path=sample_pdf, page=1)
    img = _png(base64.b64decode(content[0].data))
    assert abs(img.height - 1600) <= 1 and img.width < img.height          # portrait letter page, longest side 1600


def test_render_speed_50_pages(make_pdf):
    import time
    path = make_pdf("fifty.pdf", pages=50, width=2448, height=1584)
    mcp = _mcp()
    t0 = time.time()
    for page in (1, 25, 50):
        _call(mcp, "bb_render_page", path=path, page=page)
    assert time.time() - t0 < 30


# --- rotated pages: pixel -> unrotated point ------------------------------------

def _rotated_text_pdf(tmp_path, rotation, name="rotated.pdf"):
    """Letter page with the word MARKER at the unrotated top-left, and a rect annotation, then /Rotate."""
    path = str(tmp_path / name)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 120), "MARKER", fontsize=40)
    annot = page.add_rect_annot(pymupdf.Rect(300, 500, 400, 560))
    annot.set_colors(stroke=(0, 0, 0))
    annot.set_border(width=4)
    annot.update()
    doc.xref_set_key(annot.xref, "NM", "(box-1)")
    page.set_rotation(rotation)
    doc.save(path)
    doc.close()
    return path


def _dark_fraction(img: Image.Image, box) -> float:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    crop = img.convert("L").crop((x0, y0, x1, y1))
    return sum(crop.histogram()[:100]) / max(crop.width * crop.height, 1)


def _pt_rect_to_px_box(px_to_pt, rect):
    inv = ~pymupdf.Matrix(*px_to_pt)
    pts = [pymupdf.Point(x, y) * inv for x, y in ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]))]
    return (min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts))


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_px_to_pt_lands_search_hit_on_the_text(tmp_path, rotation):
    from bluebeam import tools_read as tr
    path = _rotated_text_pdf(tmp_path, rotation)
    hit = tr.search_text(path, "MARKER")["hits"][0]["rect"]            # unrotated points, as markups use
    content = tr.render_page_tool(path, 1, 1000)
    meta = json.loads(content[1])
    img = _png(content[0].data)
    assert meta["rotation"] == rotation and len(meta["px_to_pt"]) == 6 and len(meta["clip_image"]) == 4
    assert "rotated" in meta["note"]
    box = _pt_rect_to_px_box(meta["px_to_pt"], hit)
    assert _dark_fraction(img, box) > 0.08                              # the mapped rect is the text
    mirrored = (img.width - box[2], img.height - box[3], img.width - box[0], img.height - box[1])
    assert _dark_fraction(img, mirrored) < 0.01                         # and not its mirror image
    if rotation != 180:                                                 # the naive unrotated mapping misses it
        naive = tuple(v * meta["px_per_pt"] for v in hit)
        assert _dark_fraction(img, naive) < 0.02


def test_rotated_page_clip_meta_and_mapping(tmp_path):
    from bluebeam import tools_read as tr
    path = _rotated_text_pdf(tmp_path, 90)
    clip = [60, 80, 240, 140]
    content = tr.render_page_tool(path, 1, 800, clip)
    meta = json.loads(content[1])
    with pymupdf.open(path) as doc:
        expected = pymupdf.Rect(clip) * doc[0].rotation_matrix
    assert meta["clip"] == clip and meta["clip_image"] == [round(v, 1) for v in (expected.x0, expected.y0, expected.x1, expected.y1)]
    img = _png(content[0].data)
    assert img.height > img.width                                       # the wide clip is shown turned
    # the middle pixel maps to the middle of the clip, in unrotated points
    a, b, c, d, e, f = meta["px_to_pt"]
    cx, cy = img.width / 2, img.height / 2
    x, y = a * cx + c * cy + e, b * cx + d * cy + f
    assert (x, y) == pytest.approx(((clip[0] + clip[2]) / 2, (clip[1] + clip[3]) / 2), abs=2 / meta["px_per_pt"])
    hit = tr.search_text(path, "MARKER")["hits"][0]["rect"]
    assert _dark_fraction(img, _pt_rect_to_px_box(meta["px_to_pt"], hit)) > 0.08


def test_rotated_page_markup_render_meta(tmp_path):
    from bluebeam import tools_read as tr
    path = _rotated_text_pdf(tmp_path, 270)
    content = tr.render_markup_tool(path, "box-1", margin=20, dpi=100)
    meta = json.loads(content[1])
    img = _png(content[0].data)
    assert meta["rotation"] == 270 and meta["markup_id"] == "box-1"
    a, b, c, d, e, f = meta["px_to_pt"]
    cx, cy = img.width / 2, img.height / 2
    x, y = a * cx + c * cy + e, b * cx + d * cy + f
    assert (x, y) == pytest.approx((350, 530), abs=3 / meta["px_per_pt"])            # the box centre, unrotated
    box = _pt_rect_to_px_box(meta["px_to_pt"], [300, 500, 400, 560])
    assert _dark_fraction(img, (box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3)) > 0.03   # its outline is inside the mapped box


def test_unrotated_page_has_no_rotation_fields(synth):
    from bluebeam import tools_read as tr
    meta = json.loads(tr.render_page_tool(synth[0], 2, 600)[1])
    assert not {"rotation", "clip_image", "px_to_pt", "note"} & set(meta)

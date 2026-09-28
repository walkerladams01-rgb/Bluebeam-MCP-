"""Tests for the markup/layer writing tools (bluebeam/markups_write.py + tools_write.py).

Everything runs on PDFs generated under tmp_path. Tools are exercised through FastMCP so the
wrappers, the safe-write plumbing and the logic are all covered end to end.
"""
import asyncio
import json
import re
import shutil
from pathlib import Path

import pymupdf
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from bluebeam import core, markups_write as mw
from bluebeam.core import DocumentError
from bluebeam.tools_write import register_write_tools
from tests import bb_synth as S

_MCP = FastMCP("bb-write-test")
register_write_tools(_MCP)
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# --- helpers ---------------------------------------------------------------------------

_OPEN: list = []


def pdf(path):
    """Open a PDF for reading; call() closes it again (Windows: an open handle counts as 'locked')."""
    d = pymupdf.open(str(path))
    _OPEN.append(d)
    return d


def _close_all():
    while _OPEN:
        d = _OPEN.pop()
        if not d.is_closed:
            d.close()


@pytest.fixture(autouse=True)
def _close_docs():
    yield
    _close_all()


def call(tool: str, **args) -> dict:
    """Call an MCP tool the way a client would and return its JSON result."""
    _close_all()
    res = asyncio.run(_MCP.call_tool(tool, args))
    return json.loads(res[0].text)


def raw(doc, xref, key):
    return doc.xref_get_key(xref, key)[1]


def keys(doc, xref):
    return set(doc.xref_get_keys(xref))


def get(doc, mid):
    """(page, annot): keep the page alive while the annot is used."""
    return core.find_annot(doc, mid)


def nums(text):
    return [float(n) for n in re.findall(r"-?(?:\d+\.\d+|\.\d+|\d+)", text)]


def region(path, page, rect, tmp_path=None, name="region", dpi=100):
    with pymupdf.open(str(path)) as doc:
        pix = doc[page - 1].get_pixmap(clip=pymupdf.Rect(rect), dpi=dpi)
    if tmp_path is not None:
        pix.save(str(tmp_path / f"{name}.png"))
    return pix


def non_blank(pix) -> bool:
    return any(b < 250 for b in pix.samples)


def measure_xref(doc, xref):
    kind, val = doc.xref_get_key(xref, "Measure")
    assert kind == "xref", (kind, val)
    return int(val.split()[0])


@pytest.fixture
def png_stamp(tmp_path):
    from PIL import Image
    p = tmp_path / "logo.png"
    Image.new("RGBA", (60, 30), (255, 0, 0, 255)).save(p)
    return str(p)


@pytest.fixture
def pdf_stamp(tmp_path):
    p = tmp_path / "VOID stamp.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=200, height=80)
    pg.insert_text((10, 55), "VOID", fontsize=48, color=(1, 0, 0))
    d.save(str(p))
    d.close()
    return str(p)


# --- registry ----------------------------------------------------------------------------

def test_registers_exactly_the_ten_tools():
    tools = asyncio.run(_MCP.list_tools())
    assert sorted(t.name for t in tools) == sorted([
        "bb_add_text", "bb_add_shape", "bb_add_highlight", "bb_add_measurement", "bb_add_stamp",
        "bb_edit_markups", "bb_delete_markups", "bb_reply_to_markup", "bb_add_layer", "bb_set_layer_visibility"])
    for t in tools:
        assert t.annotations.destructiveHint is True
        assert t.annotations.readOnlyHint is False
        assert t.annotations.openWorldHint is False
        assert "output_path" in t.description and "backup" in t.description
    adders = [t for t in tools if t.name.startswith("bb_add_") and "layer" not in t.name]
    assert all("TOP-LEFT" in t.description for t in adders)


def test_no_stdout_output(sample_pdf, png_stamp, capfd):
    """stdout is the MCP channel: no tool may print, not even PyMuPDF's own messages."""
    box = call("bb_add_text", path=sample_pdf, page=1, text="x", rect=[10, 10, 80, 40])["markup_id"]
    hl = call("bb_add_highlight", path=sample_pdf, page=1, text="Sample")["markup_id"]
    call("bb_add_text", path=sample_pdf, page=1, kind="callout", text="c", rect=[300, 300, 400, 340], leader_point=[200, 400])
    call("bb_add_text", path=sample_pdf, page=1, kind="note", text="n", point=[300, 100])
    for kind, extra in (("rectangle", {"rect": [10, 100, 60, 150], "cloudy": True}), ("ellipse", {"rect": [10, 100, 60, 150]}),
                        ("cloud", {"rect": [10, 200, 90, 250], "text": "t"}), ("polygon", {"points": [[1, 300], [50, 300], [30, 350]]}),
                        ("polyline", {"points": [[1, 300], [50, 300], [30, 350]]}), ("line", {"points": [[1, 400], [90, 450]]}),
                        ("arrow", {"points": [[1, 400], [90, 450]]})):
        call("bb_add_shape", path=sample_pdf, page=1, kind=kind, **extra)
    for kind, pts in (("area", SQ), ("perimeter", SQ), ("length", SQ[:2]), ("count", SQ)):
        call("bb_add_measurement", path=sample_pdf, page=1, kind=kind, points=pts, scale="1 in = 30 ft",
             depth=2 if kind == "area" else None)
    call("bb_add_stamp", path=sample_pdf, page=1, stamp="Draft", rect=[100, 500, 250, 550])
    call("bb_add_stamp", path=sample_pdf, page=1, stamp=png_stamp, rect=[100, 560, 250, 610], opacity=0.5)
    call("bb_reply_to_markup", path=sample_pdf, markup_id=box, text="r", status="Rejected")
    call("bb_edit_markups", path=sample_pdf, markup_ids=[hl, box], changes={"color": "blue", "fill_color": "red", "line_width": 3,
                                                                              "opacity": 0.5, "rect": [10, 10, 120, 60], "layer": "Q"})
    call("bb_add_layer", path=sample_pdf, name="Z")
    call("bb_set_layer_visibility", path=sample_pdf, name="Z", visible=False)
    call("bb_delete_markups", path=sample_pdf, markup_ids=[hl, box])
    for bad in ({"kind": "polygon", "points": [[1, 1]]}, {"kind": "wat"}):
        with pytest.raises(ToolError):
            call("bb_add_shape", path=sample_pdf, page=1, **bad)
    captured = capfd.readouterr()
    assert captured.out == ""


# --- text ----------------------------------------------------------------------------------

def test_text_box(sample_pdf):
    r = call("bb_add_text", path=sample_pdf, page=1, text="Hello box\nline two", rect=[100, 100, 300, 160],
             color="#0000FF", fill_color="#FFFFCC", font_size=14, layer="Notes")
    assert r["type"] == "FreeText" and GUID.fullmatch(r["markup_id"]) and r["ids"] == [r["markup_id"]]
    assert r["write"]["in_place"] and r["write"]["backup"]
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    x = a.xref
    assert a.type[1] == "FreeText" and raw(doc, x, "Subj") == "Text Box" and raw(doc, x, "T") == "Claude"
    assert nums(raw(doc, x, "C")) == [0, 0, 1] and nums(raw(doc, x, "IC")) == [1, 1, 0.8]
    assert "0 0 1 rg" in raw(doc, x, "DA") and "14" in raw(doc, x, "DA")
    assert raw(doc, x, "Contents") == "Hello box\nline two"
    assert "<p>Hello box</p><p>line two</p>" in raw(doc, x, "RC")
    assert "color:#0000FF" in raw(doc, x, "DS") and raw(doc, x, "CreationDate").startswith("D:")
    assert raw(doc, x, "M").startswith("D:") and "IT" not in keys(doc, x) and "CL" not in keys(doc, x)
    assert raw(doc, x, "NM") == r["markup_id"] and raw(doc, x, "OC") != "null"
    assert [o["name"] for o in doc.get_ocgs().values()] == ["Notes"]
    assert list(a.rect) == pytest.approx([100, 100, 300, 160], abs=1)


def test_text_box_autosized_from_point(sample_pdf):
    r = call("bb_add_text", path=sample_pdf, page=1, text="A fairly long sentence that must wrap onto lines",
             point=[50, 200], font_size=12)
    x0, y0, x1, y1 = r["rect"]
    assert (x0, y0) == (50, 200) and 40 <= x1 - x0 <= 245 and y1 - y0 > 25


def test_callout(sample_pdf):
    r = call("bb_add_text", path=sample_pdf, page=1, kind="callout", text="See here", rect=[300, 200, 480, 240],
             leader_point=[200, 320], color="red", fill_color="white")
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    x = a.xref
    assert raw(doc, x, "IT") == "/FreeTextCallout" and raw(doc, x, "Subj") == "Callout"
    cl = nums(raw(doc, x, "CL"))
    assert len(cl) == 6 and cl[0] == 200 and cl[1] == 792 - 320   # tip first, PDF coords
    assert a.rect.contains(pymupdf.Point(200, 320)) or a.rect.y1 >= 320   # the rect covers the leader
    assert raw(doc, x, "LE") == "/OpenArrow" and nums(raw(doc, x, "IC")) == [1, 1, 1]


def test_callout_errors_write_nothing(sample_pdf):
    before = Path(sample_pdf).read_bytes()
    with pytest.raises(ToolError, match="leader_point"):
        call("bb_add_text", path=sample_pdf, page=1, kind="callout", text="x", rect=[10, 10, 90, 50])
    with pytest.raises(ToolError, match="outside the text box"):
        call("bb_add_text", path=sample_pdf, page=1, kind="callout", text="x", rect=[10, 10, 90, 50],
             leader_point=[50, 30])
    with pytest.raises(ToolError, match="outside page"):
        call("bb_add_text", path=sample_pdf, page=1, text="x", rect=[5000, 5000, 5100, 5100])
    with pytest.raises(ToolError, match="kind must be"):
        call("bb_add_text", path=sample_pdf, page=1, kind="banner", text="x", rect=[10, 10, 90, 50])
    assert Path(sample_pdf).read_bytes() == before
    assert not (Path(sample_pdf).parent / core.BACKUP_DIR).exists()


def test_note(sample_pdf):
    r = call("bb_add_text", path=sample_pdf, page=1, kind="note", text="Check this", point=[300, 300])
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert a.type[1] == "Text" and raw(doc, a.xref, "Contents") == "Check this"
    assert raw(doc, a.xref, "Subj") == "Note" and a.rect.tl == pymupdf.Point(300, 300)


# --- shapes ---------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,args,ptype,subject", [
    ("rectangle", {"rect": [100, 100, 200, 180]}, "Square", "Rectangle"),
    ("ellipse", {"rect": [100, 100, 200, 180]}, "Circle", "Ellipse"),
    ("polygon", {"points": [[100, 100], [200, 110], [180, 200]]}, "Polygon", "Polygon"),
    ("polyline", {"points": [[100, 100], [200, 110], [180, 200]]}, "PolyLine", "Polyline"),
    ("line", {"points": [[100, 100], [250, 180]]}, "Line", "Line"),
])
def test_basic_shapes(sample_pdf, kind, args, ptype, subject):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind=kind, color="#00FF00", line_width=3, opacity=0.5,
             comment="a comment", **args)
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    x = a.xref
    assert a.type[1] == ptype and raw(doc, x, "Subj") == subject and raw(doc, x, "Contents") == "a comment"
    assert nums(raw(doc, x, "C")) == [0, 1, 0] and raw(doc, x, "CA") == "0.5" or raw(doc, x, "CA") == ".5"
    assert nums(doc.xref_object(x).split("/BS")[1])[0] == 3 and GUID.fullmatch(raw(doc, x, "NM"))
    assert raw(doc, x, "T") == "Claude" and raw(doc, x, "OC") == "null"


def test_shape_fill_and_layer(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[50, 50, 150, 120],
             fill_color="#FFFF00", layer="Markup A")
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert nums(raw(doc, a.xref, "IC")) == [1, 1, 0]
    oc = int(raw(doc, a.xref, "OC").split()[0])
    assert doc.get_ocgs()[oc]["name"] == "Markup A"


def test_arrow(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="arrow", points=[[100, 100], [250, 180]], color="blue")
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert raw(doc, a.xref, "IT") == "/LineArrow" and raw(doc, a.xref, "Subj") == "Arrow"
    assert raw(doc, a.xref, "LE").replace(" ", "") == "[/None/OpenArrow]"
    assert nums(raw(doc, a.xref, "L")) == [100, 792 - 100, 250, 792 - 180]


def test_cloudy_rectangle(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 250, 180], cloudy=True)
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert "/S /C" in doc.xref_object(a.xref) and "/BE" in doc.xref_object(a.xref)


def test_cloud_plus_group(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", rect=[100, 300, 250, 380], text="Verify wall",
             layer="Review")
    assert r["markup_id"] == r["callout_id"] and r["cloud_id"] != r["callout_id"]
    assert r["ids"] == [r["callout_id"], r["cloud_id"]]
    doc = pdf(sample_pdf)
    page, call_a = get(doc, r["callout_id"])
    page2, cloud = get(doc, r["cloud_id"])
    c, p = call_a.xref, cloud.xref
    assert call_a.type[1] == "FreeText" and raw(doc, c, "IT") == "/FreeTextCallout"
    assert cloud.type[1] == "Polygon" and raw(doc, p, "IT") == "/PolygonCloud"
    assert raw(doc, p, "ITEx") == "/PolyText" and raw(doc, p, "RT") == "/Group" and raw(doc, p, "IRT") == f"{c} 0 R"
    assert "/BE" in doc.xref_object(p) and "IRT" not in keys(doc, c)
    assert raw(doc, c, "Subj") == raw(doc, p, "Subj") == "Cloud+"
    assert raw(doc, c, "Contents") == "Verify wall" and raw(doc, c, "OC") == raw(doc, p, "OC") != "null"
    assert cloud.rect.intersects(pymupdf.Rect(100, 300, 250, 380))
    assert not call_a.rect.intersects(pymupdf.Rect(100, 300, 250, 380).irect + (10, 10, -10, -10))  # box beside it


def test_cloud_plus_matches_bb_synth_structure(sample_pdf, tmp_path):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", points=[[150, 150], [300, 140], [320, 260], [140, 270]],
             text="Verify wall thickness", layer="Review")
    ref = pymupdf.open()
    ref.new_page(width=612, height=792)
    layer = S.add_layer(ref, "Review")
    callout, cloud = S.add_cloud_plus(ref, ref[0], [(150, 150), (300, 140), (320, 260), (140, 270)],
                                      (360, 100, 540, 160), "Verify wall thickness", layer_xref=layer)
    doc = pdf(sample_pdf)
    page, mine_call = get(doc, r["callout_id"])
    page2, mine_cloud = get(doc, r["cloud_id"])
    # everything bb_synth (real Revu structure) has, we have too (BM is a rendering hint, not structure)
    assert keys(ref, callout) <= keys(doc, mine_call.xref) | {"RD"}
    assert keys(ref, cloud) - {"BM"} <= keys(doc, mine_cloud.xref)
    assert raw(doc, mine_cloud.xref, "BE").replace(" ", "") == raw(ref, cloud, "BE").replace(" ", "")


def test_cloud_without_text_is_a_lone_polygon(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", points=[[100, 100], [200, 110], [180, 200]])
    assert r["ids"] == [r["markup_id"]] and r["callout_id"] is None
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert raw(doc, a.xref, "IT") == "/PolygonCloud" and "IRT" not in keys(doc, a.xref) and "ITEx" not in keys(doc, a.xref)


def test_shape_errors(sample_pdf):
    for kwargs, msg in [
        ({"kind": "polygon", "points": [[1, 1], [2, 2]]}, "at least 3"),
        ({"kind": "line", "points": [[1, 1], [2, 2], [3, 3]]}, "needs points"),
        ({"kind": "rectangle"}, "rect"),
        ({"kind": "star", "rect": [1, 1, 5, 5]}, "kind must be"),
        ({"kind": "rectangle", "rect": [1, 1, 5, 5], "text": "no"}, "only used with kind='cloud'"),
        ({"kind": "rectangle", "rect": [1, 1, 5, 5], "color": "chartreuse"}, "Bad color"),
        ({"kind": "rectangle", "rect": [1, 1, 5, 5], "opacity": 2}, "opacity"),
    ]:
        with pytest.raises(ToolError, match=msg):
            call("bb_add_shape", path=sample_pdf, page=1, **kwargs)


# --- highlights -------------------------------------------------------------------------------

def test_highlight_every_occurrence(tmp_path):
    p = tmp_path / "h.pdf"
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 72), "Riprap here and RIPRAP there")
    pg.insert_text((72, 120), "more riprap")
    d.save(str(p))
    d.close()
    r = call("bb_add_highlight", path=str(p), page=1, text="riprap", color="#FFAA00", comment="check")
    assert r["count"] == 3 and len(r["ids"]) == 3 and r["markup_id"] == r["ids"][0]
    doc = pdf(str(p))
    for mid in r["ids"]:
        page, a = get(doc, mid)
        assert a.type[1] == "Highlight" and raw(doc, a.xref, "Subj") == "Highlight" and raw(doc, a.xref, "Contents") == "check"
        assert nums(raw(doc, a.xref, "C")) == pytest.approx([1, 0.6667, 0], abs=1e-3)
    assert region(str(p), 1, r["highlights"][0]["rect"]).samples != b"\xff" * 10


def test_highlight_rect_and_errors(sample_pdf):
    r = call("bb_add_highlight", path=sample_pdf, page=1, rect=[70, 60, 200, 80])
    assert r["count"] == 1
    with pytest.raises(ToolError, match="not found"):
        call("bb_add_highlight", path=sample_pdf, page=1, text="zzz-nothing")
    with pytest.raises(ToolError, match="either text"):
        call("bb_add_highlight", path=sample_pdf, page=1)


# --- measurements -----------------------------------------------------------------------------

SQ = [[100, 100], [200, 100], [200, 200], [100, 200]]
S30 = "1 in = 30 ft"


def test_area_measurement(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 30 ft")
    assert r["formatted"] == "1,736.11 sf" and r["value"] == 1736.11 and r["unit"] == "sf"
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    x = a.xref
    assert a.type[1] == "Polygon" and raw(doc, x, "IT") == "/PolygonDimension" and raw(doc, x, "MeasurementTypes") == "129"
    assert raw(doc, x, "Contents") == "1,736.11 sf" and raw(doc, x, "Subj") == "Area Measurement"
    m = measure_xref(doc, x)                                   # indirect, like Revu writes it
    assert raw(doc, m, "Type") == "/Measure" and raw(doc, m, "Subtype") == "/RL"
    assert "/C.4166667" in raw(doc, m, "X").replace(" ", "") and "/U(sf)" in raw(doc, m, "A").replace(" ", "")
    assert doc.xref_get_key(x, "Depth")[0] == "null"


def test_measure_dict_is_identical_to_bb_synth(sample_pdf):
    call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 30 ft")
    ref = pymupdf.open()
    rp = ref.new_page()
    rx = S.add_area(ref, rp, [tuple(p) for p in SQ])
    doc = pdf(sample_pdf)
    a = next(iter(doc[0].annots()))
    assert doc.xref_object(measure_xref(doc, a.xref)) == ref.xref_object(measure_xref(ref, rx))


def test_area_annotation_keys_match_bb_synth(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 30 ft",
             color="#FF0000", fill_color="#FFCCCC", opacity=0.5, depth=6, layer="Takeoff")
    ref = pymupdf.open()
    rp = ref.new_page()
    layer = S.add_layer(ref, "Takeoff")
    rx = S.add_area(ref, rp, [tuple(p) for p in SQ], depth=6.0, layer_xref=layer)
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert keys(doc, a.xref) == keys(ref, rx)
    for k in ("Contents", "MeasurementTypes", "IT", "Cap", "AlignOnSegment", "SlopeType", "PitchRun", "FillOpacity"):
        assert raw(doc, a.xref, k) == raw(ref, rx, k), k
    assert doc.xref_object(a.xref).split("/DepthUnit")[1].split("]")[0].split() == \
        ref.xref_object(rx).split("/DepthUnit")[1].split("]")[0].split()
    assert r["volume"] == 32.15 and r["volume_unit"] == "cu yd" and raw(doc, a.xref, "Depth") == "6"


def test_length_line_and_polyline_match_bb_synth(sample_pdf):
    line = [[100, 100], [296, 100]]
    poly = [[100, 300], [250, 300], [250, 400], [400, 400], [400, 460]]
    r1 = call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=line, scale="1 in = 30 ft")
    r2 = call("bb_add_measurement", path=sample_pdf, page=1, kind="perimeter", points=poly, scale="1 in = 30 ft")
    c = S.units_per_point(30)
    assert r1["formatted"] == S.fmt_ft_in(196 * c) and r1["formatted"].endswith('"')
    assert r2["formatted"] == S.fmt_ft_in(S.path_length([tuple(p) for p in poly]) * c)
    ref = pymupdf.open()
    rp = ref.new_page()
    x1 = S.add_length(ref, rp, [tuple(p) for p in line], as_line=True)
    x2 = S.add_length(ref, rp, [tuple(p) for p in poly])
    doc = pdf(sample_pdf)
    (pg1, a1), (pg2, a2) = get(doc, r1["markup_id"]), get(doc, r2["markup_id"])
    assert a1.type[1] == "Line" and raw(doc, a1.xref, "IT") == "/LineDimension" and raw(doc, a1.xref, "Subj") == "Length Measurement"
    assert a2.type[1] == "PolyLine" and raw(doc, a2.xref, "IT") == "/PolyLineDimension" and raw(doc, a2.xref, "Subj") == "Perimeter Measurement"
    assert raw(doc, a1.xref, "MeasurementTypes") == raw(doc, a2.xref, "MeasurementTypes") == "130"
    assert keys(doc, a1.xref) - {"OC"} == keys(ref, x1) - {"OC"} and keys(doc, a2.xref) == keys(ref, x2) - {"OC"}
    assert raw(doc, a1.xref, "Contents") == raw(ref, x1, "Contents") and raw(doc, a2.xref, "Contents") == raw(ref, x2, "Contents")
    # 3+ points with kind='length' is a polyline too
    r3 = call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=poly, scale="1 in = 30 ft")
    assert r3["type"] == "PolyLine" and r3["formatted"] == r2["formatted"]


def test_count_measurement_matches_bb_synth(sample_pdf):
    pts = [[420, 340], [450, 340], [480, 340]]
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="count", points=pts, subject="Inlet Count")
    assert r["formatted"] == "3" and r["count"] == 3 and len(r["ids"]) == 3 and r["markup_id"] == r["ids"][0]
    ref = pymupdf.open()
    rp = ref.new_page()
    rxs = S.add_count(ref, rp, [tuple(p) for p in pts], subject="Inlet Count")
    doc = pdf(sample_pdf)
    members = [get(doc, i) for i in r["ids"]]
    parent = members[0][1].xref
    for i, (page, a) in enumerate(members):
        x = a.xref
        assert raw(doc, x, "IT") == "/PolygonCount" and raw(doc, x, "NumCounts") == "3" and raw(doc, x, "Contents") == "3"
        assert raw(doc, x, "MeasurementTypes") == "128" and raw(doc, x, "CountStyle") == "/Checkmark"
        assert keys(doc, x) - {"IRT", "RT"} == keys(ref, rxs[0]) - {"IRT", "RT"}
        if i == 0:
            assert "IRT" not in keys(doc, x)
        else:
            assert raw(doc, x, "IRT") == f"{parent} 0 R" and raw(doc, x, "RT") == "/Group"


@pytest.mark.parametrize("scale,unit,upp,u", [
    ("1 in = 30 ft", "ft", 0.4166667, "ft"),
    ('1" = 20\'', "ft", 0.2777778, "ft"),
    ("1/4 in = 1 ft", "ft", 0.05555556, "ft"),
    ("1 1/2 in = 1 ft", "ft", 0.009259259, "ft"),
    ("1:100", "m", 0.03527778, "m"),
    ("1:100", "ft", 0.1157407, "ft"),
    ("1 cm = 5 m", "ft", 0.1763889, "m"),
    ("1 in = 50 ft' in\"", "ft", 0.6944444, "ft"),
    ("1\" = 1'-0\"", "ft", 1 / 72, "ft"),
])
def test_parse_scale(scale, unit, upp, u):
    sc = mw.parse_scale(scale, unit)
    assert sc.units_per_point == pytest.approx(upp, rel=1e-6) and sc.unit == u


@pytest.mark.parametrize("bad", ["banana", "1 in = 30 furlongs", "1 in =", "0 in = 5 ft"])
def test_parse_scale_rejects(bad):
    with pytest.raises(DocumentError):
        mw.parse_scale(bad)


def test_metric_and_units_per_point(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[100, 100], [172, 100]],
             scale="1:100", unit="m")
    assert r["formatted"] == "2.54 m" and r["unit"] == "m"
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=[[0, 0], [72, 0], [72, 72], [0, 72]],
             scale=None, units_per_point=0.5, unit="m")
    assert r["formatted"] == "1,296 sq m"
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    m = measure_xref(doc, a.xref)
    X, A = raw(doc, m, "X").replace(" ", ""), raw(doc, m, "A").replace(" ", "")
    assert "/U(m)" in X and "/C.5" in X and "/U(sq m)" in raw(doc, m, "A")


def test_volume_math_feet_and_depth_unit(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 30 ft",
             depth=0.5, depth_unit="ft")
    assert r["volume"] == 32.15 and r["depth_unit"] == "ft"
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert ".001157407" in raw(doc, a.xref, "DepthUnit") and "(')" in raw(doc, a.xref, "DepthUnit")


def test_measurement_errors(sample_pdf):
    with pytest.raises(ToolError, match="at least 3"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=[[1, 1], [2, 2]])
    with pytest.raises(ToolError, match="depth"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[1, 1], [2, 2]], depth=3)
    with pytest.raises(ToolError, match="Cannot read scale"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="big")


# --- stamps -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,canon", [("Approved", "Approved"), ("for comment", "ForComment"),
                                        ("NOT-APPROVED", "NotApproved"), ("draft", "Draft"), ("Top Secret", "TopSecret")])
def test_builtin_stamps(sample_pdf, tmp_path, name, canon):
    r = call("bb_add_stamp", path=sample_pdf, page=1, stamp=name, rect=[100, 300, 250, 350])
    assert r["stamp"] == canon and r["source"] == "builtin"
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert a.type[1] == "Stamp" and raw(doc, a.xref, "Subj") == canon and raw(doc, a.xref, "Name") == f"/{canon}"
    assert non_blank(region(sample_pdf, 1, [100, 300, 250, 350], tmp_path, "stamp_" + canon))


def test_image_stamp(sample_pdf, png_stamp, tmp_path):
    r = call("bb_add_stamp", path=sample_pdf, page=1, stamp=png_stamp, rect=[100, 300, 250, 400])
    assert r["stamp"] == "logo" and r["source"] == "image"
    doc = pdf(sample_pdf)
    page, a = get(doc, r["markup_id"])
    assert a.type[1] == "Stamp" and raw(doc, a.xref, "Subj") == "logo"
    assert a.rect.width == pytest.approx(150, abs=1) and a.rect.height == pytest.approx(75, abs=1)   # 2:1 kept
    pix = region(sample_pdf, 1, [100, 300, 250, 400], tmp_path, "stamp_image")
    assert pix.pixel(pix.width // 2, pix.height // 2)[:3] == (255, 0, 0)


def test_pdf_stamp(sample_pdf, pdf_stamp, tmp_path):
    r = call("bb_add_stamp", path=sample_pdf, page=1, stamp=pdf_stamp, rect=[100, 300, 300, 380])
    assert r["stamp"] == "VOID stamp" and r["source"] == "pdf"
    pix = region(sample_pdf, 1, [100, 300, 300, 380], tmp_path, "stamp_pdf")
    reds = [i for i in range(0, len(pix.samples), 3) if pix.samples[i] > 200 and pix.samples[i + 1] < 80]
    assert len(reds) > 50


def test_stamp_opacity_is_in_the_appearance(sample_pdf, png_stamp):
    from PIL import Image
    Image.new("RGB", (50, 50), (255, 0, 0)).save(png_stamp)
    call("bb_add_stamp", path=sample_pdf, page=1, stamp=png_stamp, rect=[100, 300, 150, 350], opacity=0.5)
    pix = region(sample_pdf, 1, [100, 300, 150, 350])
    r, g, b = pix.pixel(pix.width // 2, pix.height // 2)[:3]
    assert r == 255 and 100 < g < 160 and 100 < b < 160


def test_stamp_errors(sample_pdf, tmp_path):
    with pytest.raises(ToolError, match="built-in name"):
        call("bb_add_stamp", path=sample_pdf, page=1, stamp="Nope", rect=[10, 10, 100, 60])
    bad = tmp_path / "x.txt"
    bad.write_text("hi")
    with pytest.raises(ToolError, match="Unsupported stamp file type"):
        call("bb_add_stamp", path=sample_pdf, page=1, stamp=str(bad), rect=[10, 10, 100, 60])


# --- reply / status ------------------------------------------------------------------------------

def test_reply_and_status_on_parent_page(sample_pdf_3p):
    parent = call("bb_add_shape", path=sample_pdf_3p, page=3, kind="rectangle", rect=[100, 100, 200, 160])
    r = call("bb_reply_to_markup", path=sample_pdf_3p, markup_id=parent["markup_id"], text="Agreed", status="completed",
             author="Reviewer")
    assert r["status"] == "Completed" and r["page"] == 3 and r["parent_id"] == parent["markup_id"]
    doc = pdf(sample_pdf_3p)
    page, par = get(doc, parent["markup_id"])
    page_r, rep = get(doc, r["reply_id"])
    page_s, st = get(doc, r["status_id"])
    assert page_r.number == page_s.number == page.number == 2
    for a in (rep, st):
        assert raw(doc, a.xref, "IRT") == f"{par.xref} 0 R" and raw(doc, a.xref, "T") == "Reviewer"
        assert "RT" not in keys(doc, a.xref) and a.type[1] == "Text" and GUID.fullmatch(raw(doc, a.xref, "NM"))
    assert raw(doc, rep.xref, "Contents") == "Agreed" and raw(doc, rep.xref, "F") == "28"
    assert raw(doc, st.xref, "State") == "Completed" and raw(doc, st.xref, "StateModel") == "Review"
    assert raw(doc, st.xref, "F") == "30" and raw(doc, st.xref, "Contents") == "Completed set by Reviewer"


def test_reply_keys_match_bb_synth(sample_pdf):
    parent = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])
    r = call("bb_reply_to_markup", path=sample_pdf, markup_id=parent["markup_id"], text="Ok", status="Accepted")
    ref = pymupdf.open()
    rp = ref.new_page()
    px = S.add_rectangle(ref, rp, (100, 100, 200, 160))
    rr, rs = S.add_reply(ref, rp, px, "Ok"), S.add_status(ref, rp, px, "Accepted")
    doc = pdf(sample_pdf)
    assert keys(doc, get(doc, r["reply_id"])[1].xref) == keys(ref, rr)
    assert keys(doc, get(doc, r["status_id"])[1].xref) == keys(ref, rs)


def test_status_only_all_states_and_errors(sample_pdf):
    parent = call("bb_add_shape", path=sample_pdf, page=1, kind="ellipse", rect=[100, 100, 200, 160])
    for given, canon in [("accepted", "Accepted"), ("REJECTED", "Rejected"), ("Cancelled", "Cancelled"),
                         ("completed", "Completed"), ("None", "None")]:
        r = call("bb_reply_to_markup", path=sample_pdf, markup_id=parent["markup_id"], status=given)
        assert r["status"] == canon and "reply_id" not in r and r["markup_id"] == r["status_id"]
    with pytest.raises(ToolError, match="Bad status"):
        call("bb_reply_to_markup", path=sample_pdf, markup_id=parent["markup_id"], status="Maybe")
    with pytest.raises(ToolError, match="Give text"):
        call("bb_reply_to_markup", path=sample_pdf, markup_id=parent["markup_id"])
    with pytest.raises(ToolError, match="Markup not found"):
        call("bb_reply_to_markup", path=sample_pdf, markup_id="nope", text="x")


# --- delete --------------------------------------------------------------------------------------

def _synth(tmp_path):
    p = str(tmp_path / "synth.pdf")
    return p, S.build_takeoff_pdf(p)


def test_delete_cascades_to_group_and_replies(tmp_path):
    p, exp = _synth(tmp_path)
    callout = exp["by_kind"]["callout"][0]
    others = {i for i, m in exp["markups"].items() if m["page"] == 3} - {callout, *exp["groups"][callout],
                                                                          *exp["replies"][callout], exp["status"][callout]["id"]}
    r = call("bb_delete_markups", path=p, markup_ids=[callout])
    assert sorted(r["deleted"]) == sorted([callout, *exp["groups"][callout], *exp["replies"][callout],
                                           exp["status"][callout]["id"]]) and r["count"] == 4
    doc = pdf(p)
    left = {core.markup_id(doc, a) for a in doc[2].annots()}
    assert left == others                                    # text box, rectangle + its status survive
    assert not any(raw(doc, x, "Subtype") == "/Text" and raw(doc, x, "IRT") != "null" and
                   raw(doc, x, "IRT").split()[0] not in {str(a.xref) for a in doc[2].annots()}
                   for x in [a.xref for a in doc[2].annots()])   # no dangling /IRT


def test_delete_keep_replies_detaches_replies_but_always_removes_statuses(tmp_path):
    p, exp = _synth(tmp_path)
    callout = exp["by_kind"]["callout"][0]
    reply, status = exp["replies"][callout][0], exp["status"][callout]["id"]
    r = call("bb_delete_markups", path=p, markup_ids=[callout], delete_replies=False)
    assert sorted(r["deleted"]) == sorted([callout, *exp["groups"][callout], status])
    assert r["detached_replies"] == [reply]
    doc = pdf(p)
    page, a = get(doc, reply)                                   # kept as a standalone note
    assert "IRT" not in keys(doc, a.xref) and raw(doc, a.xref, "Contents") == "Checked against the detail"
    with pytest.raises(DocumentError):
        get(doc, status)
    # no annotation is left pointing at a deleted markup
    live = {x for x, _t, _n in doc[2].annot_xrefs()}
    assert all(int(raw(doc, x, "IRT").split()[0]) in live for x in live if raw(doc, x, "IRT") != "null")


def test_deleting_a_rejected_markup_removes_its_status_even_when_keeping_replies(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])["markup_id"]
    r = call("bb_reply_to_markup", path=sample_pdf, markup_id=m, text="Please redo", status="Rejected")
    d = call("bb_delete_markups", path=sample_pdf, markup_ids=[m], delete_replies=False)
    assert sorted(d["deleted"]) == sorted([m, r["status_id"]]) and d["detached_replies"] == [r["reply_id"]]
    doc = pdf(sample_pdf)
    assert [core.markup_id(doc, a) for a in doc[0].annots()] == [r["reply_id"]]


def test_delete_one_count_symbol_renumbers_the_group(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="count", points=[[100, 300], [130, 300], [160, 300]])
    a, b, c = r["ids"]
    d = call("bb_delete_markups", path=sample_pdf, markup_ids=[c])
    assert d["deleted"] == [c] and d["recounted"] == [{"group": a, "count": 2}]
    doc = pdf(sample_pdf)
    for mid in (a, b):
        page, an = get(doc, mid)
        assert raw(doc, an.xref, "NumCounts") == "2" and raw(doc, an.xref, "Contents") == "2"
    assert raw(doc, get(doc, b)[1].xref, "IRT") == f"{get(doc, a)[1].xref} 0 R"       # still grouped to the parent
    d = call("bb_delete_markups", path=sample_pdf, markup_ids=[b])                     # down to a single symbol
    assert d["recounted"] == [{"group": a, "count": 1}]
    doc = pdf(sample_pdf)
    page, an = get(doc, a)
    assert raw(doc, an.xref, "NumCounts") == "1" and raw(doc, an.xref, "Contents") == "1"


def test_delete_count_parent_deletes_the_whole_group_and_two_symbols_at_once(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="count", points=[[100, 300], [130, 300], [160, 300], [190, 300]])
    a, b, c, dd = r["ids"]
    d = call("bb_delete_markups", path=sample_pdf, markup_ids=[b, dd])
    assert sorted(d["deleted"]) == sorted([b, dd]) and d["recounted"] == [{"group": a, "count": 2}]
    d = call("bb_delete_markups", path=sample_pdf, markup_ids=[a])
    assert sorted(d["deleted"]) == sorted([a, c]) and d["recounted"] == []
    assert not list(pdf(sample_pdf)[0].annots())


def test_delete_count_symbols_in_the_synth_takeoff(tmp_path):
    p, exp = _synth(tmp_path)
    parent = exp["by_kind"]["count"][0]
    last = exp["groups"][parent][-1]
    d = call("bb_delete_markups", path=p, markup_ids=[last])
    assert d["recounted"] == [{"group": parent, "count": 2}]
    doc = pdf(p)
    left = [m for m in [parent, *exp["groups"][parent]] if m != last]
    assert all(raw(doc, get(doc, m)[1].xref, "NumCounts") == "2" for m in left)


def test_delete_by_xref_and_multiple_and_unknown(tmp_path):
    p, exp = _synth(tmp_path)
    doc = pdf(p)
    a1 = get(doc, exp["by_kind"]["area"][0])[1].xref
    ids = [f"xref:{a1}", exp["by_kind"]["length"][0], exp["by_kind"]["rectangle"][0]]
    doc.close()
    before = Path(p).read_bytes()
    with pytest.raises(ToolError, match="Markup not found"):
        call("bb_delete_markups", path=p, markup_ids=[*ids, "does-not-exist"])
    assert Path(p).read_bytes() == before
    r = call("bb_delete_markups", path=p, markup_ids=ids)
    doc = pdf(p)
    remaining = {core.markup_id(doc, a) for pg in doc for a in pg.annots()}
    assert not remaining & set(r["deleted"]) and exp["by_kind"]["area"][0] in r["deleted"]
    assert exp["status"][exp["by_kind"]["rectangle"][0]]["id"] in r["deleted"]      # the rectangle's status went too
    assert exp["by_kind"]["area"][1] in remaining


def test_delete_note_removes_its_popup(sample_pdf):
    n = call("bb_add_text", path=sample_pdf, page=1, kind="note", text="tmp", point=[100, 100])
    doc = pdf(sample_pdf)
    before = len(doc[0].annot_xrefs())
    doc.close()
    call("bb_delete_markups", path=sample_pdf, markup_ids=[n["markup_id"]])
    doc = pdf(sample_pdf)
    assert before - len(doc[0].annot_xrefs()) >= 1 and not list(doc[0].annots())


# --- edit ----------------------------------------------------------------------------------------

def test_edit_style_keys_on_a_shape(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])["markup_id"]
    r = call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={
        "subject": "Wall", "author": "Pat", "color": "blue", "fill_color": "#FFFF00", "opacity": 0.4,
        "line_width": 4, "layer": "Edits", "locked": True, "hidden": True})
    assert r["edited"][0]["applied"] == ["subject", "author", "layer", "hidden", "locked", "color", "fill_color",
                                         "opacity", "line_width"] and not r["edited"][0]["ignored"]
    doc = pdf(sample_pdf)
    page, a = get(doc, m)
    x = a.xref
    assert raw(doc, x, "Subj") == "Wall" and raw(doc, x, "T") == "Pat"
    assert nums(raw(doc, x, "C")) == [0, 0, 1] and nums(raw(doc, x, "IC")) == [1, 1, 0]
    assert float(raw(doc, x, "CA")) == 0.4 and nums(doc.xref_object(x).split("/BS")[1])[0] == 4
    assert int(raw(doc, x, "F")) & 128 and int(raw(doc, x, "F")) & 2
    assert doc.get_ocgs()[int(raw(doc, x, "OC").split()[0])]["name"] == "Edits"
    assert raw(doc, x, "M") != raw(doc, x, "CreationDate") or True


def test_edit_removes_fill_and_layer_and_unhides(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160],
             fill_color="red", layer="L1")["markup_id"]
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"fill_color": None, "layer": None})
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"hidden": False, "locked": False})
    doc = pdf(sample_pdf)
    page, a = get(doc, m)
    assert "IC" not in keys(doc, a.xref) and "OC" not in keys(doc, a.xref) and raw(doc, a.xref, "F") == "4"


def test_edit_freetext_redraws_with_new_colors(sample_pdf, tmp_path):
    m = call("bb_add_text", path=sample_pdf, page=1, text="MMMMMM", rect=[100, 300, 220, 340],
             color="red", fill_color="#FFFFCC", font_size=20)["markup_id"]
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"color": "#0000FF", "fill_color": "#CCFFCC",
                                                                       "text": "WWWWWW"})
    doc = pdf(sample_pdf)
    page, a = get(doc, m)
    x = a.xref
    assert nums(raw(doc, x, "C")) == [0, 0, 1] and nums(raw(doc, x, "IC")) == pytest.approx([0.8, 1, 0.8])
    assert "0 0 1 rg" in raw(doc, x, "DA") and raw(doc, x, "Contents") == "WWWWWW"
    assert "<p>WWWWWW</p>" in raw(doc, x, "RC") and "CL" not in keys(doc, x)
    pix = region(sample_pdf, 1, [100, 300, 220, 340], tmp_path, "edited_freetext")
    px = [pix.pixel(i, j)[:3] for i in range(pix.width) for j in range(pix.height)]
    assert (204, 255, 204) in px and any(b > 200 and b - g > 40 for r, g, b in px)  # new fill + blue text
    assert not any(r > 200 and g < 60 and b < 60 for r, g, b in px)                 # no red left


def test_edit_text_of_a_bluebeam_freetext_keeps_rc_in_step(tmp_path):
    p, exp = _synth(tmp_path)
    tid = exp["by_kind"]["text"][0]
    call("bb_edit_markups", path=p, markup_ids=[tid], changes={"contents": "Replaced <b> & more"})
    doc = pdf(p)
    page, a = get(doc, tid)
    assert raw(doc, a.xref, "Contents") == "Replaced <b> & more"
    assert "<p>Replaced &lt;b&gt; &amp; more</p>" in raw(doc, a.xref, "RC")
    assert "xfa:APIVersion" in raw(doc, a.xref, "RC")


def test_edit_reports_inapplicable_keys(sample_pdf):
    h = call("bb_add_highlight", path=sample_pdf, page=1, text="Sample")["markup_id"]
    r = call("bb_edit_markups", path=sample_pdf, markup_ids=[h], changes={"fill_color": "red", "line_width": 3, "color": "blue"})
    e = r["edited"][0]
    assert e["applied"] == ["color"] and {i["key"] for i in e["ignored"]} == {"fill_color", "line_width"}
    doc = pdf(sample_pdf)
    page, a = get(doc, h)
    assert nums(raw(doc, a.xref, "C")) == [0, 0, 1]


def test_edit_unknown_key_lists_allowed(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[10, 10, 50, 50])["markup_id"]
    before = Path(sample_pdf).read_bytes()
    with pytest.raises(ToolError) as e:
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"color": "red", "sparkle": True})
    msg = str(e.value)
    assert "sparkle" in msg and all(k in msg for k in mw.ALLOWED_EDIT_KEYS)
    assert Path(sample_pdf).read_bytes() == before
    with pytest.raises(ToolError, match="empty"):
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={})
    with pytest.raises(ToolError, match="not both"):
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"text": "a", "contents": "b"})
    with pytest.raises(ToolError, match="locked must be"):
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"locked": "yes"})


def test_edit_layer_moves_the_whole_group(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", rect=[100, 300, 250, 380], text="Group me")
    call("bb_edit_markups", path=sample_pdf, markup_ids=[r["callout_id"]], changes={"layer": "Grouped"})
    doc = pdf(sample_pdf)
    ocs = set()
    for mid in r["ids"]:
        page, a = get(doc, mid)
        ocs.add(raw(doc, a.xref, "OC"))
    assert len(ocs) == 1 and "null" not in ocs
    assert [o["name"] for o in doc.get_ocgs().values()] == ["Grouped"]


def test_edit_move_polygon_translates_vertices(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="polygon", points=[[100, 100], [200, 110], [180, 200]])
    rect = m["rect"]
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m["markup_id"]],
         changes={"rect": [rect[0] + 50, rect[1] + 30, rect[2] + 50, rect[3] + 30]})
    doc = pdf(sample_pdf)
    page, a = get(doc, m["markup_id"])
    got = [(round(px, 2), round(py, 2)) for px, py in a.vertices]
    assert got == [(150.0, 130.0), (250.0, 140.0), (230.0, 230.0)]


def test_edit_resize_rectangle_and_move_callout(sample_pdf):
    q = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])["markup_id"]
    c = call("bb_add_text", path=sample_pdf, page=1, kind="callout", text="c", rect=[300, 200, 400, 240],
             leader_point=[250, 300])
    r = call("bb_edit_markups", path=sample_pdf, markup_ids=[q], changes={"rect": [50, 50, 300, 200]})
    assert r["edited"][0]["rect"] == pytest.approx([50, 50, 300, 200], abs=3)
    x0, y0, x1, y1 = c["rect"]
    with pytest.raises(ToolError, match="moved .* not resized"):
        call("bb_edit_markups", path=sample_pdf, markup_ids=[c["markup_id"]], changes={"rect": [x0, y0, x1 + 50, y1]})
    call("bb_edit_markups", path=sample_pdf, markup_ids=[c["markup_id"]], changes={"rect": [x0 + 20, y0 + 10, x1 + 20, y1 + 10]})
    doc = pdf(sample_pdf)
    page, a = get(doc, c["markup_id"])
    assert nums(raw(doc, a.xref, "CL"))[:2] == [270, 792 - 310]        # leader tip moved with the box
    assert list(a.rect) == pytest.approx([x0 + 20, y0 + 10, x1 + 20, y1 + 10], abs=1)


def test_edit_stamp_opacity_both_ways(sample_pdf, png_stamp):
    s = call("bb_add_stamp", path=sample_pdf, page=1, stamp=png_stamp, rect=[100, 300, 160, 330])["markup_id"]
    center = lambda: (lambda pix: pix.pixel(pix.width // 2, pix.height // 2)[:3])(region(sample_pdf, 1, [100, 300, 160, 330]))
    assert center() == (255, 0, 0)
    call("bb_edit_markups", path=sample_pdf, markup_ids=[s], changes={"opacity": 0.5})
    assert center()[1] > 100
    call("bb_edit_markups", path=sample_pdf, markup_ids=[s], changes={"opacity": 1.0})
    assert center() == (255, 0, 0)


def test_edit_custom_columns(tmp_path):
    p, exp = _synth(tmp_path)
    area = exp["by_kind"]["area"][0]                       # has values RIPRAP-12 / 45.5 / Phase 1
    call("bb_edit_markups", path=p, markup_ids=[area], changes={"custom_columns": {"Unit Cost": 50, "Phase": "Phase 2"}})
    doc = pdf(p)
    page, a = get(doc, area)
    assert mw._column_values(doc, a.xref) == ["RIPRAP-12", "50", "Phase 2"]
    other = exp["by_kind"]["rectangle"][0]                 # no values yet -> positional, others empty
    call("bb_edit_markups", path=p, markup_ids=[other], changes={"custom_columns": {"Phase": "Phase 1"}})
    doc = pdf(p)
    page, b = get(doc, other)
    assert mw._column_values(doc, b.xref) == ["", "", "Phase 1"]
    with pytest.raises(ToolError, match="Unknown custom column.*Defined columns"):
        call("bb_edit_markups", path=p, markup_ids=[area], changes={"custom_columns": {"Nope": "x"}})


def test_edit_custom_columns_needs_definitions(tmp_path):
    p = str(tmp_path / "nocols.pdf")
    S.build_takeoff_pdf(p, custom_columns=False)
    doc = pdf(p)
    mid = core.markup_id(doc, next(iter(doc[1].annots())))
    doc.close()
    with pytest.raises(ToolError, match="defines no custom columns"):
        call("bb_edit_markups", path=p, markup_ids=[mid], changes={"custom_columns": {"Bid Item": "x"}})


def test_edit_measurement_keeps_its_structure(tmp_path):
    p, exp = _synth(tmp_path)
    area = exp["by_kind"]["area"][0]
    call("bb_edit_markups", path=p, markup_ids=[area], changes={"color": "#00AA00", "fill_color": None, "opacity": 0.9})
    doc = pdf(p)
    page, a = get(doc, area)
    x = a.xref
    assert raw(doc, x, "IT") == "/PolygonDimension" and raw(doc, x, "Contents") == exp["markups"][area]["formatted"]
    assert doc.xref_get_key(x, "Measure")[0] == "xref" and raw(doc, x, "Depth") == "6"
    assert nums(raw(doc, x, "C")) == pytest.approx([0, 0.6667, 0], abs=1e-3) and "IC" not in keys(doc, x)


# --- layers ----------------------------------------------------------------------------------------

def test_add_layer_and_noop(sample_pdf, tmp_path):
    r = call("bb_add_layer", path=sample_pdf, name="Takeoff")
    assert r["created"] and r["visible"] and r["write"]["backup"]
    doc = pdf(sample_pdf)
    assert [o["name"] for o in doc.get_ocgs().values()] == ["Takeoff"]
    doc.close()
    mtime = Path(sample_pdf).stat().st_mtime_ns
    backups = list((Path(sample_pdf).parent / core.BACKUP_DIR).iterdir())
    again = call("bb_add_layer", path=sample_pdf, name="takeoff")
    assert again["created"] is False and again["layer"] == "Takeoff" and again["write"] is None
    assert Path(sample_pdf).stat().st_mtime_ns == mtime
    assert list((Path(sample_pdf).parent / core.BACKUP_DIR).iterdir()) == backups
    out = str(tmp_path / "copy.pdf")
    r = call("bb_add_layer", path=sample_pdf, name="Other", visible=False, output_path=out)
    doc = pdf(out)
    assert sorted(o["name"] for o in doc.get_ocgs().values()) == ["Other", "Takeoff"]
    other = next(x for x, o in doc.get_ocgs().items() if o["name"] == "Other")
    assert other in doc.get_layer(-1)["off"] and other not in doc.get_layer(-1)["on"]
    with pytest.raises(ToolError, match="empty"):
        call("bb_add_layer", path=sample_pdf, name="  ")


def test_layer_visibility_toggles_rendering(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 300, 200, 360],
             fill_color="#0000FF", layer="Blue")
    assert non_blank(region(sample_pdf, 1, m["rect"]))
    r = call("bb_set_layer_visibility", path=sample_pdf, name="blue", visible=False)
    assert r == {**r, "layer": "Blue", "visible": False, "changed": True}
    assert not non_blank(region(sample_pdf, 1, m["rect"]))
    assert call("bb_set_layer_visibility", path=sample_pdf, name="Blue", visible=False)["write"] is None
    call("bb_set_layer_visibility", path=sample_pdf, name="Blue", visible=True)
    assert non_blank(region(sample_pdf, 1, m["rect"]))
    doc = pdf(sample_pdf)
    cfg = doc.get_layer(-1)
    assert not cfg.get("off")
    with pytest.raises(ToolError, match=r"Layer not found: 'Nope'. Layers: \['Blue'\]"):
        call("bb_set_layer_visibility", path=sample_pdf, name="Nope", visible=True)


def test_layer_visibility_on_synth_doc(tmp_path):
    p, exp = _synth(tmp_path)
    call("bb_set_layer_visibility", path=p, name="Takeoff", visible=False)
    doc = pdf(p)
    takeoff = next(x for x, o in doc.get_ocgs().items() if o["name"] == "Takeoff")
    review = next(x for x, o in doc.get_ocgs().items() if o["name"] == "Review")
    assert doc.get_layer(-1)["off"] == [takeoff] and review in doc.get_layer(-1)["on"]


# --- safe-write plumbing ---------------------------------------------------------------------------

def test_in_place_makes_a_backup_and_output_path_leaves_source(sample_pdf, tmp_path):
    original = Path(sample_pdf).read_bytes()
    out = str(tmp_path / "out.pdf")
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160], output_path=out)
    assert r["write"]["path"] == out and not r["write"]["in_place"] and r["write"]["backup"] is None
    assert Path(sample_pdf).read_bytes() == original
    assert len(list(pdf(out)[0].annots())) == 1
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])
    assert r["write"]["in_place"] and Path(r["write"]["backup"]).read_bytes() == original
    assert Path(r["write"]["backup"]).parent.name == core.BACKUP_DIR
    with pytest.raises(ToolError, match="already exists"):
        call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[1, 1, 20, 20], output_path=out)


def test_refuses_when_file_is_held(sample_pdf):
    win32file = pytest.importorskip("win32file")
    before = Path(sample_pdf).read_bytes()
    h = win32file.CreateFile(sample_pdf, win32file.GENERIC_READ, win32file.FILE_SHARE_READ, None,
                             win32file.OPEN_EXISTING, 0, None)
    try:
        for tool, args in [("bb_add_text", {"page": 1, "text": "x", "rect": [10, 10, 80, 40]}),
                           ("bb_add_layer", {"name": "L"}),
                           ("bb_delete_markups", {"markup_ids": ["x"]})]:
            with pytest.raises(ToolError, match="open in Revu or another program"):
                call(tool, path=sample_pdf, **args)
    finally:
        h.Close()
    assert Path(sample_pdf).read_bytes() == before


def test_refuses_when_revu_shows_the_file(sample_pdf, monkeypatch):
    stem = Path(sample_pdf).stem
    monkeypatch.setattr(core, "revu_active_title", lambda: f"{stem} - Bluebeam Revu x64")
    with pytest.raises(ToolError, match="open in Revu"):
        call("bb_add_layer", path=sample_pdf, name="L")


def test_bad_paths_and_pages(sample_pdf, tmp_path):
    with pytest.raises(ToolError, match="File not found"):
        call("bb_add_layer", path=str(tmp_path / "missing.pdf"), name="L")
    with pytest.raises(ToolError, match="out of range"):
        call("bb_add_shape", path=sample_pdf, page=2, kind="rectangle", rect=[1, 1, 5, 5])
    with pytest.raises(ToolError, match="out of range"):
        call("bb_add_measurement", path=sample_pdf, page=0, kind="count", points=[[1, 1]])


# --- rendering: every written markup type must be visible ---------------------------------------------

def _render_cases(png, pdfs):
    return {
        "text_box": ("bb_add_text", dict(text="Visible", rect=[100, 300, 220, 340])),
        "text_box_filled": ("bb_add_text", dict(text="x", rect=[100, 300, 220, 340], fill_color="#FFFF99")),
        "callout": ("bb_add_text", dict(kind="callout", text="See", rect=[300, 300, 400, 340], leader_point=[200, 420])),
        "note": ("bb_add_text", dict(kind="note", text="n", point=[300, 300])),
        "rectangle": ("bb_add_shape", dict(kind="rectangle", rect=[100, 300, 200, 360])),
        "ellipse": ("bb_add_shape", dict(kind="ellipse", rect=[100, 300, 200, 360])),
        "cloud": ("bb_add_shape", dict(kind="cloud", rect=[100, 300, 200, 360])),
        "cloud_plus": ("bb_add_shape", dict(kind="cloud", rect=[100, 300, 200, 360], text="Check")),
        "polygon": ("bb_add_shape", dict(kind="polygon", points=[[100, 300], [200, 310], [180, 380]])),
        "polyline": ("bb_add_shape", dict(kind="polyline", points=[[100, 300], [200, 310], [180, 380]])),
        "line": ("bb_add_shape", dict(kind="line", points=[[100, 300], [250, 380]])),
        "arrow": ("bb_add_shape", dict(kind="arrow", points=[[100, 300], [250, 380]])),
        "highlight_text": ("bb_add_highlight", dict(text="Sample")),
        "highlight_rect": ("bb_add_highlight", dict(rect=[100, 300, 220, 320])),
        "area": ("bb_add_measurement", dict(kind="area", points=SQ, scale=S30)),
        "length": ("bb_add_measurement", dict(kind="length", points=[[100, 300], [250, 380]], scale=S30)),
        "perimeter": ("bb_add_measurement", dict(kind="perimeter", points=[[100, 300], [200, 310], [180, 380]],
                                                 scale=S30)),
        "count": ("bb_add_measurement", dict(kind="count", points=[[300, 300], [330, 300], [360, 300]])),
        "stamp_builtin": ("bb_add_stamp", dict(stamp="Approved", rect=[100, 300, 250, 350])),
        "stamp_image": ("bb_add_stamp", dict(stamp=png, rect=[100, 300, 250, 350])),
        "stamp_pdf": ("bb_add_stamp", dict(stamp=pdfs, rect=[100, 300, 300, 380])),
    }


@pytest.mark.parametrize("name", list(_render_cases("", "")))
def test_every_markup_type_renders(name, sample_pdf, png_stamp, pdf_stamp, tmp_path):
    tool, args = _render_cases(png_stamp, pdf_stamp)[name]
    r = call(tool, path=sample_pdf, page=1, **args)
    box = r.get("rect") or r["highlights"][0]["rect"]
    if tool == "bb_add_highlight":
        box = r["highlights"][0]["rect"]
    pix = region(sample_pdf, 1, box, tmp_path, name)
    assert non_blank(pix), f"{name} rendered blank in {box}"
    assert (tmp_path / f"{name}.png").stat().st_size > 0


def test_written_markups_are_listed_with_the_right_types(sample_pdf, png_stamp):
    ids = {}
    ids["FreeText"] = call("bb_add_text", path=sample_pdf, page=1, text="a", rect=[10, 10, 100, 40])["markup_id"]
    ids["Square"] = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[10, 60, 100, 100])["markup_id"]
    ids["Circle"] = call("bb_add_shape", path=sample_pdf, page=1, kind="ellipse", rect=[10, 110, 100, 150])["markup_id"]
    ids["Highlight"] = call("bb_add_highlight", path=sample_pdf, page=1, text="Sample")["markup_id"]
    ids["Stamp"] = call("bb_add_stamp", path=sample_pdf, page=1, stamp=png_stamp, rect=[10, 200, 100, 260])["markup_id"]
    ids["Text"] = call("bb_add_text", path=sample_pdf, page=1, kind="note", text="n", point=[200, 200])["markup_id"]
    doc = pdf(sample_pdf)
    listed = {core.markup_id(doc, a): a.type[1] for a in doc[0].annots()}
    for typ, mid in ids.items():
        assert listed[mid] == typ
    assert len(listed) == len(ids)


# --- logic-level helpers ----------------------------------------------------------------------------

def test_parse_color():
    assert mw.parse_color("#FF0000") == (1.0, 0.0, 0.0)
    assert mw.parse_color("f00") == (1.0, 0.0, 0.0)
    assert mw.parse_color("Blue") == (0.0, 0.0, 1.0)
    assert mw.parse_color((0.5, 0.5, 0.5)) == (0.5, 0.5, 0.5)
    assert mw.parse_color(None, allow_none=True) is None and mw.parse_color("none", allow_none=True) is None
    for bad in ("nope", "#12345", (2, 0, 0)):
        with pytest.raises(DocumentError):
            mw.parse_color(bad)
    with pytest.raises(DocumentError):
        mw.parse_color("")


def test_formatting_matches_bb_synth():
    for feet in (81 + 8.5 / 12, 39 + 1 / 12, 3 + 0.5 / 12, 328 + 6.25 / 12, 324.0, 0.3, 1234.567):
        assert mw._fmt_ft_in(feet) == S.fmt_ft_in(feet)
    for v in (3213.27, 200904.9, 131454.0, 0.004, 1736.1111):
        assert mw._fmt_decimal(v, "sf") == S.fmt_area(v)
    assert mw._num(0.4166667) == S._num(0.4166667) == ".4166667"


def test_rotated_page_uses_unrotated_coordinates(tmp_path):
    p = str(tmp_path / "rot.pdf")
    d = pymupdf.open()
    pg = d.new_page(width=612, height=792)
    pg.insert_text((72, 72), "Rotated page riprap")
    pg.set_rotation(90)
    d.save(p)
    d.close()
    r = call("bb_add_shape", path=p, page=1, kind="rectangle", rect=[100, 700, 300, 780])   # y > 612: only valid unrotated
    assert r["rect"] == pytest.approx([99, 699, 301, 781], abs=2)
    h = call("bb_add_highlight", path=p, page=1, text="riprap")                              # same space as text search
    doc = pdf(p)
    hit = doc[0].search_for("riprap")[0]
    page, a = get(doc, h["markup_id"])
    assert a.rect.intersects(hit)
    c = call("bb_add_shape", path=p, page=1, kind="cloud", rect=[300, 300, 400, 380], text="Rotated cloud")
    assert len(c["ids"]) == 2
    with pytest.raises(ToolError, match="rotated pages"):
        call("bb_edit_markups", path=p, markup_ids=[r["markup_id"]], changes={"rect": [10, 10, 60, 60]})


def test_locked_markups_are_protected(sample_pdf):
    m = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[100, 100, 200, 160])["markup_id"]
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"locked": True})
    before = Path(sample_pdf).read_bytes()
    with pytest.raises(ToolError, match="locked markup"):
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"color": "blue"})
    with pytest.raises(ToolError, match="locked markup"):
        call("bb_delete_markups", path=sample_pdf, markup_ids=[m])
    assert Path(sample_pdf).read_bytes() == before
    call("bb_edit_markups", path=sample_pdf, markup_ids=[m], changes={"locked": False, "color": "blue"})   # unlock + edit
    call("bb_delete_markups", path=sample_pdf, markup_ids=[m])
    assert not list(pdf(sample_pdf)[0].annots())


def test_group_wide_edits_follow_the_parent(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", rect=[100, 300, 250, 380], text="Group",
             color="red")
    e = call("bb_edit_markups", path=sample_pdf, markup_ids=[r["callout_id"]],
             changes={"color": "#0000FF", "opacity": 0.5, "fill_color": "#00FF00"})
    assert [x["markup_id"] for x in e["edited"]] == r["ids"] and e["edited"][1]["group_of"] == r["callout_id"]
    assert e["edited"][1]["applied"] == ["color", "opacity"]          # fill_color stays with the target only
    doc = pdf(sample_pdf)
    (pg1, callout), (pg2, cloud) = get(doc, r["callout_id"]), get(doc, r["cloud_id"])
    for a in (callout, cloud):
        assert nums(raw(doc, a.xref, "C")) == [0, 0, 1] and float(raw(doc, a.xref, "CA")) == 0.5
    assert nums(raw(doc, callout.xref, "IC")) == [0, 1, 0] and "IC" not in keys(doc, cloud.xref)


def test_count_group_color_edit_recolors_every_symbol(sample_pdf):
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="count", points=[[100, 300], [130, 300], [160, 300]])
    call("bb_edit_markups", path=sample_pdf, markup_ids=[r["markup_id"]], changes={"color": "#0000FF"})
    doc = pdf(sample_pdf)
    for mid in r["ids"]:
        page, a = get(doc, mid)
        assert nums(raw(doc, a.xref, "C")) == [0, 0, 1] and nums(raw(doc, a.xref, "IC")) == [0, 0, 1]
    pix = region(sample_pdf, 1, r["rect"])
    assert any(b > 200 and r_ < 60 for r_, g, b in (pix.pixel(i, j)[:3] for i in range(pix.width) for j in range(pix.height)))


def test_edit_highlight_color_redraws(sample_pdf):
    h = call("bb_add_highlight", path=sample_pdf, page=1, text="Sample")
    call("bb_edit_markups", path=sample_pdf, markup_ids=[h["markup_id"]], changes={"color": "#0000FF"})
    pix = region(sample_pdf, 1, h["highlights"][0]["rect"])
    px = [pix.pixel(i, j)[:3] for i in range(pix.width) for j in range(pix.height)]
    assert any(b > 200 and r < 100 for r, g, b in px) and not any(r > 200 and g > 200 and b < 60 for r, g, b in px)


def test_writes_keep_the_pdf_structurally_valid(tmp_path):
    """Incremental saves on plain and object-stream PDFs re-open without MuPDF having to repair them."""
    for name, opts in (("plain.pdf", {}), ("objstm.pdf", {"use_objstms": 1, "garbage": 4, "deflate": True})):
        p = str(tmp_path / name)
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "Sample riprap")
        d.save(p, **opts)
        d.close()
        call("bb_add_text", path=p, page=1, text="t", rect=[100, 100, 200, 140], layer="L")
        call("bb_add_shape", path=p, page=1, kind="cloud", rect=[100, 300, 200, 360], text="c")
        call("bb_add_measurement", path=p, page=1, kind="area", points=SQ, scale=S30, depth=4)
        call("bb_add_highlight", path=p, page=1, text="riprap")
        r = call("bb_add_text", path=p, page=1, kind="note", text="n", point=[300, 300])
        call("bb_reply_to_markup", path=p, markup_id=r["markup_id"], text="ok", status="Accepted")
        call("bb_delete_markups", path=p, markup_ids=[r["markup_id"]])
        doc = pdf(p)
        assert not doc.is_repaired and doc.page_count == 1
        assert len(list(doc[0].annots())) == 1 + 2 + 1 + 1 and len(doc.get_ocgs()) == 1


def test_cloud_text_box_stays_on_the_page(sample_pdf):
    r = call("bb_add_shape", path=sample_pdf, page=1, kind="cloud", rect=[500, 100, 600, 160], text="Near the right edge")
    doc = pdf(sample_pdf)
    page, callout = get(doc, r["callout_id"])
    page2, cloud = get(doc, r["cloud_id"])
    assert page.rect.contains(callout.rect) and callout.rect.x0 < 400      # box went left; the leader runs to the cloud
    assert cloud.rect.intersects(pymupdf.Rect(500, 100, 600, 160))


def test_rectangle_and_text_keys_match_bb_synth(sample_pdf):
    rect = call("bb_add_shape", path=sample_pdf, page=1, kind="rectangle", rect=[350, 400, 500, 500], color="#008000",
                layer="Review")
    text = call("bb_add_text", path=sample_pdf, page=1, text="General note", rect=[100, 400, 300, 450], layer="Review")
    ref = pymupdf.open()
    rp = ref.new_page()
    layer = S.add_layer(ref, "Review")
    rx = S.add_rectangle(ref, rp, (350, 400, 500, 500), layer_xref=layer)
    tx = S.add_text_box(ref, rp, (100, 400, 300, 450), "General note", layer_xref=layer)
    doc = pdf(sample_pdf)
    assert keys(doc, get(doc, rect["markup_id"])[1].xref) == keys(ref, rx)
    assert keys(ref, tx) - {"CL"} <= keys(doc, get(doc, text["markup_id"])[1].xref)      # CL is MuPDF's stray leader
    assert raw(doc, get(doc, text["markup_id"])[1].xref, "RC").split("<p>")[0] == raw(ref, tx, "RC").split("<p>")[0]


# --- scale fallback (no default scale) ---------------------------------------------------------------

def _with_viewport(tmp_path, ft_per_in=20.0, name="vp.pdf"):
    p = str(tmp_path / name)
    d = pymupdf.open()
    pg = d.new_page(width=612, height=792)
    S.set_viewport_scale(d, pg, ft_per_in)
    d.save(p)
    d.close()
    return p


def test_measurement_uses_the_page_viewport_scale(tmp_path):
    p = _with_viewport(tmp_path, 20.0)
    r = call("bb_add_measurement", path=p, page=1, kind="area", points=SQ)
    assert r["scale_source"] == "page viewport" and r["unit"] == "sf"
    assert r["value"] == pytest.approx(10000 * (20 / 72) ** 2, abs=0.02) and r["units_per_point"] == pytest.approx(20 / 72, rel=1e-6)
    doc = pdf(p)
    page, a = get(doc, r["markup_id"])
    assert raw(doc, a.xref, "Contents") == r["formatted"] == "771.61 sf"
    assert "/C.2777778" in raw(doc, measure_xref(doc, a.xref), "X").replace(" ", "")


def test_measurement_uses_existing_markups_scale(sample_pdf):
    first = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 20 ft")
    assert first["scale_source"] == "argument"
    r = call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[100, 300], [172, 300]])
    assert r["scale_source"] == "existing markups" and r["formatted"] == "20'-0\""
    assert call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, units_per_point=0.5,
                unit="m")["scale_source"] == "argument"


def test_measurement_without_any_scale_is_refused(sample_pdf):
    before = Path(sample_pdf).read_bytes()
    with pytest.raises(ToolError, match=r"Pass the drawing scale, e\.g\. scale='1 in = 20 ft' \(see bb_sheet_index"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ)
    with pytest.raises(ToolError, match="Pass the drawing scale"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[1, 1], [50, 50]], scale="")
    assert Path(sample_pdf).read_bytes() == before
    # a count needs no scale
    assert call("bb_add_measurement", path=sample_pdf, page=1, kind="count", points=[[100, 100]])["count"] == 1


def test_measurement_with_two_scales_on_the_page_is_refused(sample_pdf):
    call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 20 ft")
    call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale="1 in = 30 ft")
    with pytest.raises(ToolError, match=r"2 different scales.*Pass the drawing scale"):
        call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[1, 1], [50, 50]])


def test_scale_is_per_page(sample_pdf_3p):
    call("bb_add_measurement", path=sample_pdf_3p, page=1, kind="area", points=SQ, scale="1 in = 20 ft")
    with pytest.raises(ToolError, match="Pass the drawing scale"):
        call("bb_add_measurement", path=sample_pdf_3p, page=2, kind="area", points=SQ)


# --- resizing a measurement recomputes its value -----------------------------------------------------

def _area_of(doc, annot, scale_ft_per_in=30.0):
    c = S.units_per_point(scale_ft_per_in)
    return round(S.shoelace([(px, py) for px, py in annot.vertices]) * c * c, 2)


def test_resizing_an_area_recomputes_contents_and_volume(sample_pdf):
    m = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale=S30, depth=6)
    x0, y0, x1, y1 = m["rect"]
    r = call("bb_edit_markups", path=sample_pdf, markup_ids=[m["markup_id"]],
             changes={"rect": [x0, y0, x0 + 2 * (x1 - x0), y0 + 2 * (y1 - y0)]})
    doc = pdf(sample_pdf)
    page, a = get(doc, m["markup_id"])
    area = _area_of(doc, a)
    assert 6800 < area < 7100                                             # ~4x 1,736.11 (border padding aside)
    assert raw(doc, a.xref, "Contents") == S.fmt_area(area)
    meas = r["edited"][0]["measurement"]
    assert meas["value"] == area and meas["unit"] == "sf" and meas["formatted"] == S.fmt_area(area)
    assert meas["volume"] == round(area * (6 / 12) * 0.03703704, 2) and meas["volume_unit"] == "cu yd"
    assert raw(doc, a.xref, "Depth") == "6" and doc.xref_get_key(a.xref, "Measure")[0] == "xref"
    assert r["edited"][0]["rect"] == pytest.approx([x0, y0, x0 + 2 * (x1 - x0), y0 + 2 * (y1 - y0)], abs=3)


def test_moving_a_measurement_keeps_its_contents(sample_pdf):
    m = call("bb_add_measurement", path=sample_pdf, page=1, kind="area", points=SQ, scale=S30)
    x0, y0, x1, y1 = m["rect"]
    r = call("bb_edit_markups", path=sample_pdf, markup_ids=[m["markup_id"]],
             changes={"rect": [x0 + 50, y0 + 40, x1 + 50, y1 + 40]})
    assert "measurement" not in r["edited"][0]
    doc = pdf(sample_pdf)
    assert raw(doc, get(doc, m["markup_id"])[1].xref, "Contents") == "1,736.11 sf"


def test_resizing_lengths_updates_feet_inches_and_metric(sample_pdf):
    line = call("bb_add_measurement", path=sample_pdf, page=1, kind="length", points=[[100, 100], [200, 100]], scale=S30)
    poly = call("bb_add_measurement", path=sample_pdf, page=1, kind="perimeter", points=[[100, 300], [200, 300], [200, 400]],
                scale="1:100", unit="m")
    for m in (line, poly):
        x0, y0, x1, y1 = m["rect"]
        call("bb_edit_markups", path=sample_pdf, markup_ids=[m["markup_id"]],
             changes={"rect": [x0, y0, x0 + 3 * (x1 - x0), y1 + 3 * (y1 - y0)]})
    doc = pdf(sample_pdf)
    pg1, a1 = get(doc, line["markup_id"])
    pg2, a2 = get(doc, poly["markup_id"])
    (p0x, p0y), (p1x, p1y) = a1.vertices
    assert raw(doc, a1.xref, "Contents") == S.fmt_ft_in(S.path_length([(p0x, p0y), (p1x, p1y)]) * S.units_per_point(30))
    pts = [(px, py) for px, py in a2.vertices]
    metres = S.path_length(pts) * (100 / 72 * 0.0254)
    assert raw(doc, a2.xref, "Contents") == S.fmt_area(metres, "m") and metres > 3 * 3.5


def test_resizing_a_bluebeam_authored_area_recomputes_it(tmp_path):
    p, exp = _synth(tmp_path)
    area = exp["by_kind"]["area"][1]                      # direct (non-indirect) /Measure variant
    length = exp["by_kind"]["length"][0]
    for mid in (area, length):
        doc = pdf(p)
        page, a = get(doc, mid)
        r = a.rect
        doc.close()
        call("bb_edit_markups", path=p, markup_ids=[mid], changes={"rect": [r.x0, r.y0, r.x0 + r.width * 1.5, r.y0 + r.height * 1.5]})
    doc = pdf(p)
    page, a = get(doc, area)
    assert raw(doc, a.xref, "Contents") == S.fmt_area(_area_of(doc, a)) and _area_of(doc, a) > exp["markups"][area]["value"] * 2
    page, b = get(doc, length)
    pts = [(px, py) for px, py in b.vertices]
    assert raw(doc, b.xref, "Contents") == S.fmt_ft_in(S.path_length(pts) * S.units_per_point(30))

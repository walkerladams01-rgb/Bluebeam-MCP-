import asyncio
import json
import os
from pathlib import Path

import pymupdf
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from bluebeam import core, pages
from bluebeam.core import DocumentError, FileLocked
from bluebeam.tools_pages import register_page_tools


# --- helpers ---------------------------------------------------------------------------

def make_annotated(path, n=3, prefix="P"):
    """n pages; page i has text '<prefix> page i', a red-filled square and a FreeText 'note <prefix>i'."""
    doc = pymupdf.open()
    for i in range(1, n + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"{prefix} page {i}")
        sq = page.add_rect_annot(pymupdf.Rect(100, 200, 160, 260))
        sq.set_colors(stroke=(1, 0, 0), fill=(1, 0, 0))
        sq.update()
        page.add_freetext_annot(pymupdf.Rect(100, 300, 300, 340), f"note {prefix}{i}", fontsize=12)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def annotated(tmp_path):
    return make_annotated(tmp_path / "annotated.pdf", 3)


def annot_count(path, pno):
    with core.open_readonly(path) as d:
        return len(list(d[pno].annots()))


def texts(path):
    with core.open_readonly(path) as d:
        return [p.get_text() for p in d]


def raw(path):
    return Path(path).read_bytes()


def red_at_markup(path, pno):
    """True if the square's red fill is part of the page CONTENT (annotations not drawn)."""
    with core.open_readonly(path) as d:
        pix = d[pno].get_pixmap(dpi=72, annots=False)
        return pix.pixel(130, 230) == (255, 0, 0)


@pytest.fixture
def mcp_pages():
    mcp = FastMCP("t")
    register_page_tools(mcp)
    return mcp


def call(mcp, name, **args):
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


# --- document_info ---------------------------------------------------------------------

def test_document_info(tmp_path):
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    p2 = doc.new_page(width=1224, height=792)  # 17 x 11 in landscape
    p2.add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    p2.add_freetext_annot(pymupdf.Rect(60, 60, 160, 90), "hi")
    p2.add_rect_annot(pymupdf.Rect(70, 10, 90, 50))
    doc.new_page(width=612, height=792).set_rotation(90)
    doc.set_metadata({"title": "Set A", "author": "J. Smith", "producer": "Bluebeam Revu x64",
                      "creationDate": "D:20240131101500+01'00'"})
    doc.set_page_labels([{"startpage": 0, "prefix": "C-10", "style": "D", "firstpagenum": 1}])
    doc.add_ocg("Layer 1", on=True)
    path = str(tmp_path / "info.pdf")
    doc.save(path)
    doc.close()

    info = pages.document_info(path)
    assert info["page_count"] == 3 and info["file_size_bytes"] == os.path.getsize(path)
    assert info["file_size_mb"] == round(os.path.getsize(path) / 1_048_576, 2)
    assert info["metadata"]["title"] == "Set A" and info["metadata"]["author"] == "J. Smith"
    assert info["metadata"]["created"] == "2024-01-31T10:15:00"
    assert info["is_bluebeam"] and not info["encrypted"]
    assert info["has_layers"] and info["has_page_labels"]
    assert info["annotation_counts"] == {"Square": 2, "FreeText": 1}
    p1, p2i, p3 = info["pages"]
    assert (p1["number"], p1["label"], p1["width_in"], p1["height_in"]) == (1, "C-101", 8.5, 11.0)
    assert p1["orientation"] == "portrait" and p1["rotation"] == 0 and p1["annotations"] == {}
    assert (p2i["width_in"], p2i["height_in"], p2i["orientation"]) == (17.0, 11.0, "landscape")
    assert p2i["annotations"] == {"Square": 2, "FreeText": 1}
    # a page rotated by 90 is reported as displayed: landscape
    assert p3["rotation"] == 90 and p3["orientation"] == "landscape" and p3["width_in"] == 11.0
    assert "pages_note" not in info


def test_document_info_plain_pdf(sample_pdf):
    info = pages.document_info(sample_pdf)
    assert not info["is_bluebeam"] and not info["has_layers"] and not info["has_page_labels"]
    assert info["pages"][0]["label"] == "" and info["annotation_counts"] == {}


def test_document_info_caps_page_list(tmp_path):
    doc = pymupdf.open()
    for i in range(205):
        page = doc.new_page(width=200, height=200)
        if i == 204:
            page.add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    path = str(tmp_path / "big.pdf")
    doc.save(path)
    doc.close()
    info = pages.document_info(path)
    assert info["page_count"] == 205 and len(info["pages"]) == pages.MAX_INFO_PAGES
    assert "205" in info["pages_note"]
    assert info["annotation_counts"] == {"Square": 1}  # totals cover pages beyond the cap


def test_document_info_errors(tmp_path):
    with pytest.raises(DocumentError):
        pages.document_info(str(tmp_path / "missing.pdf"))
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(DocumentError):
        pages.document_info(str(bad))


# --- rotate ----------------------------------------------------------------------------

def test_rotate_in_place_backs_up_and_keeps_annotations(annotated):
    before = raw(annotated)
    res = pages.rotate_pages(annotated, "1,3", 90)
    assert res["pages_rotated"] == "1,3" and res["write"]["in_place"]
    assert raw(res["write"]["backup"]) == before
    assert os.path.dirname(res["write"]["backup"]).endswith(core.BACKUP_DIR)
    with core.open_readonly(annotated) as d:
        assert [p.rotation for p in d] == [90, 0, 90]
    assert [annot_count(annotated, i) for i in range(3)] == [2, 2, 2]


def test_rotate_is_relative_and_accepts_negative(sample_pdf, tmp_path):
    out1, out2 = str(tmp_path / "r1.pdf"), str(tmp_path / "r2.pdf")
    pages.rotate_pages(sample_pdf, "all", 90, out1)
    pages.rotate_pages(out1, 1, 180, out2)
    with core.open_readonly(out2) as d:
        assert d[0].rotation == 270
    out3 = str(tmp_path / "r3.pdf")
    pages.rotate_pages(sample_pdf, 1, -90, out3)
    with core.open_readonly(out3) as d:
        assert d[0].rotation == 270


def test_rotate_output_path_leaves_source_identical(annotated, tmp_path):
    before = raw(annotated)
    out = str(tmp_path / "rot.pdf")
    res = pages.rotate_pages(annotated, 2, 90, out)
    assert raw(annotated) == before
    assert res["write"]["path"] == out and not res["write"]["in_place"] and res["write"]["backup"] is None
    assert not (tmp_path / core.BACKUP_DIR).exists()
    with core.open_readonly(out) as d:
        assert [p.rotation for p in d] == [0, 90, 0]


@pytest.mark.parametrize("degrees", [0, 45, 360, 100])
def test_rotate_bad_degrees(annotated, degrees):
    with pytest.raises(DocumentError):
        pages.rotate_pages(annotated, "all", degrees)


@pytest.mark.parametrize("spec", ["9", "0", "3-1", "x"])
def test_rotate_bad_page_range_writes_nothing(annotated, spec):
    before = raw(annotated)
    with pytest.raises(DocumentError):
        pages.rotate_pages(annotated, spec, 90)
    assert raw(annotated) == before and not (Path(annotated).parent / core.BACKUP_DIR).exists()


def test_in_place_refused_when_open_in_revu(annotated, monkeypatch):
    stem = Path(annotated).stem
    monkeypatch.setattr(core, "revu_active_title", lambda: f"{stem} - Bluebeam Revu x64")
    with pytest.raises(FileLocked):
        pages.rotate_pages(annotated, "all", 90)
    # output_path is the way around it, and works
    out = str(Path(annotated).with_name("copy.pdf"))
    pages.rotate_pages(annotated, "all", 90, out)
    assert Path(out).exists()


# --- delete ----------------------------------------------------------------------------

def test_delete_pages_in_place(annotated):
    before = raw(annotated)
    res = pages.delete_pages(annotated, "2", None)
    assert (res["pages_before"], res["pages_after"], res["pages_deleted"]) == (3, 2, "2")
    assert raw(res["write"]["backup"]) == before and not res["write"]["incremental"]
    t = texts(annotated)
    assert "P page 1" in t[0] and "P page 3" in t[1] and "note P2" not in "".join(t)
    assert [annot_count(annotated, i) for i in range(2)] == [2, 2]
    assert not [f for f in os.listdir(Path(annotated).parent) if "bbtmp" in f]


def test_delete_pages_to_output_and_refuse_all(annotated, tmp_path):
    before = raw(annotated)
    out = str(tmp_path / "less.pdf")
    res = pages.delete_pages(annotated, "1,3", out)
    assert res["pages_after"] == 1 and raw(annotated) == before
    with core.open_readonly(out) as d:
        assert "P page 2" in d[0].get_text()
    for spec in ("all", "1-3", None):
        with pytest.raises(DocumentError, match="every page"):
            pages.delete_pages(annotated, spec)
    assert raw(annotated) == before


def test_delete_pages_bad_range(annotated):
    with pytest.raises(DocumentError):
        pages.delete_pages(annotated, "7")


# --- extract ---------------------------------------------------------------------------

def test_extract_pages_new_file_keeps_annotations_and_metadata(annotated, tmp_path):
    doc = pymupdf.open(annotated)
    doc.set_metadata({"title": "Whole set"})
    doc.saveIncr()
    doc.close()
    before = raw(annotated)
    out = str(tmp_path / "part.pdf")
    res = pages.extract_pages(annotated, "1,3", out)
    assert raw(annotated) == before
    assert res["pages_extracted"] == "1,3" and res["pages_after"] == 2 and not res["write"]["in_place"]
    t = texts(out)
    assert "P page 1" in t[0] and "P page 3" in t[1] and "P page 2" not in "".join(t)
    assert [annot_count(out, i) for i in range(2)] == [2, 2]
    with core.open_readonly(out) as d:
        assert d.metadata["title"] == "Whole set"
    assert not (tmp_path / core.BACKUP_DIR).exists()


def test_extract_pages_needs_a_different_new_output(annotated, tmp_path):
    with pytest.raises(DocumentError, match="output_path is required"):
        pages.extract_pages(annotated, "1", "")
    with pytest.raises(DocumentError, match="different file"):
        pages.extract_pages(annotated, "1", annotated)
    out = tmp_path / "exists.pdf"
    out.write_bytes(b"keep me")
    with pytest.raises(DocumentError, match="already exists"):
        pages.extract_pages(annotated, "1", str(out))
    assert out.read_bytes() == b"keep me"
    with pytest.raises(DocumentError, match="folder does not exist"):
        pages.extract_pages(annotated, "1", str(tmp_path / "nope" / "x.pdf"))
    with pytest.raises(DocumentError):
        pages.extract_pages(annotated, "5", str(tmp_path / "x.pdf"))
    assert not (tmp_path / "x.pdf").exists()


def test_extract_drops_other_pages_content(tmp_path):
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), "SECRET-PAGE-TWO " * 50 if i == 1 else f"page {i}")
    src = str(tmp_path / "s.pdf")
    doc.save(src)
    doc.close()
    out = str(tmp_path / "e.pdf")
    pages.extract_pages(src, "1", out)
    with core.open_readonly(out) as d:
        streams = b"".join(d.xref_stream(x) or b"" for x in range(1, d.xref_length())
                           if d.xref_is_stream(x))
    assert b"SECRET" not in streams


# --- merge -----------------------------------------------------------------------------

def test_merge_keeps_annotations_and_bookmarks(tmp_path):
    a = make_annotated(tmp_path / "a.pdf", 2, "A")
    b = make_annotated(tmp_path / "b.pdf", 3, "B")
    c = make_annotated(tmp_path / "c.pdf", 1, "C")
    for path, toc in ((a, [[1, "A start", 1]]), (b, [[1, "B start", 1], [2, "B two", 2]])):
        d = pymupdf.open(path)
        d.set_toc(toc)
        d.saveIncr()
        d.close()
    before = [raw(p) for p in (a, b, c)]
    out = str(tmp_path / "merged.pdf")
    res = pages.merge_pdfs([a, b, c], out)
    assert [raw(p) for p in (a, b, c)] == before
    assert res["pages_after"] == 6 and [s["pages"] for s in res["sources"]] == [2, 3, 1]
    t = texts(out)
    assert [s.split("\n")[0] for s in t] == ["A page 1", "A page 2", "B page 1", "B page 2", "B page 3", "C page 1"]
    assert [annot_count(out, i) for i in range(6)] == [2] * 6
    with core.open_readonly(out) as d:
        assert d.get_toc() == [[1, "A start", 1], [1, "B start", 3], [2, "B two", 4]]
    assert not (tmp_path / core.BACKUP_DIR).exists()


def test_merge_errors(tmp_path):
    a = make_annotated(tmp_path / "a.pdf", 1, "A")
    b = make_annotated(tmp_path / "b.pdf", 1, "B")
    with pytest.raises(DocumentError, match="at least two"):
        pages.merge_pdfs([a], str(tmp_path / "o.pdf"))
    with pytest.raises(DocumentError, match="one of the input"):
        pages.merge_pdfs([a, b], b)
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"x")
    with pytest.raises(DocumentError, match="already exists"):
        pages.merge_pdfs([a, b], str(existing))
    with pytest.raises(DocumentError, match="File not found"):
        pages.merge_pdfs([a, str(tmp_path / "missing.pdf")], str(tmp_path / "o.pdf"))
    assert not (tmp_path / "o.pdf").exists()


# --- split -----------------------------------------------------------------------------

def test_split_one_file_per_page(annotated, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    before = raw(annotated)
    res = pages.split_pdf(annotated, str(out_dir))
    assert raw(annotated) == before and res["source_pages"] == 3
    names = sorted(os.listdir(out_dir))
    assert names == ["annotated p1.pdf", "annotated p2.pdf", "annotated p3.pdf"]
    assert [f["pages"] for f in res["files"]] == ["1", "2", "3"]
    assert "P page 2" in texts(str(out_dir / "annotated p2.pdf"))[0]
    assert annot_count(str(out_dir / "annotated p3.pdf"), 0) == 2


def test_split_by_ranges(tmp_path):
    src = make_annotated(tmp_path / "set.pdf", 6)
    out_dir = tmp_path / "parts"
    out_dir.mkdir()
    res = pages.split_pdf(src, str(out_dir), ["1-3", "4-", "2"])  # ranges may overlap when names differ
    assert sorted(os.listdir(out_dir)) == ["set p1-3.pdf", "set p2.pdf", "set p4-6.pdf"]
    assert [f["page_count"] for f in res["files"]] == [3, 3, 1]
    assert "P page 5" in texts(str(out_dir / "set p4-6.pdf"))[1]
    # a single string is accepted too
    res = pages.split_pdf(src, str(out_dir), "1,3-4")
    assert res["files"][0]["path"].endswith("set p1,3-4.pdf")


def test_split_refuses_to_overwrite_and_writes_nothing(annotated, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "annotated p2.pdf").write_bytes(b"precious")
    with pytest.raises(DocumentError, match="overwrite"):
        pages.split_pdf(annotated, str(out_dir))
    assert sorted(os.listdir(out_dir)) == ["annotated p2.pdf"]
    assert (out_dir / "annotated p2.pdf").read_bytes() == b"precious"


def test_split_errors(annotated, tmp_path):
    with pytest.raises(DocumentError, match="folder does not exist"):
        pages.split_pdf(annotated, str(tmp_path / "nope"))
    out_dir = tmp_path / "o"
    out_dir.mkdir()
    with pytest.raises(DocumentError, match="same output"):
        pages.split_pdf(annotated, str(out_dir), ["1-2", "1,2"])
    with pytest.raises(DocumentError):
        pages.split_pdf(annotated, str(out_dir), ["1-9"])
    assert os.listdir(out_dir) == []


# --- insert ----------------------------------------------------------------------------

def test_insert_pages_in_place_middle_and_annotations(tmp_path):
    dst = make_annotated(tmp_path / "dst.pdf", 3, "D")
    src = make_annotated(tmp_path / "src.pdf", 4, "S")
    src_before, dst_before = raw(src), raw(dst)
    res = pages.insert_pages(dst, src, 2, "1,3-4")
    assert raw(src) == src_before
    assert raw(res["write"]["backup"]) == dst_before
    assert (res["pages_before"], res["pages_after"], res["pages_inserted"]) == (3, 6, 3)
    order = [t.split("\n")[0] for t in texts(dst)]
    assert order == ["D page 1", "S page 1", "S page 3", "S page 4", "D page 2", "D page 3"]
    assert [annot_count(dst, i) for i in range(6)] == [2] * 6


def test_insert_pages_append_to_new_file(tmp_path):
    dst = make_annotated(tmp_path / "dst.pdf", 2, "D")
    src = make_annotated(tmp_path / "src.pdf", 2, "S")
    before = raw(dst)
    out = str(tmp_path / "out.pdf")
    res = pages.insert_pages(dst, src, 3, None, out)
    assert raw(dst) == before and res["pages_after"] == 4
    assert [t.split("\n")[0] for t in texts(out)] == ["D page 1", "D page 2", "S page 1", "S page 2"]
    # inserting at the start, from the same file
    res = pages.insert_pages(dst, dst, 1, 2, str(tmp_path / "out2.pdf"))
    assert [t.split("\n")[0] for t in texts(str(tmp_path / "out2.pdf"))] == ["D page 2", "D page 1", "D page 2"]


@pytest.mark.parametrize("at_page", [0, 4, -1])
def test_insert_pages_bad_position(tmp_path, at_page):
    dst = make_annotated(tmp_path / "dst.pdf", 2, "D")
    src = make_annotated(tmp_path / "src.pdf", 1, "S")
    before = raw(dst)
    with pytest.raises(DocumentError, match="at_page"):
        pages.insert_pages(dst, src, at_page)
    assert raw(dst) == before


# --- flatten ---------------------------------------------------------------------------

def test_flatten_default_output_bakes_markups_into_content(annotated):
    before = raw(annotated)
    assert not red_at_markup(annotated, 0)  # a live markup is not page content
    res = pages.flatten(annotated)
    expected = str(Path(annotated).with_name("annotated (flattened).pdf"))
    assert res["write"]["path"] == expected and not res["write"]["in_place"]
    assert raw(annotated) == before
    assert res["markups_flattened"] == 6 and res["markups_remaining"] == 0
    assert res["pages_flattened"] == "1-3"
    for i in range(3):
        assert annot_count(expected, i) == 0
        assert red_at_markup(expected, i)
        assert f"note P{i + 1}" in texts(expected)[i]  # the FreeText's words are page content now
        assert f"P page {i + 1}" in texts(expected)[i]
    with pytest.raises(DocumentError, match="already exists"):
        pages.flatten(annotated)


def test_flatten_in_place_needs_explicit_flag(annotated, tmp_path):
    before = raw(annotated)
    res = pages.flatten(annotated, in_place=True)
    assert res["write"]["in_place"] and raw(res["write"]["backup"]) == before
    assert [annot_count(annotated, i) for i in range(3)] == [0, 0, 0] and red_at_markup(annotated, 1)
    with pytest.raises(DocumentError, match="Nothing to flatten"):
        pages.flatten(annotated, in_place=True)
    assert not (Path(annotated).parent / "annotated (flattened).pdf").exists()


def test_flatten_argument_conflicts(annotated, tmp_path):
    with pytest.raises(DocumentError, match="not both"):
        pages.flatten(annotated, str(tmp_path / "o.pdf"), in_place=True)
    with pytest.raises(DocumentError, match="in_place=True"):
        pages.flatten(annotated, annotated)
    out = tmp_path / "given.pdf"
    res = pages.flatten(annotated, str(out))
    assert res["write"]["path"] == str(out) and out.exists()


def test_flatten_only_selected_pages(tmp_path):
    src = make_annotated(tmp_path / "four.pdf", 4)
    # page 3 keeps its markups in an INDIRECT /Annots array, as many real-world files do
    d = pymupdf.open(src)
    xref = d[2].xref
    kind, arr = d.xref_get_key(xref, "Annots")
    assert kind == "array"
    new = d.get_new_xref()
    d.update_object(new, arr)
    d.xref_set_key(xref, "Annots", f"{new} 0 R")
    d.save(src, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    d.close()
    with core.open_readonly(src) as d:
        untouched = {i: d[i].read_contents() for i in (0, 2, 3)}
        assert d.xref_get_key(d[2].xref, "Annots")[0] == "xref"

    out = str(tmp_path / "part flat.pdf")
    res = pages.flatten(src, out, pages="2")
    assert res["pages_flattened"] == "2" and res["markups_flattened"] == 2
    assert [annot_count(out, i) for i in range(4)] == [2, 0, 2, 2]
    assert red_at_markup(out, 1)
    for i in (0, 2, 3):  # other pages: still live markups, not baked, content untouched
        assert not red_at_markup(out, i)
    with core.open_readonly(out) as d:
        assert {i: d[i].read_contents() for i in (0, 2, 3)} == untouched
        assert "note P2" in d[1].get_text()


def test_flatten_keeps_links(tmp_path):
    src = make_annotated(tmp_path / "l.pdf", 2)
    d = pymupdf.open(src)
    d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(0, 0, 40, 20), "uri": "https://example.com"})
    d.saveIncr()
    d.close()
    out = str(tmp_path / "l flat.pdf")
    pages.flatten(src, out)
    with core.open_readonly(out) as d:
        assert [lk["uri"] for lk in d[0].get_links()] == ["https://example.com"]


def test_flatten_nothing_to_flatten(sample_pdf, tmp_path):
    with pytest.raises(DocumentError, match="Nothing to flatten"):
        pages.flatten(sample_pdf)
    assert not (tmp_path / "sample (flattened).pdf").exists()
    with pytest.raises(DocumentError):
        pages.flatten(sample_pdf, pages="4")


# --- the registered MCP tools, end to end -----------------------------------------------

def test_tool_registration_and_annotations(mcp_pages):
    tools = {t.name: t for t in asyncio.run(mcp_pages.list_tools())}
    assert set(tools) == {"bb_document_info", "bb_rotate_pages", "bb_delete_pages", "bb_extract_pages",
                          "bb_merge_pdfs", "bb_split_pdf", "bb_insert_pages", "bb_flatten"}
    assert tools["bb_document_info"].annotations.readOnlyHint is True
    for name, tool in tools.items():
        assert tool.annotations.openWorldHint is False
        if name != "bb_document_info":
            assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True
        assert tool.description and len(tool.description) > 40


def test_tool_document_info_and_rotate(mcp_pages, annotated, tmp_path):
    info = call(mcp_pages, "bb_document_info", path=annotated)
    assert info["page_count"] == 3 and info["annotation_counts"] == {"Square": 3, "FreeText": 3}
    out = str(tmp_path / "rot.pdf")
    res = call(mcp_pages, "bb_rotate_pages", path=annotated, pages="2-3", degrees=270, output_path=out)
    assert res["pages_rotated"] == "2-3" and res["write"]["path"] == out
    res = call(mcp_pages, "bb_rotate_pages", path=annotated, pages=1, degrees=-90)  # page as an int, in place
    assert res["pages_rotated"] == "1" and res["write"]["backup"]
    with core.open_readonly(annotated) as d:
        assert [p.rotation for p in d] == [270, 0, 0]


def test_tool_page_operations(mcp_pages, tmp_path):
    a = make_annotated(tmp_path / "a.pdf", 3, "A")
    b = make_annotated(tmp_path / "b.pdf", 2, "B")
    merged = str(tmp_path / "ab.pdf")
    assert call(mcp_pages, "bb_merge_pdfs", paths=[a, b], output_path=merged)["pages_after"] == 5
    part = str(tmp_path / "part.pdf")
    assert call(mcp_pages, "bb_extract_pages", path=merged, pages="4-", output_path=part)["pages_after"] == 2
    res = call(mcp_pages, "bb_insert_pages", path=a, source_path=part, at_page=4, output_path=str(tmp_path / "ins.pdf"))
    assert res["pages_after"] == 5
    res = call(mcp_pages, "bb_delete_pages", path=merged, pages="1-2")
    assert res["pages_after"] == 3 and res["write"]["backup"]
    out_dir = tmp_path / "split"
    out_dir.mkdir()
    res = call(mcp_pages, "bb_split_pdf", path=merged, output_dir=str(out_dir), ranges=["1-2", "3"])
    assert [f["pages"] for f in res["files"]] == ["1-2", "3"]
    res = call(mcp_pages, "bb_flatten", path=merged, pages="1")
    assert res["pages_flattened"] == "1" and res["markups_flattened"] == 2
    assert Path(res["write"]["path"]).name == "ab (flattened).pdf"


def test_tools_raise_tool_errors(mcp_pages, annotated, tmp_path):
    before = raw(annotated)
    with pytest.raises(ToolError, match="every page"):
        call(mcp_pages, "bb_delete_pages", path=annotated, pages="all")
    with pytest.raises(ToolError, match="out of range"):
        call(mcp_pages, "bb_rotate_pages", path=annotated, pages="12", degrees=90)
    with pytest.raises(ToolError, match="File not found"):
        call(mcp_pages, "bb_document_info", path=str(tmp_path / "nope.pdf"))
    with pytest.raises(ToolError, match="different file"):
        call(mcp_pages, "bb_extract_pages", path=annotated, pages="1", output_path=annotated)
    assert raw(annotated) == before

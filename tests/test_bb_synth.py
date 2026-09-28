"""Sanity tests for the synthetic Bluebeam-style fixture builder (tests/bb_synth.py)."""
import re

import pymupdf
import pytest

from bluebeam.core import decode_label
from tests import bb_synth as S


def test_formatters_match_real_bluebeam_strings():
    # strings copied from the structure survey (values are formatting examples, not drawing data)
    assert S.fmt_ft_in(81 + 8.5 / 12) == "81'-8 1/2\""
    assert S.fmt_ft_in(39 + 1 / 12) == "39'-1\""
    assert S.fmt_ft_in(3 + 0.5 / 12) == "3'-1/2\""
    assert S.fmt_ft_in(328 + 6.25 / 12) == "328'-6 1/4\""
    assert S.fmt_ft_in(324.0) == "324'-0\""
    assert S.fmt_area(3213.27) == "3,213.27 sf"
    assert S.fmt_area(200904.9) == "200,904.9 sf"
    assert S.fmt_area(131454.0) == "131,454 sf"
    assert S.units_per_point(30) == 0.4166667
    assert S.units_per_point(20) == 0.2777778
    assert S.units_per_point(100) == 1.388889
    assert S._num(0.4166667) == ".4166667"


def _resolve_measure(doc, xref):
    kind, val = doc.xref_get_key(xref, "Measure")
    if kind == "xref":
        mx = int(val.split()[0])
        return True, doc.xref_object(mx, compressed=True)
    assert kind == "dict", (kind, val)
    return False, val


def _x_factor(measure_src):
    m = re.search(r"/X\[<<[^>]*?/C\s*([-\d.]+)", measure_src)
    return float(m.group(1))


def _index(doc):
    """nm -> (page, annot); keeps every page referenced so annots stay bound."""
    pages = [doc[i] for i in range(doc.page_count)]
    out = {}
    for pg in pages:
        for a in pg.annots():
            out[doc.xref_get_key(a.xref, "NM")[1]] = (pg, a)
    return out, pages


@pytest.mark.parametrize("indirect", [True, False])
def test_build_reopen_and_verify(tmp_path, indirect):
    path = str(tmp_path / "synth.pdf")
    exp = S.build_takeoff_pdf(path, indirect_measure=indirect)
    doc = pymupdf.open(path)
    try:
        assert doc.page_count == exp["page_count"] == 3
        for pno, text in exp["page_labels"].items():
            assert decode_label(doc[pno - 1].get_label()) == text
        ocg_names = {x: info["name"] for x, info in doc.get_ocgs().items()}
        assert sorted(ocg_names.values()) == sorted(exp["layers"])
        assert doc.xref_get_key(doc[1].xref, "VP")[0] == "array"
        assert doc.xref_get_key(doc[0].xref, "VP")[0] == "null"
        ck, cv = doc.xref_get_key(doc.pdf_catalog(), "BSIColumnData")
        assert ck == "xref"
        defs = doc.xref_object(int(cv.split()[0]), compressed=True)
        assert [m for m in re.findall(r"/Name\(([^)]*)\)", defs)] == exp["custom_column_defs"]

        idx, _pages = _index(doc)
        assert set(idx) == set(exp["markups"]), "every expected /NM is present exactly once"
        xref_to_nm = {a.xref: nm for nm, (_, a) in idx.items()}
        for nm, m in exp["markups"].items():
            pg, a = idx[nm]
            assert pg.number + 1 == m["page"]
            assert a.xref == m["xref"]
            assert a.type[1] == m["pdf_type"]
            it = doc.xref_get_key(a.xref, "IT")[1]
            assert it == ("/" + m["intent"] if m["intent"] else "null")
            if m["subject"] is not None:
                assert a.info["subject"] == m["subject"]
            assert a.info["title"] == m["author"]
            if m["layer"]:
                assert ocg_names[a.get_oc()] == m["layer"]
            if m["kind"] in ("area", "length"):
                assert a.info["content"] == m["formatted"]
                is_ind, src = _resolve_measure(doc, a.xref)
                assert is_ind == m["measure_indirect"]
                c = _x_factor(src)
                assert c == exp["scale"]["units_per_point"]
                assert "/R(1 in = 30 ft' in\")" in src
                mt = int(doc.xref_get_key(a.xref, "MeasurementTypes")[1])
                verts = a.vertices
                assert len(verts) == m["vertex_count"]
                if m["kind"] == "area":
                    assert mt == 129
                    assert abs(S.shoelace(verts) * c * c - m["value"]) < 0.005
                    if m["depth"] is not None:
                        assert float(doc.xref_get_key(a.xref, "Depth")[1]) == m["depth"]
                        assert "/U(in)" in doc.xref_get_key(a.xref, "DepthUnit")[1]
                        assert abs(m["value"] * m["depth"] / 12 * 0.03703704 - m["volume"]) < 0.01
                    else:
                        assert doc.xref_get_key(a.xref, "Depth")[0] == "null"
                else:
                    assert mt == 130
                    assert abs(S.path_length(verts) * c - m["value"]) < 0.001
                if m["custom_columns"]:
                    vals = re.findall(r"\(([^)]*)\)", doc.xref_get_key(a.xref, "BSIColumnData")[1])
                    assert dict(zip(exp["custom_column_defs"], vals)) == m["custom_columns"]
                else:
                    assert doc.xref_get_key(a.xref, "BSIColumnData")[0] == "null"
            if m["kind"] == "count":
                assert int(doc.xref_get_key(a.xref, "MeasurementTypes")[1]) == 128
                assert int(doc.xref_get_key(a.xref, "NumCounts")[1]) == m["count"] == 3
                assert a.info["content"] == "3"
                assert doc.xref_get_key(a.xref, "CountStyle")[1] == "/Checkmark"
            if m["group_parent"]:
                k, v = doc.xref_get_key(a.xref, "IRT")
                assert k == "xref" and xref_to_nm[int(v.split()[0])] == m["group_parent"]
                assert doc.xref_get_key(a.xref, "RT")[1] == "/Group"
            if m["kind"] == "cloud":
                assert doc.xref_get_key(a.xref, "ITEx")[1] == "/PolyText"
                assert doc.xref_get_key(a.xref, "BE")[0] == "dict"
            if m["kind"] in ("reply", "status"):
                k, v = doc.xref_get_key(a.xref, "IRT")
                assert k == "xref" and xref_to_nm[int(v.split()[0])] == m["reply_to"]
                assert doc.xref_get_key(a.xref, "RT")[0] == "null", "replies carry no /RT"
            if m["kind"] == "status":
                assert doc.xref_get_key(a.xref, "StateModel")[1] == "Review"
                assert doc.xref_get_key(a.xref, "State")[1] in ("Accepted", "Rejected")
            else:
                assert doc.xref_get_key(a.xref, "State")[0] == "null"
        for parent, kids in exp["groups"].items():
            for kid in kids:
                assert exp["markups"][kid]["group_parent"] == parent
        for parent, st in exp["status"].items():
            _, sa = idx[st["id"]]
            assert doc.xref_get_key(sa.xref, "State")[1] == st["state"]
        areas = [m["value"] for m in exp["markups"].values() if m["kind"] == "area"]
        assert abs(sum(areas) - exp["totals"]["area_sf"]) < 0.01
        assert exp["totals"]["count"] == 3
        assert exp["totals"]["volume_cy"] > 0
    finally:
        doc.close()


def test_builders_are_deterministic(tmp_path):
    a = S.build_takeoff_pdf(str(tmp_path / "a.pdf"))
    b = S.build_takeoff_pdf(str(tmp_path / "b.pdf"))
    a.pop("path"), b.pop("path")
    assert a == b


def test_no_custom_columns_variant(tmp_path):
    path = str(tmp_path / "plain.pdf")
    exp = S.build_takeoff_pdf(path, custom_columns=False)
    assert exp["custom_column_defs"] == []
    doc = pymupdf.open(path)
    try:
        assert doc.xref_get_key(doc.pdf_catalog(), "BSIColumnData")[0] == "null"
        assert all(not m["custom_columns"] for m in exp["markups"].values())
    finally:
        doc.close()

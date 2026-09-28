"""Takeoff math, Bluebeam string parsing and summarize() grouping / totals / warnings."""
import math

import pymupdf
import pytest

from bluebeam import markups_read as mr
from bluebeam import takeoff as T
from bluebeam.core import DocumentError
from tests import bb_synth as S

C30 = 0.4166667                      # ft per PDF point for 1 in = 30 ft


def _mk(kind, points=None, *, pdf_type="Polygon", upp=C30, factor=1.0, count=None, subject="S", page=1,
        mid=None, unit="sf", **extra):
    me = {"kind": kind, "value": None, "unit": unit, "formatted": None, "computed": None, "units_per_point": upp,
          "unit_factor": factor, "count": count, "x_unit": "ft", **extra}
    mk = mr.Markup(id=mid or f"id-{id(me)}", xref=1, page=page, pdf_type=pdf_type, subject=subject,
                   vertices=points, measurement=me)
    me["value"] = me["computed"] = T.measure_value(mk)
    me["value_source"] = "computed"
    return mk


# --- geometry ----------------------------------------------------------------

def test_area_square_at_30ft_scale():
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    assert T.area_pts(sq) == 10000
    assert T.area_pts(sq[::-1]) == 10000                    # winding does not matter
    assert round(_mk("area", sq).measurement["value"], 2) == 1736.11


def test_area_degenerate_and_triangle():
    assert T.area_pts([(0, 0), (5, 5)]) == 0
    assert T.area_pts([(0, 0), (4, 0), (0, 3)]) == 6


def test_length_345():
    assert T.length_pts([(0, 0), (3, 4)]) == 5
    poly = [(0, 0), (3, 0), (3, 4)]
    assert T.length_pts(poly) == 7
    assert T.length_pts(poly, closed=True) == 12            # + the 5-long closing segment
    assert T.length_pts([(1, 1)]) == 0


def test_measure_value_length_and_perimeter():
    line = _mk("length", [(0, 0), (300, 400)], pdf_type="Line", unit="ft")
    assert line.measurement["value"] == pytest.approx(500 * C30)
    open_poly = _mk("length", [(0, 0), (300, 0), (300, 400)], pdf_type="PolyLine", unit="ft")
    assert open_poly.measurement["value"] == pytest.approx(700 * C30)
    closed = _mk("perimeter", [(0, 0), (300, 0), (300, 400)], pdf_type="Polygon", unit="ft")
    assert closed.measurement["value"] == pytest.approx(1200 * C30)          # includes the closing segment
    scaled = _mk("length", [(0, 0), (300, 400)], pdf_type="Line", unit="in", factor=12.0)
    assert scaled.measurement["value"] == pytest.approx(500 * C30 * 12)      # D.C is applied


def test_measure_value_area_factor_and_unscaled():
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    assert _mk("area", sq, factor=2.0).measurement["value"] == pytest.approx(10000 * C30 ** 2 * 2)
    assert _mk("area", sq, upp=None).measurement["value"] is None            # no scale -> not computed
    assert T.measure_value(mr.Markup(id="p", xref=1, page=1, pdf_type="Square")) is None


def test_measure_value_count_and_angle():
    assert _mk("count", None, count=7, unit="count").measurement["value"] == 7
    right = _mk("angle", [(0, 0), (100, 0), (100, 100)], pdf_type="PolyLine", unit="°")
    assert right.measurement["value"] == pytest.approx(90)
    assert T.angle_deg([(1, 0), (0, 0), (1, 1)]) == pytest.approx(45)
    assert T.angle_deg([(0, 0), (0, 0), (1, 1)]) is None


def test_volume_value_depth_units():
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    common = {"volume_factor": 0.03703704, "x_unit": "ft"}
    inches = _mk("area", sq, depth=6.0, depth_unit="in", volume_unit="cu yd", **common)
    feet = _mk("area", sq, depth=0.5, depth_unit="ft", volume_unit="cu yd", **common)
    expected = 10000 * C30 ** 2 * 0.5 * 0.03703704
    assert T.volume_value(inches) == pytest.approx(expected)
    assert T.volume_value(feet) == pytest.approx(expected)
    assert round(expected, 2) == 32.15
    assert T.volume_value(_mk("area", sq)) is None                            # no depth
    assert T.volume_value(_mk("area", sq, depth=6.0, depth_unit="furlong", **common)) is None


@pytest.mark.parametrize("text,expected", [
    ("3,213.27 sf", (3213.27, "sf", 0.01)),
    ("200,904.9 sf", (200904.9, "sf", 0.01)),
    ("131,454 sf", (131454.0, "sf", 1.0)),
    ("96.45 cu yd", (96.45, "cu yd", 0.01)),
    ("3", (3.0, "", 1.0)),
    ("45.00°", (45.0, "°", 0.01)),
    ("81'-8 1/2\"", (81 + 8.5 / 12, "ft", 1 / 24)),
    ("39'-1\"", (39 + 1 / 12, "ft", 1 / 24)),
    ("3'-1/2\"", (3 + 0.5 / 12, "ft", 1 / 24)),
    ("324'-0\"", (324.0, "ft", 1 / 24)),
    ("1,234'-6\"", (1234.5, "ft", 1 / 24)),
    ("-2'-6\"", (-2.5, "ft", 1 / 24)),
    ("8 1/2\"", (8.5 / 12, "ft", 1 / 24)),
])
def test_parse_formatted_quantity(text, expected):
    got = T.parse_formatted_quantity(text)
    assert got[0] == pytest.approx(expected[0])
    assert got[1] == expected[1]
    assert got[2] == pytest.approx(expected[2])


@pytest.mark.parametrize("text", ["", None, "n/a", "sf 12"])
def test_parse_formatted_quantity_rejects(text):
    assert T.parse_formatted_quantity(text) is None


def test_synth_formatters_round_trip_through_parser():
    for feet in (81 + 8.5 / 12, 39.0, 3.04, 328.5):
        parsed = T.parse_formatted_quantity(S.fmt_ft_in(feet))
        assert parsed[0] == pytest.approx(feet, abs=1 / 24)
    assert T.parse_formatted_quantity(S.fmt_area(13454.86))[0] == 13454.86


# --- summarize ---------------------------------------------------------------

@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("tk") / "takeoff.pdf")
    exp = S.build_takeoff_pdf(path)
    with pymupdf.open(path) as doc:
        mks = mr.parse_markups(doc)
    return exp, mks


def _rows(summary, **where):
    return [r for r in summary["rows"] if all(r.get(k) == v or r["group"].get(k) == v for k, v in where.items())]


def test_summarize_by_subject_matches_expected_totals(synth):
    exp, mks = synth
    s = T.summarize(mks, ["subject"])
    totals = {(t["kind"], t["unit"]): t for t in s["totals"]}
    assert totals[("area", "sf")]["total"] == pytest.approx(exp["totals"]["area_sf"], abs=0.01)
    assert totals[("length", "ft")]["total"] == pytest.approx(exp["totals"]["length_ft"], abs=0.01)
    assert totals[("volume", "cu yd")]["total"] == pytest.approx(exp["totals"]["volume_cy"], abs=0.01)
    assert totals[("count", "count")]["total"] == exp["totals"]["count"] == 3        # once per group, not 3 x 3
    assert totals[("count", "count")]["markups"] == 1 and "count" not in totals[("count", "count")]
    riprap = _rows(s, kind="area", subject="Riprap Area")[0]
    assert riprap["markups"] == 2 and "count" not in riprap and riprap["pages"] == [2] and riprap["unit"] == "sf"
    assert len(riprap["ids"]) == 2
    assert _rows(s, kind="volume", subject="Riprap Area")[0]["total"] == pytest.approx(exp["totals"]["volume_cy"], abs=0.01)
    assert s["warnings"] == []
    assert s["measured"] == 6                                # 3 areas, 2 lengths, 1 count group (members skipped)
    assert s["scales"] == [{"page": 2, "scale": "1 in = 30 ft' in\"", "units_per_point": 0.4166667, "markups": 5}]


def test_summarize_group_by_variants(synth):
    _, mks = synth
    by_layer = T.summarize(mks, ["layer"])
    assert {r["group"]["layer"] for r in by_layer["rows"]} == {"Takeoff"}
    by_page_kind = T.summarize(mks, ["page", "kind"])
    assert ("2", "area") in {(str(r["group"]["page"]), r["group"]["kind"]) for r in by_page_kind["rows"]}
    by_author = T.summarize(mks, ["author"])
    assert by_author["rows"][0]["group"]["author"] == S.AUTHOR
    by_color = T.summarize(mks, ["color"])
    assert "#FF0000" in {r["group"]["color"] for r in by_color["rows"]}
    ungrouped = T.summarize(mks, [])
    assert {r["kind"] for r in ungrouped["rows"]} == {"area", "volume", "length", "count"}
    assert all(r["group"] == {} for r in ungrouped["rows"])
    assert T.summarize(mks, "subject")["group_by"] == ["subject"]            # a bare string is one field


def test_summarize_custom_column_group_and_bad_field(synth):
    _, mks = synth
    s = T.summarize(mks, ["bid item"])                                     # case-insensitive custom column name
    assert s["group_by"] == ["Bid Item"]
    groups = {r["group"]["Bid Item"] for r in s["rows"]}
    assert groups == {"RIPRAP-12", "(none)"}
    with pytest.raises(DocumentError, match="Unknown group_by field 'nonsense'"):
        T.summarize(mks, ["nonsense"])


def test_summarize_row_order_is_stable(synth):
    _, mks = synth
    a = T.summarize(mks, ["subject"])["rows"]
    b = T.summarize(list(reversed(mks)), ["subject"])["rows"]
    assert [(r["group"], r["kind"]) for r in a] == [(r["group"], r["kind"]) for r in b]
    kinds_in_riprap = [r["kind"] for r in a if r["group"]["subject"] == "Riprap Area"]
    assert kinds_in_riprap == ["area", "volume"]


def test_summarize_ignores_replies_and_plain_markups(synth):
    _, mks = synth
    plain = mr.Markup(id="p", xref=9, page=3, pdf_type="FreeText", subject="Text Box")
    assert T.summarize([plain], ["subject"])["rows"] == []
    reply = _mk("area", [(0, 0), (10, 0), (10, 10)], mid="reply")
    reply.is_reply = True
    assert T.summarize([reply], ["subject"])["measured"] == 0


def test_summarize_ids_capped():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    mks = [_mk("area", sq, mid=f"m{i}") for i in range(30)]
    row = T.summarize(mks, ["subject"], ids_cap=20)["rows"][0]
    assert row["markups"] == 30 and len(row["ids"]) == 20 and row["ids_truncated"] == 10


def test_warning_unscaled_measurement():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    m = _mk("area", sq, upp=None, mid="unscaled")
    m.measurement["contents_value"] = 100.0
    m.measurement["value"] = 100.0                                          # from Bluebeam's own text
    s = T.summarize([m], ["subject"])
    assert any("no /Measure scale" in w for w in s["warnings"])
    assert s["totals"][0]["total"] == 100.0                                # still totalled from the text value
    nothing = _mk("area", sq, upp=None, mid="nothing")
    s2 = T.summarize([nothing], ["subject"])
    assert s2["skipped"] == 1 and s2["rows"] == []
    assert any("no readable value" in w for w in s2["warnings"])


def test_warning_mixed_scales_on_one_page(tmp_path):
    path = str(tmp_path / "mixed.pdf")
    S.build_takeoff_pdf(path)
    with pymupdf.open(path) as doc:
        page = doc[1]
        S.add_area(doc, page, [(10, 10), (60, 10), (60, 60), (10, 60)], scale_ft_per_in=20.0, subject="Other Scale",
                   layer_xref=None)
        doc.saveIncr()
    with pymupdf.open(path) as doc:
        s = T.summarize(mr.parse_markups(doc), ["subject"])
    assert any("page 2 mixes 2 scales" in w for w in s["warnings"])
    assert len([x for x in s["scales"] if x["page"] == 2]) == 2


def test_warning_contents_mismatch_and_tolerance(tmp_path):
    path = str(tmp_path / "mismatch.pdf")
    exp = S.build_takeoff_pdf(path)
    slab = next(m for m in exp["markups"].values() if m["subject"] == "Concrete Slab")
    with pymupdf.open(path) as doc:
        doc.xref_set_key(slab["xref"], "Contents", "(4,000.00 sf)")          # Bluebeam says 4,000; geometry says 3,333.33
        doc.saveIncr()
    with pymupdf.open(path) as doc:
        s = T.summarize(mr.parse_markups(doc), ["subject"])
    assert len(s["warnings"]) == 1 and "geometry gives 3,333.33 sf" in s["warnings"][0]
    assert "4,000.00 sf" in s["warnings"][0] and "Bluebeam's value was used" in s["warnings"][0]
    # a normal file (values rounded by Bluebeam) never warns
    clean = str(tmp_path / "clean.pdf")
    S.build_takeoff_pdf(clean)
    with pymupdf.open(clean) as doc:
        assert T.summarize(mr.parse_markups(doc), ["subject"])["warnings"] == []


def test_warnings_are_capped():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    mks = []
    for i in range(40):
        m = _mk("area", sq, mid=f"w{i}")
        m.measurement.update(contents_value=99999.0, contents_step=0.01, formatted="99,999 sf", value_source="bluebeam")
        mks.append(m)
    s = T.summarize(mks, ["subject"], warnings_cap=15)
    assert len(s["warnings"]) == 16 and s["warnings"][-1].startswith("... and 25 more")


def test_summary_is_json_serializable(synth):
    import json
    _, mks = synth
    json.dumps(T.summarize(mks, ["subject", "layer"]))
    assert not math.isnan(sum(t["total"] for t in T.summarize(mks, ["subject"])["totals"]))


# --- Bluebeam's own value wins over recomputed geometry -----------------------

def _edited(tmp_path, edits, name="edited.pdf"):
    """build_takeoff_pdf with /Contents overwritten: edits = {subject-or-pdf_type: '(text)'} -> (exp, markups)."""
    path = str(tmp_path / name)
    exp = S.build_takeoff_pdf(path)
    with pymupdf.open(path) as doc:
        for key, text in edits.items():
            for m in exp["markups"].values():
                if key in (m["subject"], m["pdf_type"]) and m["kind"] in ("area", "length"):
                    doc.xref_set_key(m["xref"], "Contents", text)
        doc.saveIncr()
    with pymupdf.open(path) as doc:
        return exp, mr.parse_markups(doc)


def test_area_with_cutout_uses_bluebeams_value_and_warns(tmp_path):
    exp, mks = _edited(tmp_path, {"Concrete Slab": "(1,000.00 sf)"})           # Revu says 1,000; vertices give 3,333.33
    slab = next(m for m in mks if m.subject == "Concrete Slab")
    me = slab.measurement
    assert me["value"] == 1000.0 and me["value_source"] == "bluebeam"
    assert me["computed"] == pytest.approx(3333.33, abs=0.01)                    # kept as the cross-check
    s = T.summarize(mks, ["subject"])
    row = next(r for r in s["rows"] if r["group"]["subject"] == "Concrete Slab")
    assert row["total"] == 1000.0
    total = next(t for t in s["totals"] if t["kind"] == "area")["total"]
    assert total == pytest.approx(exp["totals"]["area_sf"] - 3333.33 + 1000.0, abs=0.01)
    (w,) = s["warnings"]
    assert "Concrete Slab" in w and "3,333.33 sf" in w and "1,000.00 sf" in w and "Bluebeam's value was used" in w


def test_normal_files_use_bluebeams_value_without_warnings(tmp_path):
    path = str(tmp_path / "plain.pdf")
    exp = S.build_takeoff_pdf(path)
    with pymupdf.open(path) as doc:
        mks = mr.parse_markups(doc)
    for m in mks:
        if m.measurement and m.kind in ("area", "length"):
            assert m.measurement["value_source"] == "bluebeam"
            assert m.measurement["value"] == m.measurement["contents_value"]
    s = T.summarize(mks, ["subject"])
    assert s["warnings"] == []
    length = next(t for t in s["totals"] if t["kind"] == "length")["total"]
    assert length == pytest.approx(exp["totals"]["length_ft"], abs=0.02)         # feet-inch text is rounded to 1/4 in


def test_volume_uses_bluebeams_area(tmp_path):
    exp, mks = _edited(tmp_path, {"Riprap Area": "(4,000 sf)"})                  # both riprap polygons are edited
    deep = next(m for m in mks if m.measurement and m.measurement.get("depth"))
    me = deep.measurement
    assert me["value"] == 4000.0
    assert me["volume"] == pytest.approx(4000 * 0.5 * 0.03703704, abs=1e-6)     # 6 in = 0.5 ft, V.C = 1/27
    s = T.summarize(mks, ["subject"])
    vol = next(t for t in s["totals"] if t["kind"] == "volume")
    assert vol["total"] == pytest.approx(74.07, abs=0.01)
    assert next(t for t in s["totals"] if t["kind"] == "area")["total"] == pytest.approx(8000 + 3333.33, abs=0.01)


def test_length_text_used_and_incompatible_units_fall_back(tmp_path):
    _, mks = _edited(tmp_path, {"Length Measurement": "(10'-0\")", "Perimeter Measurement": "(12 in)",
                                "Concrete Slab": "(n/a)"})
    by = {m.subject: m.measurement for m in mks if m.measurement and m.kind in ("area", "length")}
    line, peri, slab = by["Length Measurement"], by["Perimeter Measurement"], by["Concrete Slab"]
    assert line["value"] == 10.0 and line["unit"] == "ft" and line["value_source"] == "bluebeam"
    assert line["computed"] == pytest.approx(61.94, abs=0.01)
    assert peri["value_source"] == "computed" and peri["value"] == peri["computed"]     # 'in' is not the ft unit
    assert slab["value_source"] == "computed" and slab["value"] == pytest.approx(3333.33, abs=0.01)
    s = T.summarize(mks, ["subject"])
    assert len(s["warnings"]) == 1 and "Length Measurement" in s["warnings"][0]        # only the value Bluebeam's text set


def test_missing_contents_falls_back_to_geometry(tmp_path):
    _, mks = _edited(tmp_path, {"Concrete Slab": "()"})
    slab = next(m for m in mks if m.subject == "Concrete Slab").measurement
    assert slab["value_source"] == "computed" and slab["value"] == pytest.approx(3333.33, abs=0.01)
    assert slab["contents_value"] is None


def test_bluebeam_value_unit_matching():
    me = {"kind": "area", "unit": "cu yd", "contents_value": 96.45, "contents_unit": "cu yd"}
    assert T.bluebeam_value(me) == 96.45
    assert T.bluebeam_value({**me, "contents_unit": "cuyd"}) == 96.45              # spacing / case ignored
    assert T.bluebeam_value({**me, "contents_unit": "SF"}) is None
    assert T.bluebeam_value({**me, "contents_value": None}) is None
    assert T.bluebeam_value({**me, "kind": "count"}) is None
    assert T.bluebeam_value({**me, "unit": None, "contents_unit": ""}) == 96.45


def test_volume_value_from_a_given_area():
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    mk = _mk("area", sq, depth=6.0, depth_unit="in", volume_factor=0.03703704, factor=2.0)
    assert T.volume_value(mk, area=4000.0) == pytest.approx(4000 / 2.0 * 0.5 * 0.03703704)   # A.C is undone first
    assert T.volume_value(mk, area=None) == pytest.approx(10000 * C30 ** 2 * 0.5 * 0.03703704)
    assert T.volume_value(_mk("area", sq), area=100.0) is None                               # no depth


# --- labels: volume_unverified, markups vs total ------------------------------

def test_volume_rows_are_flagged_unverified(synth):
    _, mks = synth
    s = T.summarize(mks, ["subject"])
    volume_rows = [r for r in s["rows"] if r["kind"] == "volume"]
    assert volume_rows and all(r["volume_unverified"] is True for r in volume_rows)
    assert all("volume_unverified" not in r for r in s["rows"] if r["kind"] != "volume")
    assert [t["volume_unverified"] for t in s["totals"] if t["kind"] == "volume"] == [True]
    assert all("volume_unverified" not in t for t in s["totals"] if t["kind"] != "volume")
    assert len(s["notes"]) == 1 and "unverified" in s["notes"][0] and "/Depth" in s["notes"][0]
    no_volume = T.summarize([m for m in mks if m.page == 3 or m.kind != "area"], ["subject"])
    assert "notes" not in no_volume and not any(r["kind"] == "volume" for r in no_volume["rows"])


def test_rows_name_markups_and_total_distinctly(synth):
    _, mks = synth
    s = T.summarize(mks, ["subject"])
    inlets = next(r for r in s["rows"] if r["group"]["subject"] == "Inlet Count")
    assert inlets["kind"] == "count" and inlets["markups"] == 1 and inlets["total"] == 3   # one group of 3 symbols
    assert all("count" not in r for r in s["rows"] + s["totals"])

"""Synthetic Bluebeam-style PDF builders for tests. Pure functions, no pytest inside.

They reproduce the annotation STRUCTURE observed in real Bluebeam Revu 21 takeoff and
review PDFs (key names, value types, direct vs indirect /Measure, number formatting)
with invented geometry and text only. Nothing here comes from a real drawing.

Observed facts these builders follow (see the advisor report):
* Measurement markups carry /IT (PolygonDimension = area, PolyLineDimension = perimeter,
  LineDimension = length, PolygonCount = count), /MeasurementTypes (129 area, 130 length,
  128 count; bit 128 is always set), /Measure (usually an INDIRECT object) whose
  /X[0]/C is "real units per PDF point" (0.4166667 ft/pt for 1 in = 30 ft), /Contents is
  Bluebeam's formatted value ("3,213.27 sf", 81'-8 1/2"), /Depth + /DepthUnit make a
  volume, counts carry /NumCounts + /CountStyle, grouped markups use /IRT + /RT /Group,
  replies use /IRT without /RT, review statuses are Text annots with /State + /StateModel.
* Math: area = shoelace(vertices in pt) * X.C^2 * A.C ; length = sum(segments) * X.C * D.C.
* Page labels written by CAD tools are UTF-16 hex strings; PyMuPDF hands them back raw
  ('<FEFF...>'), so bluebeam.core.decode_label() is needed.
* /BSIColumnData (custom columns) was NOT present in the real files; the layout used here
  (catalog array of {Subtype, Name, DisplayOrder...} + positional string array per markup)
  follows public examples and is a best-effort guess. Parsers must treat it as such.
"""
from __future__ import annotations

import math
import uuid
from typing import Optional, Sequence

import pymupdf

Point = tuple[float, float]

DATE = "D:20260928120000-07'00'"
AUTHOR = "Estimator E"
REVIEWER = "Reviewer R"

_NS = uuid.UUID("9b8f2c7e-5a4d-4c3b-9e21-6f0d1a2b3c4d")
_counter = [0]


# --- ids, strings, numbers -------------------------------------------------

def reset_ids() -> None:
    """Restart the deterministic /NM sequence (build_takeoff_pdf calls this)."""
    _counter[0] = 0


def next_nm() -> str:
    """Deterministic GUID-style /NM value (Bluebeam uses GUIDs)."""
    _counter[0] += 1
    return str(uuid.uuid5(_NS, str(_counter[0])))


def pdf_str(s: str) -> str:
    """Literal PDF string with escapes, e.g. (81'-8 1/2")."""
    return "(" + str(s).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def utf16_hex(s: str) -> str:
    """UTF-16BE hex string with BOM, the way CAD plotters write page labels: <FEFF...>."""
    return "<" + ("﻿" + s).encode("utf-16-be").hex().upper() + ">"


def _num(v: float) -> str:
    """Number the way Bluebeam writes them: 7 significant digits, no leading zero (.4166667)."""
    s = f"{float(v):.7g}"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def units_per_point(scale_ft_per_in: float) -> float:
    """/Measure /X[0]/C for '1 in = N ft': feet per PDF point, stored at 7 significant digits."""
    return float(f"{scale_ft_per_in / 72.0:.7g}")


# --- math and Bluebeam formatting -----------------------------------------

def shoelace(points: Sequence[Point]) -> float:
    n = len(points)
    return abs(sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
                   for i in range(n))) / 2.0


def path_length(points: Sequence[Point], closed: bool = False) -> float:
    pts = list(points) + ([points[0]] if closed else [])
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def fmt_area(sf: float, unit: str = "sf") -> str:
    """'3,213.27 sf', '200,904.9 sf', '131,454 sf' (2 decimals, trailing zeros dropped)."""
    s = f"{round(sf, 2):,.2f}".rstrip("0").rstrip(".")
    return f"{s} {unit}"


def fmt_volume(v: float, unit: str = "cu yd") -> str:
    return fmt_area(v, unit)


def fmt_ft_in(feet: float, denom: int = 4) -> str:
    """Feet-inches to the nearest 1/denom inch: 81'-8 1/2"  39'-1"  3'-1/2"  324'-0"."""
    total = int(round(feet * 12 * denom))
    ft, rem = divmod(total, 12 * denom)
    inches, frac = divmod(rem, denom)
    d = denom
    while frac and frac % 2 == 0 and d % 2 == 0:
        frac //= 2
        d //= 2
    if frac == 0:
        return f"{ft}'-{inches}\""
    if inches == 0:
        return f"{ft}'-{frac}/{d}\""
    return f"{ft}'-{inches} {frac}/{d}\""


def fmt_count(n: int) -> str:
    return str(int(n))


# --- /Measure and friends --------------------------------------------------

def measure_dict(scale_ft_per_in: float = 30.0, *, scale_text: Optional[str] = None,
                 area_unit: str = "sf", volume_unit: str = "cu yd") -> str:
    """Source of a Bluebeam RL /Measure dictionary (feet-inches drawing scale).

    X = feet per point (the value the math uses), D = ft + in display formats (D 4 = 1/4"),
    A = area format, T = angle (degree sign as hex <B0>), V = volume (cu yd = cu ft * 1/27).
    """
    c = _num(units_per_point(scale_ft_per_in))
    r = scale_text or f"1 in = {scale_ft_per_in:g} ft' in\""
    vc = ".03703704" if volume_unit == "cu yd" else "1"
    return ("<</Type/Measure/Subtype/RL/R" + pdf_str(r)
            + "/X[<</Type/NumberFormat/U(')/C " + c + "/F/F/D 4/FD true/SS()>>]"
            + "/D[<</Type/NumberFormat/U(')/C 1/F/F/D 4/FD true/PS()/SS(-)>>"
            + "<</Type/NumberFormat/U(\")/C 12/F/F/D 4/FD true/PS()/SS()>>]"
            + "/A[<</Type/NumberFormat/U" + pdf_str(area_unit) + "/C 1/D 100/FD true/SS()>>]"
            + "/T[<</Type/NumberFormat/U<B0>/C 1/D 100/FD true/PS()/SS()>>]"
            + "/V[<</Type/NumberFormat/U" + pdf_str(volume_unit) + "/C " + vc + "/D 100/FD true/SS()>>]"
            + "/TargetUnitConversion .001157407>>")


def depth_unit_array(unit: str = "in") -> str:
    """/DepthUnit: one NumberFormat; C converts PDF points to the unit (in: 1/72, ft: 1/864)."""
    c = ".01388889" if unit == "in" else ".001157407"
    u = "(in)" if unit == "in" else "(')"
    return "[<</Type/NumberFormat/U" + u + "/C " + c + "/D 100/FD true/SS()>>]"


def attach_measure(doc: pymupdf.Document, xref: int, src: str, indirect: bool = True) -> Optional[int]:
    """Attach a /Measure dict; indirect (as Revu writes it) returns the new object's xref."""
    if indirect:
        mx = doc.get_new_xref()
        doc.update_object(mx, src)
        doc.xref_set_key(xref, "Measure", f"{mx} 0 R")
        return mx
    doc.xref_set_key(xref, "Measure", src)
    return None


# --- document-level helpers ------------------------------------------------

def add_layer(doc: pymupdf.Document, name: str, on: bool = True) -> int:
    """Create an OCG (layer); returns its xref for annot.set_oc()."""
    return doc.add_ocg(name, on=1 if on else 0)


def set_page_labels(doc: pymupdf.Document, labels: Sequence[str]) -> None:
    """One explicit label per page as UTF-16 hex /P strings (the CAD-plotter form)."""
    nums = " ".join(f"{i} <</P {utf16_hex(lab)}>>" for i, lab in enumerate(labels))
    doc.xref_set_key(doc.pdf_catalog(), "PageLabels", f"<</Nums[{nums}]>>")


def set_viewport_scale(doc: pymupdf.Document, page: pymupdf.Page, scale_ft_per_in: float = 30.0,
                       bbox: Optional[Sequence[float]] = None, indirect: bool = True) -> None:
    """Page-level /VP viewport carrying the drawing scale (Revu writes it as [N 0 R])."""
    r = page.rect
    bb = bbox or (0, 0, r.width, r.height)
    vp = ("<</Type/Viewport/BBox[" + " ".join(_num(v) for v in bb) + "]/Measure"
          + measure_dict(scale_ft_per_in) + ">>")
    if indirect:
        vx = doc.get_new_xref()
        doc.update_object(vx, vp)
        doc.xref_set_key(page.xref, "VP", f"[{vx} 0 R]")
    else:
        doc.xref_set_key(page.xref, "VP", f"[{vp}]")


def set_custom_columns(doc: pymupdf.Document, defs: Sequence[dict]) -> int:
    """Catalog /BSIColumnData -> indirect array of column definitions (best-effort layout).

    defs: [{"name": str, "subtype": "Text"|"Number"|"Choice"|"Formula",
            "precision": int?, "format": str?, "choices": [str]?, "expression": str?}]
    DisplayOrder = position in defs. Returns the array's xref.
    """
    items = []
    for i, d in enumerate(defs):
        s = "<</Subtype/" + d.get("subtype", "Text") + "/Name" + pdf_str(d["name"]) + f"/DisplayOrder {i}"
        if "precision" in d:
            s += f"/Precision {int(d['precision'])}"
        if "format" in d:
            s += "/Format" + pdf_str(d["format"])
        if "expression" in d:
            s += "/Expression" + pdf_str(d["expression"])
        if "choices" in d:
            s += "/Choices[" + "".join(pdf_str(c) for c in d["choices"]) + "]"
        items.append(s + ">>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "[" + "".join(items) + "]")
    doc.xref_set_key(doc.pdf_catalog(), "BSIColumnData", f"{cx} 0 R")
    return cx


def set_column_values(doc: pymupdf.Document, xref: int, values: Sequence[str]) -> None:
    """Per-markup /BSIColumnData: positional string array matching the catalog definitions."""
    doc.xref_set_key(xref, "BSIColumnData", "[" + "".join(pdf_str(v) for v in values) + "]")


def annot_by_xref(page: pymupdf.Page, xref: int) -> pymupdf.Annot:
    for a in page.annots():
        if a.xref == xref:
            return a
    raise KeyError(f"no annot with xref {xref} on page {page.number + 1}")


# --- per-markup internals --------------------------------------------------

def _style(annot: pymupdf.Annot, color, fill, opacity: float, width: float = 1.0, clouds: int = -1) -> None:
    annot.set_colors(stroke=color, fill=fill)
    annot.set_opacity(opacity)
    annot.set_border(width=width, clouds=clouds)
    annot.update()


def _common(doc: pymupdf.Document, annot: pymupdf.Annot, *, nm: str, subject: str, author: str,
            contents: str = "", layer_xref: Optional[int] = None, label: Optional[str] = None,
            fill_opacity: Optional[float] = None) -> None:
    annot.set_info(title=author, subject=subject, content=contents, creationDate=DATE, modDate=DATE)
    x = annot.xref
    doc.xref_set_key(x, "NM", pdf_str(nm))
    doc.xref_set_key(x, "F", "4")
    if layer_xref:
        annot.set_oc(layer_xref)
    if label is not None:
        doc.xref_set_key(x, "Label", pdf_str(label))
    if fill_opacity is not None:
        doc.xref_set_key(x, "FillOpacity", _num(fill_opacity))
    doc.xref_set_key(x, "DS", pdf_str("font: Helvetica 12pt; text-align:left; line-height:13.8pt; color:#000000"))


def _measure_keys(doc: pymupdf.Document, x: int, intent: str, mtype: int) -> None:
    doc.xref_set_key(x, "IT", "/" + intent)
    doc.xref_set_key(x, "MeasurementTypes", str(mtype))
    doc.xref_set_key(x, "Cap", "true")
    doc.xref_set_key(x, "AlignOnSegment", "false")
    doc.xref_set_key(x, "SlopeType", "0")
    doc.xref_set_key(x, "PitchRun", "12")


def _group(doc: pymupdf.Document, child_xref: int, parent_xref: int) -> None:
    """Bluebeam grouping: child /IRT parent + /RT /Group."""
    doc.xref_set_key(child_xref, "IRT", f"{parent_xref} 0 R")
    doc.xref_set_key(child_xref, "RT", "/Group")


# --- markup builders -------------------------------------------------------

def add_area(doc: pymupdf.Document, page: pymupdf.Page, points: Sequence[Point], *,
             scale_ft_per_in: float = 30.0, depth: Optional[float] = None, depth_unit: str = "in",
             subject: str = "Area Measurement", author: str = AUTHOR, layer_xref: Optional[int] = None,
             columns: Optional[Sequence[str]] = None, label: str = "", color=(1, 0, 0),
             fill=(1, 0.8, 0.8), opacity: float = 0.5, indirect_measure: bool = True,
             nm: Optional[str] = None, scale_text: Optional[str] = None) -> int:
    """Polygon /IT /PolygonDimension, /MeasurementTypes 129, /Contents '<area> sf'.

    points are page coordinates (points, origin top-left). depth (in depth_unit) adds
    /Depth + /DepthUnit, which Revu shows as a volume. Returns the annot xref.
    """
    annot = page.add_polygon_annot([pymupdf.Point(*p) for p in points])
    _style(annot, color, fill, opacity)
    c = units_per_point(scale_ft_per_in)
    area = round(shoelace(points) * c * c, 2)          # A.C == 1 (sf)
    _common(doc, annot, nm=nm or next_nm(), subject=subject, author=author, contents=fmt_area(area),
            layer_xref=layer_xref, label=label, fill_opacity=opacity)
    x = annot.xref
    _measure_keys(doc, x, "PolygonDimension", 129)
    attach_measure(doc, x, measure_dict(scale_ft_per_in, scale_text=scale_text), indirect_measure)
    if depth is not None:
        doc.xref_set_key(x, "Depth", _num(depth))
        doc.xref_set_key(x, "DepthUnit", depth_unit_array(depth_unit))
    if columns:
        set_column_values(doc, x, columns)
    return x


def add_length(doc: pymupdf.Document, page: pymupdf.Page, points: Sequence[Point], *,
               scale_ft_per_in: float = 30.0, as_line: bool = False, subject: Optional[str] = None,
               author: str = AUTHOR, layer_xref: Optional[int] = None, label: str = "",
               color=(0, 0, 1), opacity: float = 1.0, indirect_measure: bool = True,
               nm: Optional[str] = None, scale_text: Optional[str] = None) -> int:
    """PolyLine /IT /PolyLineDimension (or Line /IT /LineDimension with as_line and 2 points),
    /MeasurementTypes 130, /Contents in feet-inches (81'-8 1/2"). Returns the annot xref."""
    if as_line:
        if len(points) != 2:
            raise ValueError("as_line needs exactly 2 points")
        annot = page.add_line_annot(pymupdf.Point(*points[0]), pymupdf.Point(*points[1]))
        intent, subj = "LineDimension", subject or "Length Measurement"
    else:
        annot = page.add_polyline_annot([pymupdf.Point(*p) for p in points])
        intent, subj = "PolyLineDimension", subject or "Perimeter Measurement"
    _style(annot, color, None, opacity)
    c = units_per_point(scale_ft_per_in)
    feet = path_length(points) * c                     # D.C == 1 (ft)
    _common(doc, annot, nm=nm or next_nm(), subject=subj, author=author, contents=fmt_ft_in(feet),
            layer_xref=layer_xref, label=label)
    x = annot.xref
    _measure_keys(doc, x, intent, 130)
    if as_line:
        doc.xref_set_key(x, "LL", "0")
        doc.xref_set_key(x, "LLE", "0")
    attach_measure(doc, x, measure_dict(scale_ft_per_in, scale_text=scale_text), indirect_measure)
    return x


def _checkmark(cx: float, cy: float, s: float = 6.0) -> list[Point]:
    return [(cx - s, cy), (cx - s * 0.6, cy - s * 0.4), (cx - s * 0.2, cy + s * 0.4),
            (cx + s, cy - s), (cx + s * 0.7, cy - s * 1.3), (cx - s * 0.2, cy - s * 0.2)]


def add_count(doc: pymupdf.Document, page: pymupdf.Page, points: Sequence[Point], *,
              subject: str = "Count", author: str = AUTHOR, layer_xref: Optional[int] = None,
              count_style: str = "Checkmark", color=(1, 0.5, 0), label: str = "",
              nms: Optional[Sequence[str]] = None) -> list[int]:
    """A count of len(points): one Polygon per symbol, /IT /PolygonCount, /MeasurementTypes 128,
    /CountStyle, /NumCounts N and /Contents 'N' on EVERY member; members after the first are
    grouped to the first (/IRT + /RT /Group). The count value is N, not the sum over members.
    Returns the member xrefs (first = group parent)."""
    n = len(points)
    xrefs: list[int] = []
    for i, (px, py) in enumerate(points):
        annot = page.add_polygon_annot([pymupdf.Point(*p) for p in _checkmark(px, py)])
        _style(annot, color, color, 1.0, width=0)
        nm = nms[i] if nms else next_nm()
        _common(doc, annot, nm=nm, subject=subject, author=author, contents=fmt_count(n),
                layer_xref=layer_xref, label=label)
        x = annot.xref
        doc.xref_set_key(x, "IT", "/PolygonCount")
        doc.xref_set_key(x, "MeasurementTypes", "128")
        doc.xref_set_key(x, "CountStyle", "/" + count_style)
        doc.xref_set_key(x, "NumCounts", str(n))
        doc.xref_set_key(x, "Version", "1")
        if xrefs:
            _group(doc, x, xrefs[0])
        xrefs.append(x)
    return xrefs


def add_text_box(doc: pymupdf.Document, page: pymupdf.Page, rect: Sequence[float], text: str, *,
                 subject: str = "Text Box", author: str = AUTHOR, layer_xref: Optional[int] = None,
                 nm: Optional[str] = None) -> int:
    """Plain FreeText (no intent) with /RC rich text and /DS style. Returns the xref."""
    # PyMuPDF 1.28: border_color is only allowed with richtext=True, so the border is left to /DS
    annot = page.add_freetext_annot(pymupdf.Rect(*rect), text, fontsize=12, text_color=(0, 0, 0),
                                    fill_color=(1, 1, 0.8))
    annot.update()
    _common(doc, annot, nm=nm or next_nm(), subject=subject, author=author, contents=text, layer_xref=layer_xref)
    rc = ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
          'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" xfa:APIVersion="BluebeamPDFRevu:21.11" '
          'xfa:spec="2.0.2"><p>' + text + '</p></body>')
    doc.xref_set_key(annot.xref, "RC", pdf_str(rc))
    return annot.xref


def add_callout(doc: pymupdf.Document, page: pymupdf.Page, rect: Sequence[float], text: str,
                leader_tip: Point, *, subject: str = "Callout", author: str = AUTHOR,
                layer_xref: Optional[int] = None, nm: Optional[str] = None) -> int:
    """FreeText /IT /FreeTextCallout with a /CL leader. Returns the xref."""
    r = pymupdf.Rect(*rect)
    tip = pymupdf.Point(*leader_tip)
    knee = pymupdf.Point((tip.x + r.x0) / 2, (tip.y + (r.y0 + r.y1) / 2) / 2)
    edge = pymupdf.Point(r.x0, (r.y0 + r.y1) / 2)
    annot = page.add_freetext_annot(r, text, fontsize=12, text_color=(0, 0, 0), fill_color=(1, 1, 1),
                                    callout=[tip, knee, edge], line_end=pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
    annot.update()
    _common(doc, annot, nm=nm or next_nm(), subject=subject, author=author, contents=text, layer_xref=layer_xref)
    doc.xref_set_key(annot.xref, "IT", "/FreeTextCallout")
    return annot.xref


def add_cloud_plus(doc: pymupdf.Document, page: pymupdf.Page, cloud_points: Sequence[Point],
                   callout_rect: Sequence[float], text: str, *, subject: str = "Cloud+",
                   author: str = AUTHOR, layer_xref: Optional[int] = None,
                   nms: Optional[Sequence[str]] = None) -> tuple[int, int]:
    """Bluebeam Cloud+: a FreeText callout (group PARENT) plus a cloudy Polygon CHILD with
    /IT /PolygonCloud, /ITEx /PolyText, /BE cloudy border, /IRT parent, /RT /Group.
    Returns (callout_xref, cloud_xref)."""
    cx = sum(p[0] for p in cloud_points) / len(cloud_points)
    cy = sum(p[1] for p in cloud_points) / len(cloud_points)
    callout = add_callout(doc, page, callout_rect, text, (cx, cy), subject=subject, author=author,
                          layer_xref=layer_xref, nm=nms[0] if nms else None)
    annot = page.add_polygon_annot([pymupdf.Point(*p) for p in cloud_points])
    _style(annot, (1, 0, 0), None, 1.0, width=1, clouds=1)
    _common(doc, annot, nm=(nms[1] if nms else next_nm()), subject=subject, author=author, layer_xref=layer_xref)
    x = annot.xref
    doc.xref_set_key(x, "IT", "/PolygonCloud")
    doc.xref_set_key(x, "ITEx", "/PolyText")
    doc.xref_set_key(x, "BE", "<</S/C/I 1>>")
    doc.xref_set_key(x, "BM", "/Normal")
    _group(doc, x, callout)
    return callout, x


def add_rectangle(doc: pymupdf.Document, page: pymupdf.Page, rect: Sequence[float], *,
                  subject: str = "Rectangle", author: str = AUTHOR, layer_xref: Optional[int] = None,
                  nm: Optional[str] = None) -> int:
    annot = page.add_rect_annot(pymupdf.Rect(*rect))
    _style(annot, (0, 0.5, 0), None, 1.0)
    _common(doc, annot, nm=nm or next_nm(), subject=subject, author=author, layer_xref=layer_xref)
    return annot.xref


def _reply_point(page: pymupdf.Page, parent_xref: int) -> pymupdf.Point:
    r = annot_by_xref(page, parent_xref).rect
    return pymupdf.Point(r.x0, r.y0)


def add_reply(doc: pymupdf.Document, page: pymupdf.Page, parent_xref: int, text: str, *,
              author: str = REVIEWER, nm: Optional[str] = None) -> int:
    """Plain reply: Text annot with /IRT parent and NO /RT (reply-type R is the default)."""
    annot = page.add_text_annot(_reply_point(page, parent_xref), text, icon="Comment")
    annot.set_info(title=author, content=text, creationDate=DATE, modDate=DATE)
    annot.update()
    x = annot.xref
    doc.xref_set_key(x, "NM", pdf_str(nm or next_nm()))
    doc.xref_set_key(x, "IRT", f"{parent_xref} 0 R")
    doc.xref_set_key(x, "F", "28")
    return x


def add_status(doc: pymupdf.Document, page: pymupdf.Page, parent_xref: int, state: str = "Accepted", *,
               model: str = "Review", author: str = REVIEWER, nm: Optional[str] = None) -> int:
    """Review status: hidden Text annot with /IRT parent, /StateModel and /State (PDF spec 12.5.6.4;
    Review states: Accepted, Rejected, Cancelled, Completed, None)."""
    text = f"{state} set by {author}"
    annot = page.add_text_annot(_reply_point(page, parent_xref), text, icon="Comment")
    annot.set_info(title=author, content=text, creationDate=DATE, modDate=DATE)
    annot.update()
    x = annot.xref
    doc.xref_set_key(x, "NM", pdf_str(nm or next_nm()))
    doc.xref_set_key(x, "IRT", f"{parent_xref} 0 R")
    doc.xref_set_key(x, "StateModel", pdf_str(model))
    doc.xref_set_key(x, "State", pdf_str(state))
    doc.xref_set_key(x, "F", "30")
    return x


# --- the reference document ------------------------------------------------

def nm_of(doc: pymupdf.Document, xref: int) -> str:
    return doc.xref_get_key(xref, "NM")[1]


def build_takeoff_pdf(path: str, *, indirect_measure: bool = True, custom_columns: bool = True) -> dict:
    """Write a 3-page synthetic takeoff/review PDF and return the EXPECTED parse of it.

    Page 1 'COVER SHEET': no markups.  Page 2 'SITE PLAN' (layer 'Takeoff', page /VP scale 1"=30'):
    two 'Riprap Area' polygons (first with 6 in depth and custom-column values; second direct
    /Measure when indirect_measure is True, to cover both parser paths), one 'Concrete Slab'
    area, one perimeter polyline, one Line length, one 3-symbol count group.
    Page 3 'DETAILS' (layer 'Review'): Cloud+ (callout parent + cloud child), a plain reply and an
    'Accepted' status on the callout, a text box, a rectangle with a 'Rejected' status.

    Returned dict:
      path, page_count, page_labels {page: text}, layers [names], scale {text, ft_per_in,
      units_per_point}, custom_column_defs [names], markups {nm: {...}}, by_kind {kind: [nm]},
      groups {parent_nm: [child_nm]}, replies {parent_nm: [reply_nm]}, status {nm: {state, model, by}},
      totals {area_sf, length_ft, count, volume_cy}.
    Each markups entry: id, xref, page, pdf_type, intent, subject, author, layer, kind
      (area|length|count|cloud|callout|text|rectangle|reply|status), value, unit, formatted,
      depth, depth_unit, volume, volume_unit, count, custom_columns {name: value}, group_parent,
      reply_to, vertex_count, measure_indirect.
    """
    reset_ids()
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=612, height=792)
    labels = ["[1] 1 COVER SHEET", "[2] 2 SITE PLAN", "[3] 3 DETAILS"]
    set_page_labels(doc, labels)
    takeoff = add_layer(doc, "Takeoff")
    review = add_layer(doc, "Review")
    defs = [{"name": "Bid Item", "subtype": "Text"},
            {"name": "Unit Cost", "subtype": "Number", "precision": 2, "format": "0.00"},
            {"name": "Phase", "subtype": "Choice", "choices": ["Phase 1", "Phase 2"]}]
    if custom_columns:
        set_custom_columns(doc, defs)
    scale = 30.0
    c = units_per_point(scale)
    exp: dict = {"path": path, "page_count": 3, "page_labels": {i + 1: lab for i, lab in enumerate(labels)},
                 "layers": ["Takeoff", "Review"],
                 "scale": {"text": "1 in = 30 ft' in\"", "ft_per_in": scale, "units_per_point": c},
                 "custom_column_defs": [d["name"] for d in defs] if custom_columns else [],
                 "markups": {}, "by_kind": {}, "groups": {}, "replies": {}, "status": {},
                 "totals": {"area_sf": 0.0, "length_ft": 0.0, "count": 0, "volume_cy": 0.0}}

    def rec(xref: int, page: int, kind: str, **kw) -> str:
        nm = nm_of(doc, xref)
        base = {"id": nm, "xref": xref, "page": page, "pdf_type": None, "intent": None, "subject": None,
                "author": AUTHOR, "layer": None, "kind": kind, "value": None, "unit": None, "formatted": None,
                "depth": None, "depth_unit": None, "volume": None, "volume_unit": None, "count": None,
                "custom_columns": {}, "group_parent": None, "reply_to": None, "vertex_count": None,
                "measure_indirect": None}
        base.update(kw)
        exp["markups"][nm] = base
        exp["by_kind"].setdefault(kind, []).append(nm)
        return nm

    # ---- page 2: takeoff
    p2 = doc[1]
    set_viewport_scale(doc, p2, scale)
    areas = [("Riprap Area", [(100, 100), (300, 100), (300, 250), (100, 250)], 6.0, ["RIPRAP-12", "45.5", "Phase 1"], indirect_measure),
             ("Riprap Area", [(350, 120), (500, 140), (520, 300), (380, 320), (340, 200)], None, None, not indirect_measure),
             ("Concrete Slab", [(120, 400), (280, 400), (280, 520), (120, 520)], None, None, indirect_measure)]
    for subj, pts, depth, cols, ind in areas:
        x = add_area(doc, p2, pts, scale_ft_per_in=scale, depth=depth, subject=subj, layer_xref=takeoff,
                     columns=cols if custom_columns else None, indirect_measure=ind)
        area = round(shoelace(pts) * c * c, 2)
        vol = round(area * (depth / 12.0) * 0.03703704, 2) if depth is not None else None
        rec(x, 2, "area", pdf_type="Polygon", intent="PolygonDimension", subject=subj, layer="Takeoff",
            value=area, unit="sf", formatted=fmt_area(area), depth=depth, depth_unit="in" if depth is not None else None,
            volume=vol, volume_unit="cu yd" if vol is not None else None, vertex_count=len(pts),
            custom_columns=dict(zip(exp["custom_column_defs"], cols)) if cols and custom_columns else {},
            measure_indirect=ind)
        exp["totals"]["area_sf"] = round(exp["totals"]["area_sf"] + area, 2)
        if vol:
            exp["totals"]["volume_cy"] = round(exp["totals"]["volume_cy"] + vol, 2)
    peri_pts = [(100, 600), (250, 600), (250, 700), (400, 700), (400, 760)]
    x = add_length(doc, p2, peri_pts, scale_ft_per_in=scale, layer_xref=takeoff, indirect_measure=indirect_measure)
    feet = path_length(peri_pts) * c
    rec(x, 2, "length", pdf_type="PolyLine", intent="PolyLineDimension", subject="Perimeter Measurement",
        layer="Takeoff", value=round(feet, 4), unit="ft", formatted=fmt_ft_in(feet), vertex_count=len(peri_pts),
        measure_indirect=indirect_measure)
    exp["totals"]["length_ft"] += feet
    line_pts = [(450, 420), (560, 520)]
    x = add_length(doc, p2, line_pts, scale_ft_per_in=scale, as_line=True, layer_xref=takeoff, indirect_measure=indirect_measure)
    feet = path_length(line_pts) * c
    rec(x, 2, "length", pdf_type="Line", intent="LineDimension", subject="Length Measurement", layer="Takeoff",
        value=round(feet, 4), unit="ft", formatted=fmt_ft_in(feet), vertex_count=2, measure_indirect=indirect_measure)
    exp["totals"]["length_ft"] = round(exp["totals"]["length_ft"] + feet, 4)
    count_pts = [(470, 620), (500, 650), (530, 680)]
    xs = add_count(doc, p2, count_pts, subject="Inlet Count", layer_xref=takeoff)
    parent_nm = None
    for i, x in enumerate(xs):
        nm = rec(x, 2, "count", pdf_type="Polygon", intent="PolygonCount", subject="Inlet Count", layer="Takeoff",
                 value=len(xs), unit="count", formatted=fmt_count(len(xs)), count=len(xs), vertex_count=6,
                 group_parent=parent_nm)
        if i == 0:
            parent_nm = nm
            exp["groups"][nm] = []
        else:
            exp["groups"][parent_nm].append(nm)
    exp["totals"]["count"] += len(xs)

    # ---- page 3: review
    p3 = doc[2]
    callout, cloud = add_cloud_plus(doc, p3, [(150, 150), (300, 140), (320, 260), (140, 270)],
                                    (360, 100, 540, 160), "Verify wall thickness", layer_xref=review)
    c_nm = rec(callout, 3, "callout", pdf_type="FreeText", intent="FreeTextCallout", subject="Cloud+", layer="Review",
               formatted="Verify wall thickness")
    cl_nm = rec(cloud, 3, "cloud", pdf_type="Polygon", intent="PolygonCloud", subject="Cloud+", layer="Review",
                vertex_count=4, group_parent=c_nm)
    exp["groups"][c_nm] = [cl_nm]
    r = add_reply(doc, p3, callout, "Checked against the detail")
    r_nm = rec(r, 3, "reply", pdf_type="Text", author=REVIEWER, formatted="Checked against the detail", reply_to=c_nm)
    exp["replies"][c_nm] = [r_nm]
    s = add_status(doc, p3, callout, "Accepted")
    s_nm = rec(s, 3, "status", pdf_type="Text", author=REVIEWER, reply_to=c_nm)
    exp["status"][c_nm] = {"state": "Accepted", "model": "Review", "by": REVIEWER, "id": s_nm}
    t = add_text_box(doc, p3, (100, 400, 300, 450), "General note", layer_xref=review)
    rec(t, 3, "text", pdf_type="FreeText", subject="Text Box", layer="Review", formatted="General note")
    q = add_rectangle(doc, p3, (350, 400, 500, 500), layer_xref=review)
    q_nm = rec(q, 3, "rectangle", pdf_type="Square", subject="Rectangle", layer="Review")
    s2 = add_status(doc, p3, q, "Rejected")
    s2_nm = rec(s2, 3, "status", pdf_type="Text", author=REVIEWER, reply_to=q_nm)
    exp["status"][q_nm] = {"state": "Rejected", "model": "Review", "by": REVIEWER, "id": s2_nm}

    doc.save(path, garbage=0, deflate=True)
    doc.close()
    return exp

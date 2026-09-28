"""Takeoff math and quantity summaries for Bluebeam measurement markups.

Coordinates are PDF points; scale factors come from the markup's /Measure dictionary
(see markups_read.parse_measure). Verified against real Bluebeam files:
    area   = shoelace(points) * X.C^2 * A.C        (X.C = real units per point)
    length = sum(segment lengths) * X.C * D.C      (closed polygons include the closing segment)
    volume = area * depth(in the X unit) * V.C
"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence, Union

from . import core

if TYPE_CHECKING:  # markups_read imports this module, so only import it for typing
    from .markups_read import Markup

Point = Sequence[float]

MISMATCH_TOLERANCE = 0.005          # computed vs Bluebeam's /Contents, relative
KIND_ORDER = ("area", "volume", "perimeter", "length", "count", "angle")
GROUP_FIELDS = ("subject", "layer", "page", "label", "author", "color", "kind")
_LEN_TO_FT = {"ft": 1.0, "in": 1 / 12, "yd": 3.0, "mi": 5280.0, "m": 3.280839895,
              "cm": 0.03280839895, "mm": 0.003280839895, "km": 3280.839895}


# --- geometry --------------------------------------------------------------

def area_pts(points: Sequence[Point]) -> float:
    """Shoelace area of a polygon in square points (always >= 0)."""
    n = len(points)
    if n < 3:
        return 0.0
    s = sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
            for i in range(n))
    return abs(s) / 2.0


def length_pts(points: Sequence[Point], closed: bool = False) -> float:
    """Path length in points; closed=True adds the segment from the last point back to the first."""
    pts = list(points)
    if closed and len(pts) > 2:
        pts.append(pts[0])
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def angle_deg(points: Sequence[Point]) -> Optional[float]:
    """Angle at the middle vertex of a 3-point polyline (a-b-c), in degrees."""
    if len(points) < 3:
        return None
    a, b, c = points[0], points[1], points[2]
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return None
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


# --- reading Bluebeam's formatted strings ---------------------------------

def parse_formatted_quantity(text: Optional[str]) -> Optional[tuple[float, str, float]]:
    """Parse Bluebeam's /Contents value -> (number, unit, rounding step) or None.

    '3,213.27 sf' -> (3213.27, 'sf', 0.01);  81'-8 1/2" -> (81.708.., 'ft', 1/24);  '3' -> (3.0, '', 1);
    '45.00°' -> (45.0, '°', 0.01). Feet-inch strings return feet.
    """
    if not text:
        return None
    s = text.strip()
    if "'" in s or s.endswith('"'):
        neg = s.startswith("-")
        feet_part, _, inch_part = s.lstrip("-").partition("'")
        if '"' in feet_part:               # inches only, e.g. 8 1/2"
            feet_part, inch_part = "", feet_part
        try:
            feet = float(feet_part.replace(",", "")) if feet_part.strip() else 0.0
            inches = 0.0
            for tok in inch_part.replace('"', " ").replace("-", " ").split():
                if "/" in tok:
                    n, d = tok.split("/")
                    inches += float(n) / float(d)
                else:
                    inches += float(tok)
        except ValueError:
            return None
        return ((-1 if neg else 1) * (feet + inches / 12.0), "ft", 1 / 24)
    m = re.fullmatch(r"(-?[\d,]*\.?\d+)\s*(.*)", s)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None
    decimals = len(num.split(".")[1]) if "." in num else 0
    return value, m.group(2).strip(), 10.0 ** -max(decimals, 2 if decimals else 0)


# --- markup values ---------------------------------------------------------

def measure_value(markup: "Markup") -> Optional[float]:
    """Value computed from the markup's geometry and scale, in its measurement unit.

    None when it cannot be computed (no /Measure scale, no vertices). Counts return NumCounts.
    """
    me = markup.measurement
    if not me:
        return None
    kind, pts = me["kind"], markup.vertices
    if kind == "count":
        return me.get("count")
    if kind == "angle":
        return angle_deg(pts or [])
    upp = me.get("units_per_point")
    if not upp or not pts or len(pts) < 2:
        return None
    if kind == "volume":
        return volume_value(markup)
    factor = me.get("unit_factor") or 1.0
    if kind == "area":
        return area_pts(pts) * upp * upp * factor
    return length_pts(pts, closed=markup.pdf_type == "Polygon") * upp * factor


def bluebeam_value(me: dict) -> Optional[float]:
    """Bluebeam's own value (parsed from /Contents) if its unit matches the measurement's unit, else None.

    This is what Revu shows, so it wins over recomputed geometry (which cannot see area cutouts or
    arcs). Feet-inch text counts as feet. Counts are handled elsewhere (NumCounts).
    """
    cv = me.get("contents_value")
    if cv is None or me.get("kind") == "count":
        return None
    norm = lambda u: (u or "").replace(" ", "").lower()                        # noqa: E731
    return cv if me.get("contents_unit") is not None and norm(me["contents_unit"]) == norm(me.get("unit")) else None


def volume_value(markup: "Markup", area: Optional[float] = None) -> Optional[float]:
    """Area x depth x V.C in the /V unit (cu yd for a 6 in slab on a ft drawing), or None.

    area: the markup's area in its own area unit (Bluebeam's value, cutouts included); when omitted
    the area is computed from the vertices. /Depth being in /DepthUnit is unverified against Revu.
    """
    me = markup.measurement
    if not me or me.get("depth") is None:
        return None
    du, xu = me.get("depth_unit"), me.get("x_unit")
    if du not in _LEN_TO_FT or xu not in _LEN_TO_FT:
        return None
    if area is not None:
        area_x = area / (me.get("unit_factor") or 1.0)                          # back to X-unit squared
    else:
        pts = markup.vertices
        if not me.get("units_per_point") or not pts or len(pts) < 3:
            return None
        area_x = area_pts(pts) * me["units_per_point"] ** 2
    depth_x = me["depth"] * _LEN_TO_FT[du] / _LEN_TO_FT[xu]
    return area_x * depth_x * (me.get("volume_factor") or 1.0)


def _quantities(mk: "Markup") -> list[tuple[str, float, str]]:
    """(kind, value, unit) pieces one markup contributes to a takeoff (area+depth gives two)."""
    me = mk.measurement
    if not me:
        return []
    out: list[tuple[str, float, str]] = []
    value = me.get("value")
    if value is not None:
        out.append((me["kind"], value, me.get("unit") or ""))
    if me["kind"] == "area" and me.get("volume") is not None:
        out.append(("volume", me["volume"], me.get("volume_unit") or ""))
    return out


# --- summary ---------------------------------------------------------------

def _group_value(mk: "Markup", field: str) -> Any:
    if field == "kind":
        v = mk.measurement["kind"] if mk.measurement else ""
    elif field in GROUP_FIELDS:
        v = getattr(mk, field)
    else:
        v = mk.custom_columns.get(field, "")
    return v if v not in (None, "") else "(none)"


def _canonical_fields(markups: Sequence["Markup"], group_by: Union[str, Iterable[str]]) -> list[str]:
    custom = {name.lower(): name for mk in markups for name in mk.custom_columns}
    out: list[str] = []
    for f in ([group_by] if isinstance(group_by, str) else group_by):
        low = str(f).strip().lower()
        if low in GROUP_FIELDS:
            out.append(low)
        elif low in custom:
            out.append(custom[low])
        else:
            allowed = ", ".join(list(GROUP_FIELDS) + sorted(custom.values()))
            raise core.DocumentError(f"Unknown group_by field '{f}'. Allowed: {allowed}")
    return out


def _total(kind: str, value: float) -> float:
    return int(round(value)) if kind == "count" else round(value, 2)


def _cap(items: list[str], n: int, what: str) -> list[str]:
    return items if len(items) <= n else items[:n] + [f"... and {len(items) - n} more {what}"]


def summarize(markups: Sequence["Markup"], group_by: Union[str, Iterable[str]] = ("subject",),
              ids_cap: int = 20, warnings_cap: int = 15) -> dict:
    """Group measurement markups into takeoff quantities.

    Returns {group_by, rows, totals, scales, warnings, measured, skipped, notes?}:
      rows    [{group {field: value}, kind, markups (number of markups; count groups for kind=count),
                total (the quantity; for kind=count the number of symbols), unit, pages [1-based], ids [capped]}]
                -- an area markup with a depth adds a volume row (volume_unverified: true)
      totals  [{kind, unit, markups, total}] across all rows
      scales  [{page, scale, units_per_point, markups}] distinct scales per page
      warnings unscaled measurements, several scales on one page, and Bluebeam's own /Contents value
               differing from the geometry by > 0.5% (e.g. area cutouts or arcs the geometry cannot see)
    Quantities are Bluebeam's own values (its /Contents text) whenever that is readable in the
    measurement's unit, else recomputed from geometry x scale. Members of a count group
    (measurement.member_of) are not counted twice: N is per group.
    """
    fields = _canonical_fields(markups, group_by)
    rows: dict[tuple, dict] = {}
    totals: dict[tuple, dict] = {}
    scales: dict[int, dict[tuple, dict]] = {}
    unscaled: list[str] = []
    unmeasured: list[str] = []
    mismatches: list[str] = []
    measured = 0

    for mk in markups:
        me = mk.measurement
        if not me or mk.is_reply or me.get("member_of"):
            continue
        measured += 1
        kind = me["kind"]
        where = f"{mk.subject or mk.pdf_type} p{mk.page} [{mk.id}]"
        if kind in ("area", "length", "perimeter", "volume") and not me.get("units_per_point"):
            unscaled.append(where)
        if me.get("units_per_point"):
            key = (me.get("scale_text") or "", round(me["units_per_point"], 9))
            slot = scales.setdefault(mk.page, {}).setdefault(
                key, {"page": mk.page, "scale": key[0], "units_per_point": key[1], "markups": 0})
            slot["markups"] += 1
        cv, comp = me.get("contents_value"), me.get("computed")
        if me.get("value_source") == "bluebeam" and cv is not None and comp is not None and me.get("contents_step"):
            tol = max(MISMATCH_TOLERANCE * abs(cv), me["contents_step"])
            if abs(comp - cv) > tol and kind in ("area", "length", "perimeter"):
                mismatches.append(f"{where}: geometry gives {comp:,.2f} {me.get('unit', '')} but Bluebeam shows "
                                  f"'{me.get('formatted')}' ({abs(comp - cv) / max(abs(cv), 1e-9):.1%} off, e.g. "
                                  "cutouts or arcs); Bluebeam's value was used")
        pieces = _quantities(mk)
        if not pieces:
            unmeasured.append(where)
            continue
        gkey = tuple(_group_value(mk, f) for f in fields)
        for k, value, unit in pieces:
            for table, tkey, seed in ((rows, (gkey, k, unit), {"group": dict(zip(fields, gkey))}),
                                      (totals, (k, unit), {})):
                r = table.setdefault(tkey, {**seed, "kind": k, "markups": 0, "total": 0.0, "unit": unit,
                                            "pages": set(), "ids": []})
                r["markups"] += 1
                r["total"] += value
                r["pages"].add(mk.page)
                if table is rows:
                    r["ids"].append(mk.id)

    def order(r: dict) -> tuple:
        k = KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else len(KIND_ORDER)
        return (tuple(str(v) for v in r.get("group", {}).values()), k, r["unit"])

    out_rows = []
    for r in sorted(rows.values(), key=order):
        r["total"] = _total(r["kind"], r["total"])
        r["pages"] = sorted(r["pages"])
        if len(r["ids"]) > ids_cap:
            r["ids_truncated"] = len(r["ids"]) - ids_cap
            r["ids"] = r["ids"][:ids_cap]
        if r["kind"] == "volume":
            r["volume_unverified"] = True
        out_rows.append(r)
    out_totals = [{"kind": r["kind"], "unit": r["unit"], "markups": r["markups"], "total": _total(r["kind"], r["total"]),
                   **({"volume_unverified": True} if r["kind"] == "volume" else {})}
                  for r in sorted(totals.values(), key=order)]

    warnings: list[str] = []
    if unscaled:
        warnings.append(f"{len(unscaled)} measurement(s) have no /Measure scale, so no value was computed "
                        f"(Bluebeam's own text was used when readable): " + "; ".join(unscaled[:5]))
    if unmeasured:
        warnings.append(f"{len(unmeasured)} measurement(s) had no readable value and are not in the "
                        "totals: " + "; ".join(unmeasured[:5]))
    for page, per in sorted(scales.items()):
        if len(per) > 1:
            texts = ", ".join(f"'{s['scale']}' ({s['markups']})" for s in per.values())
            warnings.append(f"page {page} mixes {len(per)} scales: {texts}")
    warnings += mismatches
    scale_rows = [s for per in sorted(scales.items()) for s in per[1].values()]
    out = {"group_by": fields, "rows": out_rows, "totals": out_totals, "scales": scale_rows,
           "warnings": _cap(warnings, warnings_cap, "warnings"), "measured": measured,
           "skipped": len(unmeasured)}
    if any(t["kind"] == "volume" for t in out_totals):
        out["notes"] = ["volume = area x depth x V.C, taking /Depth as being in its /DepthUnit; this is unverified "
                        "against Revu, so check one volume there before relying on it"]
    return out

"""Bluebeam-aware, read-only markup parsing (PyMuPDF + a small PDF-object reader).

Everything here works on the annotation dictionaries themselves, so it understands what
PyMuPDF's Annot API hides: /Measure scales, /IT intents, /MeasurementTypes, groups
(/IRT + /RT /Group), replies, review statuses, layers (/OC) and custom columns.

Conventions: pages are 0-based here (core.parse_page_range gives 0-based indices); the tool
layer shows 1-based. Coordinates are PDF points, origin top-left, in the page's UNROTATED space
(PyMuPDF's convention for annotation and text coordinates).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

import pymupdf

from . import core, takeoff

# =============================================================================
# A small reader for PDF object syntax (xref_object() output)
# =============================================================================


class Ref(int):
    """An indirect reference (object number) inside a parsed PDF object."""


_NUM = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")
_REF = re.compile(r"(\d+)\s+\d+\s+R(?![A-Za-z0-9])")
_WS = " \t\r\n\f\x00"
_DELIMS = "()<>[]{}/%"
_ESCAPES = {"n": 10, "r": 13, "t": 9, "b": 8, "f": 12, "(": 40, ")": 41, "\\": 92}


def decode_pdf_text(raw: bytes) -> str:
    """PDF text string bytes -> str (UTF-16 with BOM, UTF-8, else PDFDocEncoding ~ cp1252)."""
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace")
    if raw[:2] == b"\xff\xfe":
        return raw[2:].decode("utf-16-le", "replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


class _Reader:
    """Recursive-descent reader: dict -> dict, array -> list, name/string -> str, n 0 R -> Ref."""

    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def _skip(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in _WS:
                self.i += 1
            elif c == "%":
                while self.i < n and s[self.i] not in "\r\n":
                    self.i += 1
            else:
                break

    def value(self) -> Any:
        self._skip()
        s, i = self.s, self.i
        if i >= len(s):
            return None
        c = s[i]
        if s.startswith("<<", i):
            return self._dict()
        if c == "<":
            return self._hex()
        if c == "(":
            return self._literal()
        if c == "[":
            return self._array()
        if c == "/":
            return self._name()
        if c.isdigit():
            m = _REF.match(s, i)
            if m:
                self.i = m.end()
                return Ref(int(m.group(1)))
        m = _NUM.match(s, i)
        if m:
            self.i = m.end()
            t = m.group(0)
            return float(t) if "." in t else int(t)
        j = i
        while j < len(s) and s[j] not in _WS and s[j] not in _DELIMS:
            j += 1
        if j == i:                       # stray delimiter: skip it so callers always progress
            self.i += 1
            return None
        self.i = j
        return {"true": True, "false": False, "null": None}.get(s[i:j], s[i:j])

    def _name(self) -> str:
        s = self.s
        j = self.i + 1
        while j < len(s) and s[j] not in _WS and s[j] not in _DELIMS:
            j += 1
        name = s[self.i + 1:j]
        self.i = j
        return re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), name)

    def _hex(self) -> str:
        end = self.s.find(">", self.i)
        end = len(self.s) if end < 0 else end
        digits = re.sub(r"[^0-9A-Fa-f]", "", self.s[self.i + 1:end])
        if len(digits) % 2:
            digits += "0"
        self.i = end + 1
        return decode_pdf_text(bytes.fromhex(digits))

    def _literal(self) -> str:
        s, n = self.s, len(self.s)
        i, depth, out = self.i + 1, 1, bytearray()
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                i += 1
                c = s[i]
                if c in _ESCAPES:
                    out.append(_ESCAPES[c])
                elif c in "01234567":
                    j = i
                    while j < n and j < i + 3 and s[j] in "01234567":
                        j += 1
                    out.append(int(s[i:j], 8) & 0xFF)
                    i = j - 1
                elif c in "\r\n":          # line continuation
                    if c == "\r" and s[i + 1:i + 2] == "\n":
                        i += 1
                else:
                    out += c.encode("utf-8")
            elif c == "(":
                depth += 1
                out += b"("
            elif c == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
                out += b")"
            else:
                out += c.encode("latin-1") if ord(c) < 256 else c.encode("utf-8")
            i += 1
        self.i = i
        return decode_pdf_text(bytes(out))

    def _array(self) -> list:
        self.i += 1
        out = []
        while True:
            self._skip()
            if self.i >= len(self.s):
                break
            if self.s[self.i] == "]":
                self.i += 1
                break
            out.append(self.value())
        return out

    def _dict(self) -> dict:
        self.i += 2
        out: dict = {}
        while True:
            self._skip()
            if self.i >= len(self.s):
                break
            if self.s.startswith(">>", self.i):
                self.i += 2
                break
            if self.s[self.i] != "/":
                self.value()               # garbage between entries: skip it
                continue
            key = self._name()
            out[key] = self.value()
        return out


def parse_pdf_object(text: str) -> Any:
    """Parse PDF object syntax (as returned by Document.xref_object) into Python values."""
    return _Reader(text).value()


class _Objects:
    """Cached, parsed access to a document's objects."""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self._cache: dict[int, Any] = {}

    def obj(self, xref: int) -> Any:
        if xref not in self._cache:
            try:
                self._cache[xref] = parse_pdf_object(self.doc.xref_object(xref, compressed=True))
            except Exception:              # unreadable object: behave as if it were absent
                self._cache[xref] = None
        return self._cache[xref]

    def get(self, v: Any) -> Any:
        """Follow a Ref (repeatedly, capped) to the object it points at."""
        for _ in range(8):
            if not isinstance(v, Ref):
                break
            v = self.obj(int(v))
        return v

    def dict_of(self, v: Any) -> dict:
        v = self.get(v)
        return v if isinstance(v, dict) else {}

    def list_of(self, v: Any) -> list:
        v = self.get(v)
        return v if isinstance(v, list) else []

    def text(self, v: Any) -> str:
        """A text value; a Ref to a text stream (rich text /RC can be one) is read too."""
        if isinstance(v, Ref):
            try:
                return decode_pdf_text(self.doc.xref_stream(int(v)) or b"")
            except Exception:
                return ""
        return v if isinstance(v, str) else ""


# =============================================================================
# small value helpers
# =============================================================================

_UNIT_NAMES = {"'": "ft", "′": "ft", '"': "in", "″": "in"}      # foot/inch marks, plain and prime


def _unit(u: Any) -> str:
    """Normalize a /U unit string: ' -> ft, \" -> in, others as written (sf, cu yd, degree sign)."""
    u = u.strip() if isinstance(u, str) else ""
    return _UNIT_NAMES.get(u, u)


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text_or(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _hex_color(arr: Any) -> Optional[str]:
    """/C or /IC array -> '#RRGGBB' (gray, RGB and CMYK; empty array = transparent = None)."""
    if not isinstance(arr, list) or not arr or not all(_num(v) is not None for v in arr):
        return None
    v = [max(0.0, min(1.0, float(x))) for x in arr]
    if len(v) == 1:
        r = g = b = v[0]
    elif len(v) == 3:
        r, g, b = v
    elif len(v) == 4:
        c, m, y, k = v
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return None
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


_DATE = re.compile(r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?(?:([Zz])|([+-])(\d\d)'?(\d\d)?'?)?")


def iso_date(s: Optional[str]) -> Optional[str]:
    """PDF date "D:20260928120000-07'00'" -> '2026-09-28T12:00:00-07:00' (raw text if unparseable)."""
    if not s:
        return None
    m = _DATE.match(s.strip())
    if not m:
        return s
    y, mo, d, h, mi, se = (int(x) if x else dflt for x, dflt in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0)))
    tz = "Z" if m.group(7) else (f"{m.group(8)}{m.group(9)}:{m.group(10) or '00'}" if m.group(8) else "")
    try:
        datetime(y, mo, d, h, mi, se)
    except ValueError:
        return s
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{se:02d}{tz}"


def plain_rich_text(rc: str) -> str:
    """Bluebeam /RC (XHTML) -> plain text: paragraph/line breaks kept, tags and styles dropped."""
    if not rc:
        return ""
    s = re.sub(r"<\?.*?\?>|<style.*?</style>", "", rc, flags=re.S)
    s = re.sub(r"</p>|<br\s*/?>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return "\n".join(line.strip() for line in s.splitlines() if line.strip())


# =============================================================================
# /Measure
# =============================================================================


def _format_entry(res: _Objects, arr: Any) -> Optional[dict]:
    """First NumberFormat of a /X /D /A /V /T array -> {unit, factor}."""
    for item in res.list_of(arr):
        d = res.dict_of(item)
        if d:
            return {"unit": _unit(d.get("U")), "factor": _num(d.get("C")) or 1.0}
    return None


def _measure_dict(res: _Objects, measure: Any) -> Optional[dict]:
    """Normalize a /Measure dictionary (direct or indirect) or return None."""
    m = res.dict_of(measure)
    if not m:
        return None
    x = _format_entry(res, m.get("X"))
    return {"scale_text": res.text(m.get("R")) or None,
            "units_per_point": x["factor"] if x else None,
            "x_unit": x["unit"] if x else None,
            "distance": _format_entry(res, m.get("D")),
            "area": _format_entry(res, m.get("A")),
            "volume": _format_entry(res, m.get("V")),
            "angle": _format_entry(res, m.get("T"))}


def parse_measure(doc: pymupdf.Document, xref: int) -> Optional[dict]:
    """The scale dictionary of the annotation with this xref, or None if it has no /Measure.

    Works for an indirect /Measure (N 0 R) and a direct dict. Returns
    {scale_text, units_per_point (X.C, real units per PDF point), x_unit,
     distance|area|volume|angle: {unit, factor} | None}.
    """
    res = _Objects(doc)
    return _measure_dict(res, res.dict_of(res.obj(xref)).get("Measure"))


# =============================================================================
# Markup
# =============================================================================


@dataclass
class Markup:
    id: str
    xref: int
    page: int                                   # 1-based
    pdf_type: str                               # PDF /Subtype: Polygon, PolyLine, Line, FreeText, ...
    page_label: str = ""
    intent: Optional[str] = None                # /IT: PolygonDimension, PolygonCount, ...
    subject: str = ""
    author: str = ""
    contents: str = ""
    rich_text_plain: str = ""
    label: str = ""
    color: Optional[str] = None
    fill_color: Optional[str] = None
    opacity: Optional[float] = None
    line_width: Optional[float] = None
    cloudy: bool = False
    layer: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    locked: bool = False
    hidden: bool = False
    rect: Optional[list[float]] = None
    vertices: Optional[list[tuple[float, float]]] = None
    group_parent_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    is_reply: bool = False
    status: Optional[dict] = None               # {state, model, by, date}
    measurement: Optional[dict] = None
    custom_columns: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Measurement kind (area, length, ...) or '' for ordinary markups."""
        return self.measurement["kind"] if self.measurement else ""

    def to_dict(self, detail: str = "summary") -> dict:
        """JSON-ready dict. summary = compact (empty fields omitted); full = every field."""
        if detail == "full":
            d = {"id": self.id, "xref": self.xref, "page": self.page, "page_label": self.page_label,
                 "type": self.pdf_type, "intent": self.intent, "subject": self.subject,
                 "author": self.author, "contents": self.contents,
                 "rich_text_plain": self.rich_text_plain if self.rich_text_plain != self.contents else "",
                 "label": self.label, "color": self.color, "fill_color": self.fill_color,
                 "opacity": self.opacity, "line_width": self.line_width, "cloudy": self.cloudy,
                 "layer": self.layer, "created": self.created, "modified": self.modified,
                 "locked": self.locked, "hidden": self.hidden,
                 "rect": _round_list(self.rect), "group_parent_id": self.group_parent_id,
                 "reply_to_id": self.reply_to_id, "is_reply": self.is_reply, "status": self.status,
                 "custom_columns": self.custom_columns}
            if self.vertices:
                d["vertex_count"] = len(self.vertices)
                d["vertices"] = [[round(x, 2), round(y, 2)] for x, y in self.vertices[:60]]
            if self.measurement:
                d["measurement"] = _round_dict(self.measurement)
            return {k: v for k, v in d.items() if v not in (None, "", [], {}, False)}
        d = {"id": self.id, "page": self.page, "type": self.pdf_type}
        for key, val in (("page_label", self.page_label), ("subject", self.subject), ("author", self.author),
                         ("contents", _clip(self.contents or self.rich_text_plain, 300)),
                         ("layer", self.layer), ("label", self.label)):
            if val:
                d[key] = val
        if self.status:
            d["status"] = self.status["state"]
        if self.measurement:
            me = self.measurement
            short = {"kind": me["kind"], "value": _round(me.get("value")), "unit": me.get("unit"),
                     "formatted": me.get("formatted")}
            if me.get("volume") is not None:
                short["volume"] = _round(me["volume"])
                short["volume_unit"] = me.get("volume_unit")
            if me.get("member_of"):
                short["member_of"] = me["member_of"]
            d["measurement"] = {k: v for k, v in short.items() if v not in (None, "")}
        for key, val in (("group_parent_id", self.group_parent_id), ("reply_to_id", self.reply_to_id),
                         ("custom_columns", self.custom_columns)):
            if val:
                d[key] = val
        if self.locked:
            d["locked"] = True
        if self.hidden:
            d["hidden"] = True
        return d


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _round(v: Any, nd: int = 2) -> Any:
    return round(v, nd) if isinstance(v, float) else v


def _round_list(v: Optional[list]) -> Optional[list]:
    return [round(x, 2) for x in v] if v else v


def _round_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if v in (None, ""):
            continue
        out[k] = round(v, 7) if k in ("units_per_point", "volume_factor", "unit_factor") and isinstance(v, float) \
            else _round(v)
    return out


# =============================================================================
# parsing a document
# =============================================================================

_MEASURE_INTENTS = {"PolygonDimension", "PolyLineDimension", "LineDimension", "PolygonCount",
                    "PolyLineAngle", "PolygonVolume"}


def _kind(pdf_type: str, intent: Optional[str], mtypes: Optional[int]) -> Optional[str]:
    """Measurement kind from /IT + /MeasurementTypes (128 count, 129 area, 130 length, 1152 angle)."""
    if intent not in _MEASURE_INTENTS:
        return None
    if intent == "PolygonCount":
        return "count"
    if intent == "PolyLineAngle":
        return "angle"
    if "Volume" in intent:
        return "volume"
    low = (mtypes or 0) & ~128
    if low & 1024:
        return "angle"
    if intent in ("LineDimension", "PolyLineDimension") or pdf_type in ("Line", "PolyLine"):
        return "length"
    # a closed polygon: area (bit 1), or its outline length = perimeter (bit 2 without bit 1)
    return "perimeter" if low & 2 and not low & 1 else "area"


def _measurement(res: _Objects, d: dict, kind: str, contents: str) -> dict:
    """The measurement dict for a markup (value/computed are filled in by the caller)."""
    md = _measure_dict(res, d.get("Measure")) or {}
    me: dict[str, Any] = {"kind": kind, "value": None, "unit": None, "formatted": contents or None,
                          "computed": None, "contents_value": None, "contents_unit": None,
                          "contents_step": None, "value_source": None,
                          "scale_text": md.get("scale_text"), "units_per_point": md.get("units_per_point"),
                          "x_unit": md.get("x_unit"), "unit_factor": None, "depth": None,
                          "depth_unit": None, "volume": None, "volume_unit": None,
                          "volume_factor": None, "count": None}
    fmt = {"area": md.get("area"), "volume": md.get("volume"), "angle": md.get("angle"),
           "length": md.get("distance"), "perimeter": md.get("distance")}.get(kind)
    if fmt:
        me["unit"], me["unit_factor"] = fmt["unit"], fmt["factor"]
    elif kind == "count":
        me["unit"] = "count"
    parsed = takeoff.parse_formatted_quantity(contents)
    if parsed:
        me["contents_value"], cunit, me["contents_step"] = parsed
        me["contents_unit"] = cunit
        if not me["unit"] and kind != "count":
            me["unit"] = cunit
    if kind == "count":
        n = _num(d.get("NumCounts"))
        me["count"] = int(n) if n is not None else (
            int(me["contents_value"]) if me["contents_value"] is not None else None)
        if isinstance(d.get("CountStyle"), str):
            me["count_style"] = d["CountStyle"]
    depth = _num(d.get("Depth"))
    if depth is not None and kind in ("area", "volume"):
        me["depth"] = depth
        unit_fmt = _format_entry(res, d.get("DepthUnit"))
        me["depth_unit"] = unit_fmt["unit"] if unit_fmt else None
        vol = md.get("volume") or {}
        me["volume_unit"], me["volume_factor"] = vol.get("unit"), vol.get("factor")
    for src, dst in (("SlopeType", "slope_type"), ("PitchRun", "pitch_run")):
        v = d.get(src)
        if isinstance(v, (str, int, float)) and v not in ("", 0, 12):      # 0 / 12 = Bluebeam's defaults
            me[dst] = v
    return me


def custom_column_defs(doc: pymupdf.Document) -> list[dict]:
    """Custom column definitions from the catalog: [{name, subtype, order}] in stored order.

    Bluebeam's exact layout is unverified: the catalog /BSIColumnData (or /BSIColumns) array of
    {Name, Subtype, DisplayOrder} dicts is read; anything unexpected yields []. Each markup's own
    /BSIColumnData string array is positional against this list.
    """
    return _column_defs(_Objects(doc))


def _column_defs(res: _Objects) -> list[dict]:
    cat = res.dict_of(res.obj(res.doc.pdf_catalog()))
    for key in ("BSIColumnData", "BSIColumns"):
        defs = [res.dict_of(x) for x in res.list_of(cat.get(key))]
        if defs and all("Name" in x for x in defs):
            return [{"name": res.text(x.get("Name")) or f"Column {i + 1}", "subtype": _text_or(x.get("Subtype")),
                     "order": x.get("DisplayOrder", i)} for i, x in enumerate(defs)]
    return []


def _layer_names(res: _Objects, oc: Any) -> Optional[str]:
    """/OC (an OCG, or an OCMD listing OCGs) -> layer name(s)."""
    d = res.dict_of(oc)
    if not d:
        return None
    if d.get("Type") == "OCMD":
        groups = res.get(d.get("OCGs"))
        groups = groups if isinstance(groups, list) else [d.get("OCGs")]
        names = [res.text(res.dict_of(g).get("Name")) for g in groups]
        return ", ".join(n for n in names if n) or None
    return res.text(d.get("Name")) or None


def _vertices(annot: pymupdf.Annot) -> Optional[list[tuple[float, float]]]:
    try:
        v = annot.vertices
    except Exception:
        return None
    if v and isinstance(v[0], (tuple, list)) and isinstance(v[0][0], (int, float)):
        return [(float(p[0]), float(p[1])) for p in v]
    return None                                     # ink or non-polygon


def parse_markups(doc: pymupdf.Document, pages: Optional[Iterable[int]] = None,
                  include_replies: bool = False) -> list[Markup]:
    """Parse the markups on the given 0-based pages (None = all), in page order.

    Review statuses are attached to their parent (Markup.status, latest wins) and never listed;
    replies (/IRT without /RT /Group) are listed only with include_replies. Group children are
    always listed, with group_parent_id set. Bluebeam counts: every symbol is its own Polygon
    carrying the group's total N; members after the first are grouped to it and carry
    measurement['member_of'] so totals count N once.
    """
    res = _Objects(doc)
    defs = _column_defs(res)
    page_numbers = range(doc.page_count) if pages is None else sorted(set(pages))
    out: list[Markup] = []
    for pno in page_numbers:
        out.extend(_parse_page(doc, res, doc[pno], defs, include_replies))
    return out


def _parse_page(doc: pymupdf.Document, res: _Objects, page: pymupdf.Page, defs: list[dict],
                include_replies: bool) -> list[Markup]:
    label = core.decode_label(page.get_label())
    annots = list(page.annots())
    ids = {a.xref: core.markup_id(doc, a) for a in annots}
    statuses: dict[int, list[dict]] = {}
    markups: list[Markup] = []
    for annot in annots:
        d = res.dict_of(res.obj(annot.xref))
        parent_ref = d.get("IRT")
        parent = int(parent_ref) if isinstance(parent_ref, Ref) and int(parent_ref) in ids else None
        if parent is not None and (d.get("State") or d.get("StateModel")):
            statuses.setdefault(parent, []).append(
                {"state": _text_or(d.get("State")), "model": _text_or(d.get("StateModel")),
                 "by": res.text(d.get("T")), "date": iso_date(res.text(d.get("CreationDate")) or res.text(d.get("M")))})
            continue
        is_group_child = parent is not None and d.get("RT") == "Group"
        is_reply = parent is not None and not is_group_child
        if is_reply and not include_replies:
            continue
        markups.append(_build_markup(res, page, annot, d, ids, label, defs, parent, is_group_child, is_reply))
    by_xref = {m.xref: m for m in markups}
    for parent, sts in statuses.items():
        if parent in by_xref:
            by_xref[parent].status = max(sts, key=lambda s: s["date"] or "")
    by_id = {m.id: m for m in markups}
    for m in markups:                                # count symbols grouped under a count markup
        parent_mk = by_id.get(m.group_parent_id or "")
        if m.kind == "count" and parent_mk is not None and parent_mk.kind == "count":
            m.measurement["member_of"] = parent_mk.id
    return markups


def _build_markup(res: _Objects, page: pymupdf.Page, annot: pymupdf.Annot, d: dict, ids: dict[int, str],
                  page_label: str, defs: list[dict], parent: Optional[int], is_group_child: bool,
                  is_reply: bool) -> Markup:
    pdf_type = annot.type[1]
    intent = _text_or(d.get("IT")) or None
    contents = res.text(d.get("Contents"))
    flags = int(d["F"]) if _num(d.get("F")) is not None else 0
    bs = res.dict_of(d.get("BS"))
    border = res.list_of(d.get("Border"))
    width = _num(bs.get("W")) if bs else (_num(border[2]) if len(border) > 2 else None)
    r = annot.rect
    mk = Markup(
        id=ids[annot.xref], xref=annot.xref, page=page.number + 1, pdf_type=pdf_type, page_label=page_label,
        intent=intent, subject=res.text(d.get("Subj")), author=res.text(d.get("T")), contents=contents,
        rich_text_plain=plain_rich_text(res.text(d.get("RC"))), label=res.text(d.get("Label")),
        color=_hex_color(res.list_of(d.get("C"))), fill_color=_hex_color(res.list_of(d.get("IC"))),
        opacity=_num(d.get("CA")), line_width=width,
        cloudy=res.dict_of(d.get("BE")).get("S") == "C" or intent == "PolygonCloud",
        layer=_layer_names(res, d.get("OC")),
        created=iso_date(res.text(d.get("CreationDate"))), modified=iso_date(res.text(d.get("M"))),
        locked=bool(flags & 128), hidden=bool(flags & (2 | 32)),
        rect=[r.x0, r.y0, r.x1, r.y1], vertices=_vertices(annot),
        group_parent_id=ids.get(parent) if is_group_child else None,
        reply_to_id=ids.get(parent) if is_reply else None, is_reply=is_reply)
    data = [res.text(v) for v in res.list_of(d.get("BSIColumnData"))]
    if data:
        names = [c["name"] for c in defs] + [f"Column {i + 1}" for i in range(len(defs), len(data))]
        mk.custom_columns = {n: v for n, v in zip(names, data) if v != ""}
    mtypes = _num(d.get("MeasurementTypes"))
    kind = _kind(pdf_type, intent, int(mtypes) if mtypes is not None else None)
    if kind:
        me = mk.measurement = _measurement(res, d, kind, contents)
        me["computed"] = takeoff.measure_value(mk)              # geometry x scale: the cross-check
        if kind == "count":
            me["value"] = me["count"]
        else:
            # Bluebeam's own text is what Revu shows (cutouts, arcs...), so it wins when its unit fits
            bb = takeoff.bluebeam_value(me)
            me["value"] = bb if bb is not None else me["computed"]
            me["value_source"] = "bluebeam" if bb is not None else ("computed" if me["computed"] is not None else None)
            if kind == "area":
                me["volume"] = takeoff.volume_value(mk, area=me["value"])
            elif kind == "volume":
                me["volume"] = me["value"]
    return mk


# =============================================================================
# raw keys, scales, layers
# =============================================================================


def raw_keys(doc: pymupdf.Document, xref: int, max_len: int = 160) -> dict[str, str]:
    """The annotation dictionary's keys with short text values (for inspecting unknown structures)."""
    res = _Objects(doc)
    return {key: _clip(_pdf_repr(val), max_len) for key, val in res.dict_of(res.obj(xref)).items()}


def _pdf_repr(v: Any) -> str:
    """Compact PDF-like text for a parsed value (refs as 'N 0 R', names and strings unquoted)."""
    if isinstance(v, Ref):
        return f"{int(v)} 0 R"
    if isinstance(v, dict):
        return "<<" + " ".join(f"/{k} {_pdf_repr(x)}" for k, x in v.items()) + ">>"
    if isinstance(v, list):
        return "[" + " ".join(_pdf_repr(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return f"{v:g}" if isinstance(v, float) else str(v)


def page_scales(doc: pymupdf.Document, pno: int, markups: Optional[list[Markup]] = None) -> list[dict]:
    """Drawing scales on a 0-based page, from its /VP viewports and its measurement markups.

    [{source: 'viewport'|'markup', scale, units_per_point, unit, markups (markup source only), bbox?}]
    Pass already parsed markups of the document to avoid parsing the page again.
    """
    res = _Objects(doc)
    page = doc[pno]
    out: list[dict] = []
    for vp in res.list_of(res.dict_of(res.obj(page.xref)).get("VP")):
        vd = res.dict_of(vp)
        md = _measure_dict(res, vd.get("Measure"))
        if md and md["units_per_point"]:
            bbox = [float(v) for v in res.list_of(vd.get("BBox")) if _num(v) is not None]
            out.append({"source": "viewport", "scale": (md["scale_text"] or "").strip(),
                        "units_per_point": md["units_per_point"],
                        "unit": md["x_unit"], "bbox": _round_list(bbox)})
    if markups is None:
        markups = parse_markups(doc, pages=[pno])
    seen: dict[tuple, dict] = {}
    for mk in markups:
        me = mk.measurement
        if mk.page != pno + 1 or not me or not me.get("units_per_point") or me.get("member_of"):
            continue
        key = ((me.get("scale_text") or "").strip(), round(me["units_per_point"], 9))
        if key not in seen:
            seen[key] = {"source": "markup", "scale": key[0], "units_per_point": key[1],
                         "unit": me.get("x_unit"), "markups": 0}
            out.append(seen[key])
        seen[key]["markups"] += 1
    for s in out:
        s["units_per_point"] = round(s["units_per_point"], 7)
    return [{k: v for k, v in s.items() if v not in (None, "", [])} for s in out]


def list_layers(doc: pymupdf.Document, markups: Optional[list[Markup]] = None) -> list[dict]:
    """Layers (OCGs): [{name, xref, visible, locked, markup_count}] in the document's order.

    visible follows the default configuration (/OCProperties /D: BaseState, ON, OFF); locked its
    /Locked list. markup_count counts markups whose /OC names the layer.
    """
    res = _Objects(doc)
    props = res.dict_of(res.dict_of(res.obj(doc.pdf_catalog())).get("OCProperties"))
    cfg = res.dict_of(props.get("D"))

    def refs(key: str) -> set[int]:
        return {int(r) for r in res.list_of(cfg.get(key)) if isinstance(r, Ref)}

    on, off, locked = refs("ON"), refs("OFF"), refs("Locked")
    base_off = cfg.get("BaseState") == "OFF"
    if markups is None:
        markups = parse_markups(doc, include_replies=True)
    counts: dict[str, int] = {}
    for mk in markups:
        if mk.layer:
            counts[mk.layer] = counts.get(mk.layer, 0) + 1
    layers = []
    for ref in res.list_of(props.get("OCGs")):
        if not isinstance(ref, Ref):
            continue
        name = res.text(res.dict_of(ref).get("Name"))
        visible = (int(ref) in on) if base_off else (int(ref) not in off)
        layers.append({"name": name, "xref": int(ref), "visible": visible,
                       "locked": int(ref) in locked, "markup_count": counts.get(name, 0)})
    return layers

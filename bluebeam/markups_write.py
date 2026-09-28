"""Write Bluebeam-style markups into PDFs with PyMuPDF (no Revu needed).

Plain functions that take an open pymupdf.Document (from core.open_for_write) and return
JSON-friendly dicts; tools_write.py wraps them as MCP tools. Everything written follows the
structure Bluebeam Revu itself writes (see tests/bb_synth.py): /NM GUIDs, /Subj, /T, dates,
/C stroke + /IC fill, /IT intents, Cloud+ groups, /Measure RL dictionaries, replies and
review statuses, layers as optional-content groups.

Coordinates everywhere: PDF points (1/72 in), origin TOP-LEFT of the page, page numbers 1-based.
"""
from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

import pymupdf

from bluebeam import core, markups_read
from bluebeam.core import DocumentError

Point = tuple[float, float]

DEFAULT_AUTHOR = "Claude"

# --------------------------------------------------------------------------------------
# small value helpers
# --------------------------------------------------------------------------------------

_COLORS = {
    "red": "#FF0000", "blue": "#0000FF", "green": "#00A000", "yellow": "#FFFF00",
    "orange": "#FFA500", "magenta": "#FF00FF", "black": "#000000", "white": "#FFFFFF",
    "gray": "#808080", "grey": "#808080", "cyan": "#00FFFF",
}


def parse_color(value: Any, allow_none: bool = False) -> Optional[tuple[float, float, float]]:
    """'#RRGGBB' / '#RGB' / a color name / (r, g, b) floats 0..1 -> (r, g, b) floats.

    None, '' and 'none' mean 'no color' and return None when allow_none, else DocumentError.
    """
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "null", "transparent")):
        if allow_none:
            return None
        raise DocumentError("A color is required (e.g. '#FF0000' or 'red')")
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in value):
            return (float(value[0]), float(value[1]), float(value[2]))
        raise DocumentError(f"Bad color {value!r}: give three numbers between 0 and 1")
    s = str(value).strip().lower()
    s = _COLORS.get(s, s).lower()
    m = re.fullmatch(r"#?([0-9a-f]{6}|[0-9a-f]{3})", s)
    if not m:
        raise DocumentError(f"Bad color {value!r}: use '#RRGGBB' or one of {', '.join(sorted(_COLORS))}")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(round(int(h[i:i + 2], 16) / 255, 4) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(v * 255):02X}" for v in rgb)


def _num(v: float) -> str:
    """A PDF number the way Bluebeam writes them: 7 significant digits, no leading zero (.4166667)."""
    s = f"{float(v):.7g}"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def _rgb_array(rgb: Sequence[float]) -> str:
    return "[" + " ".join(_num(v) for v in rgb) + "]"


def _pdf_date(now: Optional[datetime] = None) -> str:
    """D:20260928120000-07'00' (local time with its UTC offset, like Revu)."""
    now = (now or datetime.now()).astimezone()
    minutes = int((now.utcoffset() or timedelta(0)).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hh, mm = divmod(abs(minutes), 60)
    return f"D:{now:%Y%m%d%H%M%S}{sign}{hh:02d}'{mm:02d}'"


def _set_str(doc: pymupdf.Document, xref: int, key: str, value: str) -> None:
    doc.xref_set_key(xref, key, pymupdf.get_pdf_str(value))


def _del_key(doc: pymupdf.Document, xref: int, key: str) -> None:
    """Remove a dictionary key (xref_set_key(..., 'null') would leave '/Key null' behind)."""
    try:
        pdf = pymupdf.mupdf.pdf_specifics(doc.this)
        obj = pymupdf.mupdf.pdf_load_object(pdf, xref)
        pymupdf.mupdf.pdf_dict_dels(obj, key)
        pymupdf.mupdf.pdf_dirty_obj(obj)
    except Exception:                           # low-level API changed: fall back to an explicit null
        doc.xref_set_key(xref, key, "null")


def _put_key(doc: pymupdf.Document, xref: int, key: str, value: Optional[str]) -> None:
    """Set a key to raw PDF source, or remove it when value is None."""
    if value is None:
        _del_key(doc, xref, key)
    else:
        doc.xref_set_key(xref, key, value)


def _check_opacity(opacity: float) -> float:
    if not isinstance(opacity, (int, float)) or not 0 <= opacity <= 1:
        raise DocumentError(f"opacity must be between 0 and 1, got {opacity!r}")
    return float(opacity)


def _check_width(width: float) -> float:
    if not isinstance(width, (int, float)) or width < 0 or width > 100:
        raise DocumentError(f"line_width must be between 0 and 100, got {width!r}")
    return float(width)


def _choice(value: str, options: Sequence[str], name: str) -> str:
    v = str(value).strip().lower()
    if v not in options:
        raise DocumentError(f"{name} must be one of {', '.join(options)}; got {value!r}")
    return v


def _rect_arg(rect: Any, name: str = "rect") -> pymupdf.Rect:
    try:
        vals = [float(v) for v in rect]
    except (TypeError, ValueError):
        vals = []
    if len(vals) != 4:
        raise DocumentError(f"{name} must be [x0, y0, x1, y1] in PDF points (origin top-left)")
    r = pymupdf.Rect(*vals).normalize()
    if r.is_empty or r.is_infinite:
        raise DocumentError(f"{name} {list(rect)} is empty")
    return r


def _point_arg(pt: Any, name: str = "point") -> pymupdf.Point:
    try:
        x, y = (float(v) for v in pt)
    except (TypeError, ValueError):
        raise DocumentError(f"{name} must be [x, y] in PDF points (origin top-left)") from None
    return pymupdf.Point(x, y)


def _points_arg(points: Any, minimum: int, name: str = "points") -> list[pymupdf.Point]:
    if not isinstance(points, (list, tuple)) or len(points) < minimum:
        raise DocumentError(f"{name} needs at least {minimum} [x, y] points")
    return [_point_arg(p, f"{name}[{i}]") for i, p in enumerate(points)]


def _bbox(points: Sequence[pymupdf.Point]) -> pymupdf.Rect:
    return pymupdf.Rect(min(p.x for p in points), min(p.y for p in points),
                        max(p.x for p in points), max(p.y for p in points))


def _page_rect(page: pymupdf.Page) -> pymupdf.Rect:
    """The page in the space annotations are written in: the UNROTATED page, origin top-left
    (the space PyMuPDF text search reports); page.rect itself is rotated."""
    return (page.rect * page.derotation_matrix).normalize()


def _check_on_page(page: pymupdf.Page, box: pymupdf.Rect) -> None:
    pr = _page_rect(page)
    if box.x1 < pr.x0 or box.x0 > pr.x1 or box.y1 < pr.y0 or box.y0 > pr.y1:
        raise DocumentError(
            f"Coordinates {[round(v, 1) for v in box]} are outside page {page.number + 1}, which is "
            f"{pr.width:.0f} x {pr.height:.0f} points (origin top-left). Use PDF points, not pixels.")


def _info(doc: pymupdf.Document, pno: int, annot: pymupdf.Annot) -> dict:
    return {"markup_id": core.markup_id(doc, annot), "page": pno + 1, "type": annot.type[1],
            "rect": [round(v, 2) for v in annot.rect]}


# --------------------------------------------------------------------------------------
# raw annotation dictionary helpers
# --------------------------------------------------------------------------------------

def _ref(doc: pymupdf.Document, xref: int, key: str) -> Optional[int]:
    kind, val = doc.xref_get_key(xref, key)
    if kind == "xref":
        return int(val.split()[0])
    return None


def _name(doc: pymupdf.Document, xref: int, key: str) -> str:
    kind, val = doc.xref_get_key(xref, key)
    return val if kind == "name" else ""


_Kids = dict[int, list[tuple[int, str, bool]]]


def _children_index(doc: pymupdf.Document, page: pymupdf.Page) -> _Kids:
    """{parent xref: [(child xref, /RT name, is review status)]}: every annotation on the page that
    answers another one (/IRT)."""
    index: _Kids = {}
    for a in page.annot_xrefs():
        irt = _ref(doc, a[0], "IRT")
        if irt is not None:
            is_status = doc.xref_get_key(a[0], "State")[0] != "null"
            index.setdefault(irt, []).append((a[0], _name(doc, a[0], "RT"), is_status))
    return index


def _descendants(index: _Kids, root: int, replies: bool, statuses: bool = False) -> tuple[list[int], list[int]]:
    """Annots answering `root` (breadth first): group members always, review statuses when `statuses`,
    other replies when `replies`. Returns (members in discovery order, replies left out)."""
    found: list[int] = []
    skipped: list[int] = []
    queue = [root]
    while queue:
        for x, rt, is_status in index.get(queue.pop(0), ()):
            if x == root or x in found:
                continue
            if rt == "/Group" or replies or (statuses and is_status):
                found.append(x)
                queue.append(x)
            else:
                skipped.append(x)
    return found, skipped


def _find_many(doc: pymupdf.Document, ids: Sequence[str]) -> list[tuple[pymupdf.Page, pymupdf.Annot]]:
    """core.find_annot for a list of ids in ONE pass over the document (same id forms, same error).

    Returns (page, annot) per id in the order given; keep the page referenced while using the annot.
    """
    if isinstance(ids, str):
        ids = [ids]
    by_xref: dict[int, str] = {}
    by_nm: dict[str, str] = {}
    for mid in ids:
        m = str(mid).strip()
        if m.lower().startswith("xref:") and m[5:].isdigit():
            by_xref[int(m[5:])] = m
        elif m.isdigit():
            by_xref[int(m)] = m
        else:
            by_nm[m] = m
    found: dict[str, tuple[pymupdf.Page, pymupdf.Annot]] = {}
    for pno in range(doc.page_count):
        if len(found) == len(by_xref) + len(by_nm):
            break
        page = doc[pno]
        for annot in page.annots():
            key = by_xref.get(annot.xref) or by_nm.get(core.markup_id(doc, annot))
            if key is not None and key not in found:
                found[key] = (page, annot)
    for mid in ids:
        if str(mid).strip() not in found:
            raise DocumentError(f"Markup not found: {str(mid).strip()}")
    return [found[str(mid).strip()] for mid in ids]


def _flags_set(annot: pymupdf.Annot, bit: int, on: bool) -> None:
    flags = annot.flags
    annot.set_flags(flags | bit if on else flags & ~bit)


def _refuse_locked(doc: pymupdf.Document, targets: Sequence[tuple[pymupdf.Page, pymupdf.Annot]], action: str) -> None:
    """A locked markup (/F Locked or LockedContents) is left alone until it is unlocked explicitly."""
    locked = [core.markup_id(doc, a) for _, a in targets
              if a.flags & (pymupdf.PDF_ANNOT_IS_LOCKED | pymupdf.PDF_ANNOT_IS_LOCKED_CONTENTS)]
    if locked:
        raise DocumentError(f"Refusing to {action} locked markup(s) {locked}. Unlock them first with "
                            "bb_edit_markups changes={'locked': false}")


def _touch(doc: pymupdf.Document, xref: int) -> None:
    _set_str(doc, xref, "M", _pdf_date())


# --------------------------------------------------------------------------------------
# layers (optional content groups)
# --------------------------------------------------------------------------------------

def find_layer(doc: pymupdf.Document, name: str) -> Optional[int]:
    """xref of the layer called `name` (exact match first, then case/space-insensitive), or None."""
    wanted = str(name).strip()
    ocgs = doc.get_ocgs()
    for xref, info in ocgs.items():
        if info["name"] == wanted:
            return xref
    for xref, info in ocgs.items():
        if info["name"].strip().lower() == wanted.lower():
            return xref
    return None


def layer_xref(doc: pymupdf.Document, name: str, create: bool = True) -> int:
    """xref of layer `name`; creates it (visible) when missing and `create`, else DocumentError."""
    if not str(name).strip():
        raise DocumentError("Layer name is empty")
    xref = find_layer(doc, name)
    if xref is not None:
        return xref
    if not create:
        raise DocumentError(f"Layer not found: {name!r}. Layers: {layer_names(doc)}")
    return doc.add_ocg(str(name).strip(), on=1)


def layer_names(doc: pymupdf.Document) -> list[str]:
    return [i["name"] for i in doc.get_ocgs().values()]


def layer_is_visible(doc: pymupdf.Document, xref: int) -> bool:
    cfg = doc.get_layer(-1) or {}
    if xref in cfg.get("off", []):
        return False
    if str(cfg.get("basestate", "ON")).upper() == "OFF":
        return xref in cfg.get("on", [])
    return True


def add_layer(doc: pymupdf.Document, name: str, visible: bool = True) -> dict:
    """Create layer `name`. Returns {"layer", "created", "visible"}; created False = already there."""
    xref = find_layer(doc, name)
    if xref is not None:
        return {"layer": doc.get_ocgs()[xref]["name"], "created": False, "visible": layer_is_visible(doc, xref)}
    if not str(name).strip():
        raise DocumentError("Layer name is empty")
    doc.add_ocg(str(name).strip(), on=1 if visible else 0)
    return {"layer": str(name).strip(), "created": True, "visible": bool(visible)}


def set_layer_visibility(doc: pymupdf.Document, name: str, visible: bool) -> dict:
    """Show or hide a layer in the default view. Returns {"layer", "visible", "changed"}."""
    xref = layer_xref(doc, name, create=False)
    real = doc.get_ocgs()[xref]["name"]
    if layer_is_visible(doc, xref) == bool(visible):
        return {"layer": real, "visible": bool(visible), "changed": False}
    cfg = doc.get_layer(-1) or {}
    on = [x for x in cfg.get("on", []) if x != xref]
    off = [x for x in cfg.get("off", []) if x != xref]
    (on if visible else off).append(xref)
    doc.set_layer(-1, on=on, off=off)
    return {"layer": real, "visible": bool(visible), "changed": True}


# --------------------------------------------------------------------------------------
# common tagging + styling
# --------------------------------------------------------------------------------------

def _ds(font_size: float, color: Sequence[float]) -> str:
    return (f"font: Helvetica {font_size:g}pt; text-align:left; line-height:{font_size * 1.15:.1f}pt; "
            f"color:{_hex(color)}")


_DEFAULT_DS = "font: Helvetica 12pt; text-align:left; line-height:13.8pt; color:#000000"


def _rich_text(text: str) -> str:
    paras = "".join(f"<p>{html.escape(line, quote=False)}</p>" for line in text.split("\n"))
    return ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
            'xfa:APIVersion="BluebeamPDFRevu:21.11" xfa:spec="2.0.2">' + paras + "</body>")


def _tag(doc: pymupdf.Document, annot: pymupdf.Annot, *, subject: str, author: str,
         contents: Optional[str] = None, layer: Optional[str] = None, ds: str = _DEFAULT_DS) -> str:
    """The keys every Bluebeam markup carries: /NM, /T, /Subj, dates, /DS, optional /Contents and /OC."""
    x = annot.xref
    nm = core.new_markup_id()
    now = _pdf_date()
    _set_str(doc, x, "NM", nm)
    _set_str(doc, x, "T", author or DEFAULT_AUTHOR)
    _set_str(doc, x, "Subj", subject)
    _set_str(doc, x, "CreationDate", now)
    _set_str(doc, x, "M", now)
    if contents is not None:
        _set_str(doc, x, "Contents", contents)
    if layer:
        annot.set_oc(layer_xref(doc, layer))
    _set_str(doc, x, "DS", ds)
    return nm


def _style(annot: pymupdf.Annot, color: Sequence[float], fill: Optional[Sequence[float]],
           opacity: float, width: float, clouds: int = -1) -> None:
    """Stroke/fill/opacity/border through the PyMuPDF API, then let it draw the appearance stream."""
    annot.set_colors(stroke=color, fill=fill)
    annot.set_opacity(opacity)
    annot.set_border(width=width, clouds=clouds)
    annot.update()


def _group(doc: pymupdf.Document, child: int, parent: int) -> None:
    """Bluebeam grouping: child /IRT parent + /RT /Group."""
    doc.xref_set_key(child, "IRT", f"{parent} 0 R")
    doc.xref_set_key(child, "RT", "/Group")


# --------------------------------------------------------------------------------------
# text markups: box, callout, note
# --------------------------------------------------------------------------------------

def _wrap(text: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if not cur or pymupdf.get_text_length(trial, fontname="helv", fontsize=font_size) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _auto_size(text: str, font_size: float, max_width: float = 240.0) -> tuple[float, float]:
    lines = _wrap(text, font_size, max_width - 12)
    width = max(pymupdf.get_text_length(ln, fontname="helv", fontsize=font_size) for ln in lines) + 14
    return max(width, 40.0), len(lines) * font_size * 1.25 + 10


def _callout_points(box: pymupdf.Rect, tip: pymupdf.Point) -> list[pymupdf.Point]:
    """[tip, knee, edge]: the leader runs from the tip to the middle of the nearest box side."""
    if box.contains(tip):
        raise DocumentError("leader_point must be outside the text box (it is the arrow tip)")
    mx, my = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    if tip.x < box.x0:
        edge = pymupdf.Point(box.x0, my)
    elif tip.x > box.x1:
        edge = pymupdf.Point(box.x1, my)
    elif tip.y < box.y0:
        edge = pymupdf.Point(mx, box.y0)
    else:
        edge = pymupdf.Point(mx, box.y1)
    knee = pymupdf.Point((tip.x + edge.x) / 2, (tip.y + edge.y) / 2)
    return [tip, knee, edge]


def _make_freetext(doc: pymupdf.Document, page: pymupdf.Page, box: pymupdf.Rect, text: str, *,
                   font_size: float, color: Sequence[float], fill: Optional[Sequence[float]],
                   opacity: float, width: float, leader: Optional[list[pymupdf.Point]]) -> pymupdf.Annot:
    """FreeText (plain, or callout when `leader`) written the Bluebeam way.

    MuPDF draws /C as the background and the border in the text color, so the appearance is
    generated that way; the dictionary is then rewritten as Bluebeam keeps it (/C stroke, /IC fill).
    """
    annot = page.add_freetext_annot(box, text, fontsize=font_size, text_color=color, fill_color=fill,
                                    border_width=width, opacity=opacity, callout=leader)
    annot.update()
    x = annot.xref
    doc.xref_set_key(x, "C", _rgb_array(color))
    _put_key(doc, x, "IC", _rgb_array(fill) if fill else None)
    if not leader:
        _del_key(doc, x, "CL")              # MuPDF leaves a stray leader line on plain boxes
    _set_str(doc, x, "RC", _rich_text(text))
    return annot


def add_text(doc: pymupdf.Document, page: int, text: str, *, kind: str = "box",
             rect: Any = None, point: Any = None, leader_point: Any = None, font_size: float = 12,
             color: Any = "#FF0000", fill_color: Any = None, opacity: float = 1.0,
             line_width: Optional[float] = None, subject: Optional[str] = None,
             author: str = DEFAULT_AUTHOR, layer: Optional[str] = None) -> dict:
    """Add a text box, callout or sticky note (see tools_write.bb_add_text)."""
    pno = core.page_index(doc, page)
    pg = doc[pno]
    kind = _choice(kind, ("box", "callout", "note"), "kind")
    if not str(text).strip():
        raise DocumentError("text is empty")
    if not font_size or font_size <= 0 or font_size > 200:
        raise DocumentError(f"font_size must be between 1 and 200, got {font_size!r}")
    rgb, fill, opacity = parse_color(color), parse_color(fill_color, True), _check_opacity(opacity)
    width = _check_width(line_width) if line_width is not None else (1.0 if kind == "callout" else 0.0)

    if kind == "note":
        pt = _point_arg(point) if point is not None else (_rect_arg(rect).tl if rect is not None else None)
        if pt is None:
            raise DocumentError("A note needs point=[x, y] (where the note icon goes)")
        _check_on_page(pg, pymupdf.Rect(pt, pt))
        annot = pg.add_text_annot(pt, text, icon="Note")
        annot.set_colors(stroke=rgb)
        annot.set_opacity(opacity)
        annot.update()
        _tag(doc, annot, subject=subject or "Note", author=author, layer=layer)
        return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)]}

    if rect is not None:
        box = _rect_arg(rect)
    elif point is not None:
        w, h = _auto_size(text, font_size)
        p = _point_arg(point)
        box = pymupdf.Rect(p.x, p.y, p.x + w, p.y + h)
    else:
        raise DocumentError(f"A {kind} needs rect=[x0, y0, x1, y1] (or point=[x, y] to auto-size the box)")
    leader = None
    if kind == "callout":
        if leader_point is None:
            raise DocumentError("A callout needs leader_point=[x, y], the tip of its leader arrow")
        tip = _point_arg(leader_point, "leader_point")
        leader = _callout_points(box, tip)
        _check_on_page(pg, box | tip)
    else:
        _check_on_page(pg, box)
    annot = _make_freetext(doc, pg, box, text, font_size=font_size, color=rgb, fill=fill,
                           opacity=opacity, width=width, leader=leader)
    _tag(doc, annot, subject=subject or ("Callout" if leader else "Text Box"), author=author,
         layer=layer, ds=_ds(font_size, rgb))
    if leader:
        doc.xref_set_key(annot.xref, "IT", "/FreeTextCallout")
    return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)]}


# --------------------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------------------

_SHAPES = ("rectangle", "ellipse", "cloud", "polygon", "polyline", "line", "arrow")


def _nearest_on_path(points: Sequence[pymupdf.Point], target: pymupdf.Point, closed: bool = True) -> pymupdf.Point:
    """Closest point to `target` on the outline through `points`."""
    best, best_d = points[0], math.inf
    n = len(points)
    for i in range(n if closed else n - 1):
        a, b = points[i], points[(i + 1) % n]
        dx, dy = b.x - a.x, b.y - a.y
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((target.x - a.x) * dx + (target.y - a.y) * dy) / seg))
        cand = pymupdf.Point(a.x + t * dx, a.y + t * dy)
        d = math.dist(cand, target)
        if d < best_d:
            best, best_d = cand, d
    return best


def _place_cloud_text(page: pymupdf.Page, outline: Sequence[pymupdf.Point], text: str,
                      font_size: float) -> tuple[pymupdf.Rect, pymupdf.Point]:
    """A text box next to the cloud (right, else left, above, below) and the leader tip on the cloud."""
    bounds, pr = _bbox(outline), _page_rect(page)
    w, h = _auto_size(text, font_size)
    gap, edge = 30.0, 10.0
    cands = [pymupdf.Rect(bounds.x1 + gap, bounds.y0, bounds.x1 + gap + w, bounds.y0 + h),
             pymupdf.Rect(bounds.x0 - gap - w, bounds.y0, bounds.x0 - gap, bounds.y0 + h),
             pymupdf.Rect(bounds.x0, bounds.y0 - gap - h, bounds.x0 + w, bounds.y0 - gap),
             pymupdf.Rect(bounds.x0, bounds.y1 + gap, bounds.x0 + w, bounds.y1 + gap + h)]
    inner = pymupdf.Rect(pr.x0 + edge, pr.y0 + edge, pr.x1 - edge, pr.y1 - edge)
    box = next((c for c in cands if c in inner), cands[0])
    if box not in inner:  # nothing fits cleanly: keep the first choice but pull it onto the page
        dx = max(inner.x0 - box.x0, 0) + min(inner.x1 - box.x1, 0)
        dy = max(inner.y0 - box.y0, 0) + min(inner.y1 - box.y1, 0)
        box = box + (dx, dy, dx, dy)
    tip = _nearest_on_path(outline, pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2))
    return box, tip


def add_shape(doc: pymupdf.Document, page: int, *, kind: str = "rectangle", rect: Any = None,
              points: Any = None, text: Optional[str] = None, text_rect: Any = None, cloudy: bool = False,
              color: Any = "#FF0000", fill_color: Any = None, opacity: float = 1.0, line_width: float = 1.5,
              font_size: float = 12, subject: Optional[str] = None, author: str = DEFAULT_AUTHOR,
              comment: Optional[str] = None, layer: Optional[str] = None) -> dict:
    """Add a rectangle, ellipse, Cloud+, polygon, polyline, line or arrow (see tools_write.bb_add_shape)."""
    pno = core.page_index(doc, page)
    pg = doc[pno]
    kind = _choice(kind, _SHAPES, "kind")
    rgb, fill, opacity = parse_color(color), parse_color(fill_color, True), _check_opacity(opacity)
    width = _check_width(line_width)
    author = author or DEFAULT_AUTHOR
    if text is not None and kind != "cloud":
        raise DocumentError("text is only used with kind='cloud' (the Cloud+ text). Use comment for the "
                            "markup's comment, or bb_add_text for a text box")
    clouds = 2 if cloudy else -1

    if kind in ("rectangle", "ellipse"):
        if rect is not None:
            box = _rect_arg(rect)
        elif points is not None:
            box = _bbox(_points_arg(points, 2))
        else:
            raise DocumentError(f"kind='{kind}' needs rect=[x0, y0, x1, y1]")
        _check_on_page(pg, box)
        annot = pg.add_rect_annot(box) if kind == "rectangle" else pg.add_circle_annot(box)
        _style(annot, rgb, fill, opacity, width, clouds)
        _tag(doc, annot, subject=subject or kind.capitalize(), author=author, contents=comment, layer=layer)
        return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)]}

    if kind == "cloud":
        if rect is not None:
            r = _rect_arg(rect)
            pts = [r.tl, r.tr, r.br, r.bl]
        elif points is not None:
            pts = _points_arg(points, 3)
        else:
            raise DocumentError("kind='cloud' needs rect=[x0, y0, x1, y1] or points=[[x, y], ...]")
        _check_on_page(pg, _bbox(pts))
        ids: list[str] = []
        parent = None
        if text is not None and str(text).strip():
            if text_rect is not None:
                box = _rect_arg(text_rect, "text_rect")
                tip = _nearest_on_path(pts, pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2))
            else:
                box, tip = _place_cloud_text(pg, pts, text, font_size)
            call = _make_freetext(doc, pg, box, text, font_size=font_size, color=rgb, fill=parse_color("white"),
                                  opacity=opacity, width=1.0, leader=_callout_points(box, tip))
            _tag(doc, call, subject=subject or "Cloud+", author=author, layer=layer, ds=_ds(font_size, rgb))
            doc.xref_set_key(call.xref, "IT", "/FreeTextCallout")
            parent = call.xref
            ids.append(core.markup_id(doc, call))
        annot = pg.add_polygon_annot(pts)
        _style(annot, rgb, fill, opacity, width, clouds=1)
        _tag(doc, annot, subject=subject or "Cloud+", author=author, contents=comment, layer=layer)
        x = annot.xref
        doc.xref_set_key(x, "IT", "/PolygonCloud")
        doc.xref_set_key(x, "BE", "<</S/C/I 1>>")
        if parent is not None:
            doc.xref_set_key(x, "ITEx", "/PolyText")
            _group(doc, x, parent)
        ids.append(core.markup_id(doc, annot))
        out = _info(doc, pno, annot)
        out.update(ids=ids, cloud_id=ids[-1], callout_id=ids[0] if parent is not None else None)
        if parent is not None:
            out["markup_id"] = ids[0]       # the group parent (the text callout)
        return out

    if kind in ("polygon", "polyline"):
        pts = _points_arg(points, 3 if kind == "polygon" else 2)
        _check_on_page(pg, _bbox(pts))
        annot = pg.add_polygon_annot(pts) if kind == "polygon" else pg.add_polyline_annot(pts)
        _style(annot, rgb, fill if kind == "polygon" else None, opacity, width, clouds if kind == "polygon" else -1)
        _tag(doc, annot, subject=subject or kind.capitalize(), author=author, contents=comment, layer=layer)
        return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)]}

    # line / arrow
    if points is None or len(points) != 2:
        raise DocumentError(f"kind='{kind}' needs points=[[x1, y1], [x2, y2]] (arrowhead at the second point)")
    a, b = _points_arg(points, 2)
    _check_on_page(pg, _bbox([a, b]))
    annot = pg.add_line_annot(a, b)
    if kind == "arrow":
        annot.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
    _style(annot, rgb, None, opacity, width)
    _tag(doc, annot, subject=subject or kind.capitalize(), author=author, contents=comment, layer=layer)
    if kind == "arrow":
        doc.xref_set_key(annot.xref, "IT", "/LineArrow")
    return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)]}


# --------------------------------------------------------------------------------------
# highlights
# --------------------------------------------------------------------------------------

def add_highlight(doc: pymupdf.Document, page: int, *, text: Optional[str] = None, rect: Any = None,
                  color: Any = "yellow", opacity: float = 1.0, subject: Optional[str] = None,
                  author: str = DEFAULT_AUTHOR, comment: Optional[str] = None,
                  layer: Optional[str] = None) -> dict:
    """Highlight every occurrence of `text` on the page (one markup each), or the text inside `rect`."""
    pno = core.page_index(doc, page)
    pg = doc[pno]
    if (text is None) == (rect is None):
        raise DocumentError("Give either text (highlight every occurrence) or rect (highlight that area)")
    rgb, opacity = parse_color(color), _check_opacity(opacity)
    if text is not None:
        if not str(text).strip():
            raise DocumentError("text is empty")
        quads = pg.search_for(text, quads=True)
        if not quads:
            raise DocumentError(f"Text {text!r} not found on page {page} (scanned page? then pass rect instead)")
    else:
        quads = [_rect_arg(rect).quad]
        _check_on_page(pg, _rect_arg(rect))
    items = []
    for q in quads:
        annot = pg.add_highlight_annot(quads=q)
        annot.set_colors(stroke=rgb)
        annot.set_opacity(opacity)
        annot.update()
        _tag(doc, annot, subject=subject or "Highlight", author=author, contents=comment, layer=layer)
        items.append(_info(doc, pno, annot))
    return {"markup_id": items[0]["markup_id"], "ids": [i["markup_id"] for i in items], "page": page,
            "count": len(items), "highlights": items}


# --------------------------------------------------------------------------------------
# measurements
# --------------------------------------------------------------------------------------

# name: (inches per unit, /U label, area label, volume label)
_UNITS: dict[str, tuple[float, str, str, str]] = {
    "in": (1.0, "in", "sq in", "cu in"),
    "ft": (12.0, "'", "sf", "cu yd"),
    "yd": (36.0, "yd", "sq yd", "cu yd"),
    "mi": (63360.0, "mi", "sq mi", "cu mi"),
    "mm": (1 / 25.4, "mm", "sq mm", "cu mm"),
    "cm": (1 / 2.54, "cm", "sq cm", "cu cm"),
    "m": (100 / 2.54, "m", "sq m", "cu m"),
    "km": (100000 / 2.54, "km", "sq km", "cu km"),
}
_VOLUME_C = {"ft": 1 / 27}          # cu ft -> cu yd; every other unit reports volume in its own cube
_ALIASES = {
    "in": "in", "inch": "in", "inches": "in", '"': "in", "″": "in",
    "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft", "′": "ft",
    "yd": "yd", "yds": "yd", "yard": "yd", "yards": "yd",
    "mi": "mi", "mile": "mi", "miles": "mi",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "km": "km", "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
}
_QTY = re.compile(r"(\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)\s*([A-Za-z]+|[\"'″′])?")


def _unit_name(u: str) -> str:
    key = str(u).strip().lower().rstrip(".")
    if key not in _ALIASES:
        raise DocumentError(f"Unknown unit {u!r}. Use one of: {', '.join(_UNITS)}")
    return _ALIASES[key]


def _to_float(txt: str) -> float:
    parts = txt.split()
    if "/" in parts[-1]:
        n, d = parts[-1].split("/")
        return (float(parts[0]) if len(parts) == 2 else 0.0) + float(n) / float(d)
    return float(parts[0])


def _side(txt: str) -> tuple[list[tuple[float, Optional[str], str]], Optional[str]]:
    """'30 ft', '1/4"', "1'-6\"" -> ([(value, unit|None, text)], unit of the first group)."""
    groups: list[tuple[float, Optional[str], str]] = []
    for m in _QTY.finditer(txt):
        unit = _unit_name(m.group(2)) if m.group(2) else None
        if unit is None and groups and groups[0][1] == "ft":
            unit = "in"                     # 1'-6 means 1 ft 6 in
        groups.append((_to_float(m.group(1)), unit, m.group(1)))
    if not groups or any(v <= 0 for v, _, _ in groups[:1]):
        raise DocumentError(f"Cannot read the scale part {txt!r}")
    return groups, groups[0][1]


@dataclass
class Scale:
    units_per_point: float   # real units per PDF point (Revu's /X/C)
    unit: str                # canonical unit key of _UNITS
    text: str                # /Measure /R


def parse_scale(scale: str, default_unit: str = "ft") -> Scale:
    """'1 in = 30 ft', '1" = 20\'', '1/4 in = 1 ft', '1:100', '1 cm = 5 m' -> Scale.

    A ratio such as '1:100' has no units: real units are `default_unit` at paper size.
    """
    m = re.fullmatch(r"(.+?)\s*[=:]\s*(.+)", str(scale).strip())
    if not m:
        raise DocumentError(f"Cannot read scale {scale!r}. Examples: '1 in = 30 ft', '1/4 in = 1 ft', '1:100'")
    lg, lunit = _side(m.group(1))
    rg, runit = _side(m.group(2))
    ratio = lunit is None and runit is None
    real_unit = _unit_name(default_unit) if ratio else (runit or _unit_name(default_unit))
    paper = sum(v * (1.0 if ratio else _UNITS[u or "in"][0]) for v, u, _ in lg)          # paper inches
    real = sum(v * (1.0 if ratio else _UNITS[u or real_unit][0]) for v, u, _ in rg)      # real inches
    real_val = real / paper / _UNITS[real_unit][0] if ratio else real / _UNITS[real_unit][0]
    upp = real / _UNITS[real_unit][0] / (paper * 72.0)
    paper_txt = "1 in" if ratio else f"{lg[0][2]} {lg[0][1] or 'in'}"
    text = (f"{paper_txt} = {real_val:g} ft' in\"" if real_unit == "ft" else f"{paper_txt} = {real_val:g} {real_unit}")
    return Scale(float(f"{upp:.7g}"), real_unit, text)


def resolve_scale(scale: Optional[str], units_per_point: Optional[float], unit: str) -> Scale:
    if units_per_point is not None:
        if not isinstance(units_per_point, (int, float)) or units_per_point <= 0:
            raise DocumentError("units_per_point must be a positive number (real units per PDF point)")
        u = _unit_name(unit)
        text = (f"1 in = {72 * units_per_point:g} ft' in\"" if u == "ft" else f"1 in = {72 * units_per_point:g} {u}")
        return Scale(float(f"{units_per_point:.7g}"), u, text)
    if not scale:
        raise DocumentError(_NO_SCALE)
    return parse_scale(scale, unit)


_NO_SCALE = ("Pass the drawing scale, e.g. scale='1 in = 20 ft' (see bb_sheet_index / bb_list_markups "
             "for the sheet's scale)")


def page_scale(doc: pymupdf.Document, pno: int, unit: str = "ft") -> tuple[Scale, str]:
    """The one scale this 0-based page already carries (viewport /VP, else its measurement markups).

    Returns (Scale, "page viewport" | "existing markups"); DocumentError when the page has no scale
    or more than one distinct scale.
    """
    found = markups_read.page_scales(doc, pno)
    if not found:
        raise DocumentError(_NO_SCALE)
    distinct = {round(f["units_per_point"], 7) for f in found}
    if len(distinct) > 1:
        texts = sorted({f.get("scale") or f"{f['units_per_point']} per point" for f in found})
        raise DocumentError(f"Page {pno + 1} has {len(distinct)} different scales ({'; '.join(texts)}). " + _NO_SCALE)
    best = found[0]                                              # viewports come first
    try:
        real = _unit_name(best.get("unit") or unit)
    except DocumentError:
        real = _unit_name(unit)
    base = resolve_scale(None, best["units_per_point"], real)
    return (Scale(base.units_per_point, real, best.get("scale") or base.text),
            "page viewport" if best["source"] == "viewport" else "existing markups")


def _fmt_decimal(v: float, label: str) -> str:
    """'1,736.11 sf', '200,904.9 sf', '131,454 sf' (2 decimals, trailing zeros dropped)."""
    return f"{round(v, 2):,.2f}".rstrip("0").rstrip(".") + f" {label}"


def _fmt_ft_in(feet: float, denom: int = 4) -> str:
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


def _fmt_length(v: float, unit: str) -> str:
    return _fmt_ft_in(v) if unit == "ft" else _fmt_decimal(v, unit)


def _shoelace(pts: Sequence[pymupdf.Point]) -> float:
    n = len(pts)
    return abs(sum(pts[i].x * pts[(i + 1) % n].y - pts[(i + 1) % n].x * pts[i].y for i in range(n))) / 2.0


def _path_length(pts: Sequence[pymupdf.Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _pstr(s: str) -> str:
    return pymupdf.get_pdf_str(s)


def _measure_source(sc: Scale) -> str:
    """Source of a Bluebeam RL /Measure dictionary (same shape as bb_synth.measure_dict for feet)."""
    inch, label, area_label, vol_label = _UNITS[sc.unit]
    c = _num(sc.units_per_point)
    vc = _num(_VOLUME_C.get(sc.unit, 1.0))
    tuc = _num(1 / 72 / inch)                 # PDF points -> unit at paper size
    if sc.unit == "ft":
        x = f"/X[<</Type/NumberFormat/U(')/C {c}/F/F/D 4/FD true/SS()>>]"
        d = ("/D[<</Type/NumberFormat/U(')/C 1/F/F/D 4/FD true/PS()/SS(-)>>"
             "<</Type/NumberFormat/U(\")/C 12/F/F/D 4/FD true/PS()/SS()>>]")
    else:
        x = f"/X[<</Type/NumberFormat/U{_pstr(label)}/C {c}/D 100/FD true/SS()>>]"
        d = f"/D[<</Type/NumberFormat/U{_pstr(label)}/C 1/D 100/FD true/SS()>>]"
    return ("<</Type/Measure/Subtype/RL/R" + _pstr(sc.text) + x + d
            + "/A[<</Type/NumberFormat/U" + _pstr(area_label) + "/C 1/D 100/FD true/SS()>>]"
            + "/T[<</Type/NumberFormat/U<B0>/C 1/D 100/FD true/PS()/SS()>>]"
            + "/V[<</Type/NumberFormat/U" + _pstr(vol_label) + "/C " + vc + "/D 100/FD true/SS()>>]"
            + "/TargetUnitConversion " + tuc + ">>")


def _depth_unit_array(unit: str) -> str:
    inch, label = _UNITS[unit][0], _UNITS[unit][1]
    return f"[<</Type/NumberFormat/U{_pstr(label)}/C {_num(1 / 72 / inch)}/D 100/FD true/SS()>>]"


def _checkmark(cx: float, cy: float, s: float = 6.0) -> list[pymupdf.Point]:
    pts = [(cx - s, cy), (cx - s * 0.6, cy - s * 0.4), (cx - s * 0.2, cy + s * 0.4),
           (cx + s, cy - s), (cx + s * 0.7, cy - s * 1.3), (cx - s * 0.2, cy - s * 0.2)]
    return [pymupdf.Point(x, y) for x, y in pts]


def _measure_keys(doc: pymupdf.Document, x: int, intent: str, mtype: int) -> None:
    doc.xref_set_key(x, "IT", "/" + intent)
    doc.xref_set_key(x, "MeasurementTypes", str(mtype))
    doc.xref_set_key(x, "Cap", "true")
    doc.xref_set_key(x, "AlignOnSegment", "false")
    doc.xref_set_key(x, "SlopeType", "0")
    doc.xref_set_key(x, "PitchRun", "12")


def _attach_measure(doc: pymupdf.Document, x: int, sc: Scale) -> None:
    mx = doc.get_new_xref()
    doc.update_object(mx, _measure_source(sc))
    doc.xref_set_key(x, "Measure", f"{mx} 0 R")


_MEASURE_SUBJECTS = {"area": "Area Measurement", "perimeter": "Perimeter Measurement",
                     "length": "Length Measurement", "count": "Count Measurement"}


def add_measurement(doc: pymupdf.Document, page: int, *, kind: str, points: Any,
                    scale: Optional[str] = None, units_per_point: Optional[float] = None,
                    unit: str = "ft", depth: Optional[float] = None, depth_unit: str = "in",
                    color: Any = "#FF0000", fill_color: Any = None, opacity: float = 1.0,
                    line_width: float = 1.5, subject: Optional[str] = None, author: str = DEFAULT_AUTHOR,
                    label: Optional[str] = None, layer: Optional[str] = None) -> dict:
    """Write a Bluebeam measurement markup (see tools_write.bb_add_measurement)."""
    pno = core.page_index(doc, page)
    pg = doc[pno]
    kind = _choice(kind, ("area", "perimeter", "length", "count"), "kind")
    rgb, fill, opacity = parse_color(color), parse_color(fill_color, True), _check_opacity(opacity)
    width = _check_width(line_width)
    minimum = {"area": 3, "perimeter": 2, "length": 2, "count": 1}[kind]
    pts = _points_arg(points, minimum)
    _check_on_page(pg, _bbox(pts))
    if depth is not None:
        if kind != "area":
            raise DocumentError("depth (volume) only applies to kind='area'")
        if not isinstance(depth, (int, float)) or depth <= 0:
            raise DocumentError("depth must be a positive number")
    subj = subject or _MEASURE_SUBJECTS[kind]
    out: dict[str, Any] = {"kind": kind, "page": page}

    if kind == "count":                                   # counts carry no scale
        n = len(pts)
        ids: list[str] = []
        xrefs: list[int] = []
        box = pymupdf.Rect()
        for p in pts:
            annot = pg.add_polygon_annot(_checkmark(p.x, p.y))
            _style(annot, rgb, rgb, 1.0, 0)
            _tag(doc, annot, subject=subj, author=author, contents=str(n), layer=layer)
            x = annot.xref
            doc.xref_set_key(x, "IT", "/PolygonCount")
            doc.xref_set_key(x, "MeasurementTypes", "128")
            doc.xref_set_key(x, "CountStyle", "/Checkmark")
            doc.xref_set_key(x, "NumCounts", str(n))
            doc.xref_set_key(x, "Version", "1")
            _set_str(doc, x, "Label", label or "")
            if xrefs:
                _group(doc, x, xrefs[0])
            xrefs.append(x)
            ids.append(core.markup_id(doc, annot))
            box |= annot.rect
        out.update(markup_id=ids[0], ids=ids, value=n, unit="count", formatted=str(n), count=n,
                   type="Polygon", rect=[round(v, 2) for v in box])
        return out

    if scale or units_per_point is not None:
        sc, scale_source = resolve_scale(scale, units_per_point, unit), "argument"
    else:
        sc, scale_source = page_scale(doc, pno, unit)
    c = sc.units_per_point
    out.update(scale=sc.text, scale_source=scale_source, units_per_point=c)

    if kind == "area":
        area = round(_shoelace(pts) * c * c, 2)          # A.C == 1
        annot = pg.add_polygon_annot(pts)
        _style(annot, rgb, fill, opacity, width)
        formatted, mtype, intent = _fmt_decimal(area, _UNITS[sc.unit][2]), 129, "PolygonDimension"
        out.update(value=area, unit=_UNITS[sc.unit][2])
    elif kind == "length" and len(pts) == 2:
        value = _path_length(pts) * c                       # D.C == 1
        annot = pg.add_line_annot(pts[0], pts[1])
        _style(annot, rgb, None, opacity, width)
        formatted, mtype, intent = _fmt_length(value, sc.unit), 130, "LineDimension"
        out.update(value=round(value, 4), unit=sc.unit)
    else:
        value = _path_length(pts) * c
        annot = pg.add_polyline_annot(pts)
        _style(annot, rgb, None, opacity, width)
        formatted, mtype, intent = _fmt_length(value, sc.unit), 130, "PolyLineDimension"
        out.update(value=round(value, 4), unit=sc.unit)

    _tag(doc, annot, subject=subj, author=author, contents=formatted, layer=layer)
    x = annot.xref
    _set_str(doc, x, "Label", label or "")
    if kind == "area":
        doc.xref_set_key(x, "FillOpacity", _num(opacity))
    _measure_keys(doc, x, intent, mtype)
    if intent == "LineDimension":
        doc.xref_set_key(x, "LL", "0")
        doc.xref_set_key(x, "LLE", "0")
    _attach_measure(doc, x, sc)
    if depth is not None:
        du = _unit_name(depth_unit)
        doc.xref_set_key(x, "Depth", _num(depth))
        doc.xref_set_key(x, "DepthUnit", _depth_unit_array(du))
        depth_in_scale_units = depth * _UNITS[du][0] / _UNITS[sc.unit][0]
        out.update(depth=depth, depth_unit=du, volume=round(out["value"] * depth_in_scale_units
                                                            * _VOLUME_C.get(sc.unit, 1.0), 2),
                   volume_unit=_UNITS[sc.unit][3])
    out.update(_info(doc, pno, annot))
    out.update(ids=[out["markup_id"]], formatted=formatted)
    return out


# --------------------------------------------------------------------------------------
# stamps
# --------------------------------------------------------------------------------------

# Same order as PyMuPDF's Page._add_stamp_annot (its STAMP_* constants disagree for Draft/Experimental).
_BUILTIN_STAMPS = ["Approved", "AsIs", "Confidential", "Departmental", "Experimental", "Expired", "Final",
                   "ForComment", "ForPublicRelease", "NotApproved", "NotForPublicRelease", "Sold",
                   "TopSecret", "Draft"]
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}


def _norm_stamp(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(name)).lower()


def _stamp_pixmap(path: Path) -> pymupdf.Pixmap:
    """Image or 1st page of a stamp PDF as an RGB(A) pixmap (PDF pages rasterized at up to 200 dpi)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            with pymupdf.open(str(path)) as sdoc:
                if sdoc.page_count < 1:
                    raise DocumentError(f"Stamp PDF has no pages: {path}")
                p = sdoc[0]
                inches = max(p.rect.width, p.rect.height) / 72
                pix = p.get_pixmap(dpi=int(max(72, min(200, 3000 / inches))), alpha=True)
        else:
            pix = pymupdf.Pixmap(str(path))
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(f"Could not read stamp image {path}: {e}") from None
    if pix.colorspace is not None and pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix


def _stamp_opacity(doc: pymupdf.Document, annot: pymupdf.Annot, opacity: float) -> None:
    """/CA plus the same alpha inside the appearance stream (MuPDF does not draw a stamp's /CA)."""
    annot.set_opacity(opacity)
    if opacity < 1:
        annot.update()
        return
    kind, ap = doc.xref_get_key(annot.xref, "AP/N")
    if kind == "xref" and doc.xref_get_key(int(ap.split()[0]), "Resources/ExtGState/H")[0] != "null":
        doc.xref_set_key(int(ap.split()[0]), "Resources/ExtGState/H", "<</CA 1/ca 1>>")


def add_stamp(doc: pymupdf.Document, page: int, stamp: str, rect: Any, *, opacity: float = 1.0,
              subject: Optional[str] = None, author: str = DEFAULT_AUTHOR, comment: Optional[str] = None,
              layer: Optional[str] = None) -> dict:
    """Place a built-in stamp, an image stamp or a 1-page stamp PDF (see tools_write.bb_add_stamp)."""
    pno = core.page_index(doc, page)
    pg = doc[pno]
    box = _rect_arg(rect)
    _check_on_page(pg, box)
    opacity = _check_opacity(opacity)
    if not str(stamp).strip():
        raise DocumentError("stamp is empty")
    by_norm = {_norm_stamp(n): i for i, n in enumerate(_BUILTIN_STAMPS)}
    key = _norm_stamp(stamp)
    if key in by_norm:
        idx = by_norm[key]
        annot = pg.add_stamp_annot(box, stamp=idx)
        label, source = _BUILTIN_STAMPS[idx], "builtin"
    else:
        path = Path(os.path.expandvars(os.path.expanduser(str(stamp).strip().strip('"')))).absolute()
        if not path.is_file():
            raise DocumentError(
                f"Stamp {stamp!r} is neither a built-in name ({', '.join(_BUILTIN_STAMPS)}) nor an existing "
                "image / stamp PDF file")
        if path.suffix.lower() not in _IMAGE_SUFFIXES | {".pdf"}:
            raise DocumentError(f"Unsupported stamp file type {path.suffix!r}: use PNG, JPG or a 1-page PDF")
        annot = pg.add_stamp_annot(box, stamp=_stamp_pixmap(path))
        label, source = path.stem, "pdf" if path.suffix.lower() == ".pdf" else "image"
    _stamp_opacity(doc, annot, opacity)
    _tag(doc, annot, subject=subject or label, author=author, contents=comment, layer=layer)
    if comment is None and source != "builtin":
        _del_key(doc, annot.xref, "Contents")               # PyMuPDF writes 'Image Stamp' here
    return {**_info(doc, pno, annot), "ids": [core.markup_id(doc, annot)], "stamp": label, "source": source}


# --------------------------------------------------------------------------------------
# replies and review status
# --------------------------------------------------------------------------------------

_STATES = {"accepted": "Accepted", "rejected": "Rejected", "cancelled": "Cancelled",
           "canceled": "Cancelled", "completed": "Completed", "none": "None"}


def reply_to_markup(doc: pymupdf.Document, markup_id: str, *, text: Optional[str] = None,
                    status: Optional[str] = None, author: str = DEFAULT_AUTHOR) -> dict:
    """Add a reply and/or a review status to a markup (same page, /IRT to the parent)."""
    if not text and not status:
        raise DocumentError("Give text (a reply), status (Accepted, Rejected, Cancelled, Completed, None) or both")
    state = None
    if status:
        state = _STATES.get(str(status).strip().lower())
        if state is None:
            raise DocumentError(f"Bad status {status!r}: use Accepted, Rejected, Cancelled, Completed or None")
    author = author or DEFAULT_AUTHOR
    pg, parent = core.find_annot(doc, markup_id)
    pno = pg.number
    at = pymupdf.Point(parent.rect.x0, parent.rect.y0)
    parent_id = core.markup_id(doc, parent)
    out: dict[str, Any] = {"parent_id": parent_id, "page": pno + 1}

    def _child(body: str, flags: int) -> tuple[pymupdf.Annot, str]:
        annot = pg.add_text_annot(at, body, icon="Comment")
        annot.update()
        x = annot.xref
        nm, now = core.new_markup_id(), _pdf_date()
        _set_str(doc, x, "NM", nm)
        _set_str(doc, x, "T", author)
        _set_str(doc, x, "CreationDate", now)
        _set_str(doc, x, "M", now)
        doc.xref_set_key(x, "IRT", f"{parent.xref} 0 R")
        doc.xref_set_key(x, "F", str(flags))
        return annot, nm

    if text:
        annot, nm = _child(text, 28)
        out["reply_id"] = nm
    if state:
        annot, nm = _child(f"{state} set by {author}", 30)
        _set_str(doc, annot.xref, "StateModel", "Review")
        _set_str(doc, annot.xref, "State", state)
        out.update(status_id=nm, status=state)
    out["markup_id"] = out.get("reply_id") or out["status_id"]
    return out


# --------------------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------------------

def _retally_counts(doc: pymupdf.Document, index: _Kids, parent: dict[int, int], doomed: set[int],
                    by_xref: dict[int, pymupdf.Annot]) -> list[dict]:
    """A count group that loses some (not all) symbols: /NumCounts and /Contents of the survivors = new N."""
    out: list[dict] = []
    heads = {parent[x] for x in doomed if x in parent and parent[x] not in doomed
             and _name(doc, x, "IT") == "/PolygonCount" and _name(doc, x, "RT") == "/Group"}
    for head in sorted(heads):
        alive = [head] + [c for c, rt, _ in index.get(head, ())
                          if rt == "/Group" and c not in doomed and _name(doc, c, "IT") == "/PolygonCount"]
        for x in alive:
            doc.xref_set_key(x, "NumCounts", str(len(alive)))
            _set_str(doc, x, "Contents", str(len(alive)))
            _touch(doc, x)
        out.append({"group": core.markup_id(doc, by_xref[head]), "count": len(alive)})
    return out


def delete_markups(doc: pymupdf.Document, markup_ids: Sequence[str], delete_replies: bool = True) -> dict:
    """Delete markups with their group members, their review statuses and (default) their replies.

    Deleting a group's parent (a Cloud+ callout, the first symbol of a count) deletes the group;
    deleting one count symbol leaves the others, renumbered. With delete_replies=False plain replies
    are kept as standalone notes (detached from the deleted markup); statuses always go.
    """
    if not markup_ids:
        raise DocumentError("markup_ids is empty")
    targets = _find_many(doc, markup_ids)                            # fail before touching anything
    _refuse_locked(doc, targets, "delete")
    pages: dict[int, pymupdf.Page] = {}
    indexes: dict[int, _Kids] = {}
    doomed: dict[int, set[int]] = {}
    loose: dict[int, set[int]] = {}
    for pg, annot in targets:
        pages[pg.number] = pg
        index = indexes.setdefault(pg.number, _children_index(doc, pg))
        members, left = _descendants(index, annot.xref, delete_replies, statuses=True)
        doomed.setdefault(pg.number, set()).update([annot.xref, *members])
        loose.setdefault(pg.number, set()).update(left)
    deleted: list[str] = []
    detached: list[str] = []
    recounted: list[dict] = []
    for pno, xrefs in doomed.items():
        pg = pages[pno]
        parent = {child: p for p, kids in indexes[pno].items() for child, _, _ in kids}
        by_xref = {a.xref: a for a in pg.annots()}

        def depth(x: int) -> int:                                    # hops up to a root inside `xrefs`
            n = 0
            while parent.get(x) in xrefs:
                x, n = parent[x], n + 1
            return n

        recounted += _retally_counts(doc, indexes[pno], parent, xrefs, by_xref)
        for x in sorted(loose[pno] - xrefs):                         # replies we keep: cut them loose first
            _del_key(doc, x, "IRT")
            _del_key(doc, x, "RT")
            if x in by_xref:
                detached.append(core.markup_id(doc, by_xref[x]))
        for x in sorted(xrefs, key=depth, reverse=True):             # deepest first
            if x in by_xref:
                deleted.append(core.markup_id(doc, by_xref[x]))
                pg.delete_annot(by_xref[x])
    return {"deleted": deleted, "count": len(deleted), "detached_replies": detached, "recounted": recounted}


# --------------------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------------------

_GROUP_KEYS = ("color", "opacity", "line_width", "layer", "hidden", "locked")   # these follow a group's parent
ALLOWED_EDIT_KEYS = ("text", "contents", "subject", "author", "color", "fill_color", "opacity",
                     "line_width", "layer", "locked", "hidden", "rect", "custom_columns")
_STYLED = {pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE, pymupdf.PDF_ANNOT_LINE, pymupdf.PDF_ANNOT_POLYGON,
           pymupdf.PDF_ANNOT_POLY_LINE, pymupdf.PDF_ANNOT_INK}
_REDRAW = _STYLED | {pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_TEXT,
                     pymupdf.PDF_ANNOT_UNDERLINE, pymupdf.PDF_ANNOT_STRIKE_OUT, pymupdf.PDF_ANNOT_SQUIGGLY}
_POINT_KEYS = ("Vertices", "L", "CL", "QuadPoints", "InkList")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_PDF_STRING = re.compile(r"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>")


def _decode_pdf_string(tok: str) -> str:
    if tok.startswith("<"):
        data = bytes.fromhex(re.sub(r"\s", "", tok[1:-1]) + ("0" if len(re.sub(r"\s", "", tok[1:-1])) % 2 else ""))
        return data.decode("utf-16") if data[:2] in (b"\xfe\xff", b"\xff\xfe") else data.decode("latin-1")
    body = tok[1:-1]
    body = re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), body)
    body = re.sub(r"\\(.)", lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), body)
    return body


def column_names(doc: pymupdf.Document) -> list[str]:
    """Custom-column names from the catalog's /BSIColumnData definitions ([] when there are none)."""
    kind, val = doc.xref_get_key(doc.pdf_catalog(), "BSIColumnData")
    if kind == "xref":
        src = doc.xref_object(int(val.split()[0]), compressed=True)
    elif kind == "array":
        src = val
    else:
        return []
    return [_decode_pdf_string(m.group(1)) for m in
            re.finditer(r"/Name\s*(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)", src)]


def _column_values(doc: pymupdf.Document, xref: int) -> list[str]:
    kind, val = doc.xref_get_key(xref, "BSIColumnData")
    return [_decode_pdf_string(t) for t in _PDF_STRING.findall(val)] if kind == "array" else []


def _set_columns(doc: pymupdf.Document, xref: int, values: dict[str, Any]) -> None:
    names = column_names(doc)
    if not names:
        raise DocumentError("This PDF defines no custom columns, so custom_columns cannot be set "
                            "(create the columns in Revu first)")
    unknown = [k for k in values if k not in names]
    if unknown:
        raise DocumentError(f"Unknown custom column(s) {unknown}. Defined columns: {names}")
    row = _column_values(doc, xref)
    row += [""] * (len(names) - len(row))
    for k, v in values.items():
        row[names.index(k)] = "" if v is None else str(v)
    doc.xref_set_key(xref, "BSIColumnData", "[" + "".join(_pstr(v) for v in row[:len(names)]) + "]")


def _remap_points(doc: pymupdf.Document, page: pymupdf.Page, annot: pymupdf.Annot,
                  old: pymupdf.Rect, new: pymupdf.Rect) -> None:
    """Move/scale every point array of the annotation so old rect -> new rect (page space)."""
    sx, sy = new.width / old.width, new.height / old.height
    a = pymupdf.Matrix(sx, 0, 0, sy, new.x0 - old.x0 * sx, new.y0 - old.y0 * sy)
    t = page.transformation_matrix
    raw = t * a * ~t                                    # same move, expressed in raw PDF space
    for key in _POINT_KEYS:
        kind, val = doc.xref_get_key(annot.xref, key)
        if kind != "array":
            continue
        nums = [float(n) for n in _NUMBER.findall(val)]
        moved = []
        for i in range(0, len(nums) - 1, 2):
            p = pymupdf.Point(nums[i], nums[i + 1]) * raw
            moved += [p.x, p.y]
        it = iter(moved)
        doc.xref_set_key(annot.xref, key, _NUMBER.sub(lambda _m: _num(next(it)), val))


def _redraw(doc: pymupdf.Document, annot: pymupdf.Annot, text_color: Optional[Sequence[float]] = None) -> None:
    """Regenerate the appearance stream after a change to the dictionary."""
    t = annot.type[0]
    if t not in _REDRAW:
        return
    x = annot.xref
    if t != pymupdf.PDF_ANNOT_FREE_TEXT:
        annot.update()
        return
    # FreeText: MuPDF reads /C as the background, Bluebeam keeps the border color there
    stroke, fill = doc.xref_get_key(x, "C"), doc.xref_get_key(x, "IC")
    callout = _name(doc, x, "IT") == "/FreeTextCallout"
    if fill[0] == "null":
        _del_key(doc, x, "C")
    annot.update(text_color=text_color)
    _put_key(doc, x, "C", stroke[1] if stroke[0] == "array" else None)
    if fill[0] == "array":
        doc.xref_set_key(x, "IC", fill[1])
    if not callout:
        _del_key(doc, x, "CL")


def _set_ds_color(doc: pymupdf.Document, xref: int, rgb: Sequence[float]) -> None:
    """Keep /DS's color in step: MuPDF redraws a FreeText that has /RC from /DS, not from /DA."""
    kind, ds = doc.xref_get_key(xref, "DS")
    if kind != "string" or not ds:
        _set_str(doc, xref, "DS", _ds(12, rgb))
    elif re.search(r"color\s*:\s*#[0-9A-Fa-f]{3,6}", ds):
        _set_str(doc, xref, "DS", re.sub(r"(?<![-\w])color\s*:\s*#[0-9A-Fa-f]{3,6}", f"color:{_hex(rgb)}", ds))
    else:
        _set_str(doc, xref, "DS", ds.rstrip("; ") + f"; color:{_hex(rgb)}")


def _set_contents(doc: pymupdf.Document, annot: pymupdf.Annot, text: str) -> None:
    x = annot.xref
    _set_str(doc, x, "Contents", text)
    if doc.xref_get_key(x, "RC")[0] != "null":          # Revu shows /RC when present, so keep it in step
        _set_str(doc, x, "RC", _rich_text(text))


_NUMBER_FORMAT = re.compile(r"<<(.*?)>>", re.S)


def _number_formats(src: str, key: str) -> list[tuple[float, str]]:
    """[(/C, /U)] of the NumberFormat array under /X, /D, /A or /V of a /Measure dictionary source."""
    m = re.search(r"/%s\s*\[((?:\s*<<.*?>>\s*)+)\]" % key, src, re.S)
    out: list[tuple[float, str]] = []
    for body in _NUMBER_FORMAT.findall(m.group(1)) if m else []:
        c = re.search(r"/C\s*(-?[\d.]+(?:[eE][-+]?\d+)?)", body)
        u = re.search(r"/U\s*(\((?:\.|[^\()])*\)|<[0-9A-Fa-f\s]*>)", body)
        out.append((float(c.group(1)) if c else 1.0, _decode_pdf_string(u.group(1)) if u else ""))
    return out


def _refresh_measurement(doc: pymupdf.Document, x: int) -> Optional[dict]:
    """Recompute value and /Contents of a Dimension markup from its (just changed) geometry and /Measure.

    area = shoelace(pt) * X.C^2 * A.C; length = path(pt) * X.C * D[0].C; volume (when /Depth is set) =
    area * depth in scale units * V.C. Returns {value, unit, formatted, [volume, volume_unit]} or None
    when the markup is not a measurement this can handle (counts, unreadable /Measure).
    """
    intent = _name(doc, x, "IT")
    kind, val = doc.xref_get_key(x, "Measure")
    if not intent.endswith("Dimension") or kind not in ("xref", "dict"):
        return None
    src = doc.xref_object(int(val.split()[0]), compressed=True) if kind == "xref" else val
    xf, df, af, vf = (_number_formats(src, k) for k in "XDAV")
    key = "L" if intent == "/LineDimension" else "Vertices"
    ak, arr = doc.xref_get_key(x, key)
    n = [float(v) for v in _NUMBER.findall(arr)] if ak == "array" else []
    pts = [pymupdf.Point(n[i], n[i + 1]) for i in range(0, len(n) - 1, 2)]
    if not xf or len(pts) < 2:
        return None
    c = xf[0][0]
    if intent == "/PolygonDimension":
        if not af:
            return None
        value = round(_shoelace(pts) * c * c * af[0][0], 2)
        unit = af[0][1]
        formatted, out = _fmt_decimal(value, unit), {"value": value, "unit": unit}
        dk, dv = doc.xref_get_key(x, "Depth")
        tuc = re.search(r"/TargetUnitConversion\s*(-?[\d.]+)", src)
        dus = re.search(r"/C\s*(-?[\d.]+)", doc.xref_get_key(x, "DepthUnit")[1])
        if dk != "null" and vf and tuc and dus and float(dus.group(1)):
            depth = float(dv) * float(tuc.group(1)) / float(dus.group(1))          # depth in scale units
            out.update(volume=round(value * depth * vf[0][0], 2), volume_unit=vf[0][1])
    else:
        d0 = df[0][0] if df else 1.0
        value = _path_length(pts) * c * d0
        two_part = len(df) == 2 and df[0][1] == "'" and df[1][1] == '"'
        unit = df[0][1] if df else xf[0][1]
        formatted = _fmt_ft_in(value) if two_part else _fmt_decimal(value, unit)
        out = {"value": round(value, 4), "unit": "ft" if unit == "'" else unit}
    _set_str(doc, x, "Contents", formatted)
    out["formatted"] = formatted
    return out


def _edit_one(doc: pymupdf.Document, page: pymupdf.Page, annot: pymupdf.Annot, ch: dict[str, Any]) -> dict:
    x, t = annot.xref, annot.type[0]
    applied: list[str] = []
    ignored: list[dict] = []

    def skip(key: str, why: str) -> None:
        ignored.append({"key": key, "reason": why})

    if "subject" in ch:
        _set_str(doc, x, "Subj", ch["subject"])
        applied.append("subject")
    if "author" in ch:
        _set_str(doc, x, "T", ch["author"])
        applied.append("author")
    if "custom_columns" in ch:
        _set_columns(doc, x, ch["custom_columns"])
        applied.append("custom_columns")
    if "layer" in ch:
        annot.set_oc(layer_xref(doc, ch["layer"]) if ch["layer"] else 0)
        applied.append("layer")
    if "hidden" in ch:
        _flags_set(annot, pymupdf.PDF_ANNOT_IS_HIDDEN, ch["hidden"])
        applied.append("hidden")
    if "locked" in ch:
        _flags_set(annot, pymupdf.PDF_ANNOT_IS_LOCKED, ch["locked"])
        applied.append("locked")

    redraw = False
    text_color = None
    measurement = None
    if "text" in ch:
        _set_contents(doc, annot, ch["text"])
        applied.append("text")
        redraw = True
    if "color" in ch:
        rgb = ch["color"]
        if t in _STYLED or t in (pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_TEXT,
                                 pymupdf.PDF_ANNOT_UNDERLINE, pymupdf.PDF_ANNOT_STRIKE_OUT, pymupdf.PDF_ANNOT_SQUIGGLY):
            doc.xref_set_key(x, "C", _rgb_array(rgb))
            if _name(doc, x, "IT") == "/PolygonCount":           # count symbols are filled with their color
                doc.xref_set_key(x, "IC", _rgb_array(rgb))
            if t == pymupdf.PDF_ANNOT_FREE_TEXT:
                text_color = rgb
                _set_ds_color(doc, x, rgb)
            applied.append("color")
            redraw = True
        else:
            skip("color", f"{annot.type[1]} markups have no color")
    if "fill_color" in ch:
        if t in _STYLED - {pymupdf.PDF_ANNOT_INK} or t == pymupdf.PDF_ANNOT_FREE_TEXT:
            fill = ch["fill_color"]
            _put_key(doc, x, "IC", _rgb_array(fill) if fill else None)
            applied.append("fill_color")
            redraw = True
        else:
            skip("fill_color", f"{annot.type[1]} markups have no fill")
    if "opacity" in ch:
        if t == pymupdf.PDF_ANNOT_STAMP:
            _stamp_opacity(doc, annot, ch["opacity"])
        else:
            annot.set_opacity(ch["opacity"])
            redraw = True
        applied.append("opacity")
    if "line_width" in ch:
        if t in _STYLED or t == pymupdf.PDF_ANNOT_FREE_TEXT:
            annot.set_border(width=ch["line_width"])
            applied.append("line_width")
            redraw = True
        else:
            skip("line_width", f"{annot.type[1]} markups have no border")
    if "rect" in ch:
        new, old = ch["rect"], annot.rect
        if t in (pymupdf.PDF_ANNOT_POLYGON, pymupdf.PDF_ANNOT_POLY_LINE, pymupdf.PDF_ANNOT_LINE,
                 pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_UNDERLINE,
                 pymupdf.PDF_ANNOT_STRIKE_OUT, pymupdf.PDF_ANNOT_SQUIGGLY):
            _remap_points(doc, page, annot, old, new)
            if abs(new.width - old.width) > 0.01 or abs(new.height - old.height) > 0.01:
                measurement = _refresh_measurement(doc, x)               # a resized measurement changes value
            redraw = True
            if t in (pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_UNDERLINE, pymupdf.PDF_ANNOT_STRIKE_OUT,
                     pymupdf.PDF_ANNOT_SQUIGGLY):
                annot.set_rect(new)
        else:
            if t == pymupdf.PDF_ANNOT_FREE_TEXT and _name(doc, x, "IT") == "/FreeTextCallout":
                if abs(new.width - old.width) > 0.5 or abs(new.height - old.height) > 0.5:
                    raise DocumentError("A callout can be moved (same width and height) but not resized with rect; "
                                        "edit its text or add it again")
                _remap_points(doc, page, annot, old, new)
            annot.set_rect(new)
            redraw = t != pymupdf.PDF_ANNOT_STAMP
        applied.append("rect")
    if redraw:
        _redraw(doc, annot, text_color)
    if applied:
        _touch(doc, x)
    out = {"markup_id": core.markup_id(doc, annot), "page": page.number + 1, "applied": applied,
           "ignored": ignored, "rect": [round(v, 2) for v in annot.rect]}
    if measurement:
        out["measurement"] = measurement
    return out


def edit_markups(doc: pymupdf.Document, markup_ids: Sequence[str], changes: dict[str, Any]) -> dict:
    """Change properties of existing markups (see tools_write.bb_edit_markups for the keys)."""
    if not markup_ids:
        raise DocumentError("markup_ids is empty")
    if not isinstance(changes, dict) or not changes:
        raise DocumentError(f"changes is empty. Allowed keys: {', '.join(ALLOWED_EDIT_KEYS)}")
    unknown = [k for k in changes if k not in ALLOWED_EDIT_KEYS]
    if unknown:
        raise DocumentError(f"Unknown change key(s): {unknown}. Allowed keys: {', '.join(ALLOWED_EDIT_KEYS)}")
    if "text" in changes and "contents" in changes:
        raise DocumentError("Give text or contents, not both")
    ch = dict(changes)
    if "contents" in ch:
        ch["text"] = ch.pop("contents")
    if "text" in ch:
        ch["text"] = "" if ch["text"] is None else str(ch["text"])
    for key in ("subject", "author"):
        if key in ch:
            ch[key] = str(ch[key])
    if "color" in ch:
        ch["color"] = parse_color(ch["color"])
    if "fill_color" in ch:
        ch["fill_color"] = parse_color(ch["fill_color"], allow_none=True)
    if "opacity" in ch:
        ch["opacity"] = _check_opacity(ch["opacity"])
    if "line_width" in ch:
        ch["line_width"] = _check_width(ch["line_width"])
    for key in ("locked", "hidden"):
        if key in ch and not isinstance(ch[key], bool):
            raise DocumentError(f"{key} must be true or false")
    if "rect" in ch:
        ch["rect"] = _rect_arg(ch["rect"])
    if "custom_columns" in ch and not isinstance(ch["custom_columns"], dict):
        raise DocumentError("custom_columns must be an object like {\"Bid Item\": \"RIPRAP-12\"}")
    targets = _find_many(doc, markup_ids)
    if ch.get("locked") is not False:                     # unlocking (alone or with other changes) is allowed
        _refuse_locked(doc, targets, "change")
    if "rect" in ch and any(pg.rotation for pg, _ in targets):
        raise DocumentError("Moving/resizing markups on rotated pages is not supported")
    edited: list[dict] = []
    seen: set[int] = set()
    group_ch = {k: ch[k] for k in _GROUP_KEYS if k in ch}
    indexes: dict[int, _Kids] = {}
    for pg, annot in targets:
        if annot.xref in seen:
            continue
        seen.add(annot.xref)
        first = _edit_one(doc, pg, annot, ch)
        edited.append(first)
        if not group_ch:
            continue
        index = indexes.setdefault(pg.number, _children_index(doc, pg))     # a group (Cloud+, a count) is one
        members = [m for m in _descendants(index, annot.xref, False)[0] if m not in seen]   # markup to the user
        if members:
            by_xref = {a.xref: a for a in pg.annots()}
            for mx in members:
                seen.add(mx)
                edited.append({**_edit_one(doc, pg, by_xref[mx], group_ch), "group_of": first["markup_id"]})
    return {"markup_id": edited[0]["markup_id"], "edited": edited, "count": len(edited)}

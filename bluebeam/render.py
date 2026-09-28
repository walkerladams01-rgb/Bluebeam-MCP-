"""Render PDF pages or regions to PNG so Claude can look at a drawing.

Coordinates: PDF points, origin top-left, in the page's UNROTATED space (the same space as markup
rects and text hits). On a rotated page (/Rotate 90 or 270) the image is drawn rotated, as Revu shows
it: clip rectangles are converted for you, and the result carries `px_to_pt` to turn image pixels back
into unrotated points. On unrotated pages pixels = points x dpi / 72 from the clip's top-left corner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import pymupdf

from . import core

HARD_MAX_PX = 2400          # longest side of any image returned
MIN_MAX_PX = 64
MAX_DERIVED_DPI = 600       # a tiny clip is not blown up beyond this


@dataclass
class Rendered:
    png: bytes
    width_px: int
    height_px: int
    dpi: float
    clip: list[float]       # the region drawn, unrotated points [x0, y0, x1, y1]
    rotation: int = 0       # the page's /Rotate
    clip_image: Optional[list[float]] = None    # rotated pages: the clip in the rotated (displayed) page space
    px_to_pt: Optional[list[float]] = None      # rotated pages: [a, b, c, d, e, f]: pixel (px, py) ->
    #                                             unrotated point x = a*px + c*py + e, y = b*px + d*py + f

    def meta(self) -> dict:
        out = {"width_px": self.width_px, "height_px": self.height_px, "dpi": round(self.dpi, 1),
               "clip": [round(v, 1) for v in self.clip]}
        if self.rotation:
            out.update(rotation=self.rotation, clip_image=[round(v, 1) for v in self.clip_image or []],
                       px_to_pt=[round(v, 6) for v in self.px_to_pt or []],
                       note="page is rotated: image axes are rotated; convert pixels to unrotated points "
                            "(the space of markup and text coordinates) with px_to_pt")
        return out


def _clip_rect(page: pymupdf.Page, clip: Optional[Sequence[float]]) -> pymupdf.Rect:
    """Requested clip (unrotated points) limited to the page; DocumentError if empty or malformed."""
    bounds = page.rect if not page.rotation else page.rect * page.derotation_matrix
    bounds = pymupdf.Rect(bounds).normalize()
    if clip is None:
        return bounds
    if len(clip) != 4:
        raise core.DocumentError("clip must be [x0, y0, x1, y1] in PDF points")
    r = pymupdf.Rect(*[float(v) for v in clip]).normalize() & bounds
    if r.is_empty or r.width < 1 or r.height < 1:
        raise core.DocumentError(f"clip {list(clip)} is outside the page ({bounds.width:.0f} x "
                                 f"{bounds.height:.0f} pt) or smaller than 1 pt")
    return r


def _render(page: pymupdf.Page, rect: pymupdf.Rect, dpi: float, annots: bool) -> Rendered:
    zoom = dpi / 72.0
    shown = pymupdf.Rect(rect * page.rotation_matrix).normalize() if page.rotation else rect   # pixmaps use the rotated space
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=shown, annots=annots, alpha=False)
    out = Rendered(pix.tobytes("png"), pix.width, pix.height, dpi, [rect.x0, rect.y0, rect.x1, rect.y1],
                   rotation=page.rotation)
    if page.rotation:
        # pixel (i, j) sits at (pix.x + i, pix.y + j) / zoom in the rotated page space; undo the rotation
        m = pymupdf.Matrix(1 / zoom, 0, 0, 1 / zoom, pix.x / zoom, pix.y / zoom) * page.derotation_matrix
        out.clip_image = [shown.x0, shown.y0, shown.x1, shown.y1]
        out.px_to_pt = [m.a, m.b, m.c, m.d, m.e, m.f]
    return out


def _fit_dpi(rect: pymupdf.Rect, max_px: int, dpi: Optional[float]) -> float:
    """dpi so the longest side is <= max_px (and <= the hard cap); a requested dpi is only lowered."""
    longest = max(rect.width, rect.height)
    cap = min(int(max_px), HARD_MAX_PX)
    fit = cap * 72.0 / longest
    return min(dpi, fit) if dpi else min(fit, MAX_DERIVED_DPI)


def render_page_info(doc: pymupdf.Document, pno: int, max_px: int = 1600,
                     clip: Optional[Sequence[float]] = None, annots: bool = True,
                     dpi: Optional[float] = None) -> Rendered:
    """Render a 0-based page (or a clip of it) as PNG, longest side <= max_px (hard cap 2400).

    dpi is derived from max_px unless given (a given dpi is lowered if the image would exceed max_px).
    Returns the PNG with its pixel size, dpi and the clip actually drawn.
    """
    if not 0 <= pno < doc.page_count:
        raise core.DocumentError(f"Page {pno + 1} out of range (document has {doc.page_count} pages)")
    if max_px < MIN_MAX_PX:
        raise core.DocumentError(f"max_px must be at least {MIN_MAX_PX}")
    page = doc[pno]
    rect = _clip_rect(page, clip)
    return _render(page, rect, _fit_dpi(rect, max_px, dpi), annots)


def render_page(doc: pymupdf.Document, pno: int, max_px: int = 1600, clip: Optional[Sequence[float]] = None,
                annots: bool = True, dpi: Optional[float] = None) -> bytes:
    """PNG bytes of render_page_info()."""
    return render_page_info(doc, pno, max_px, clip, annots, dpi).png


def render_markup(doc: pymupdf.Document, page: pymupdf.Page, annot: pymupdf.Annot, margin: float = 36,
                  dpi: float = 150) -> bytes:
    """PNG bytes of render_markup_info()."""
    return render_markup_info(doc, page, annot, margin, dpi).png


def render_markup_info(doc: pymupdf.Document, page: pymupdf.Page, annot: pymupdf.Annot, margin: float = 36,
                       dpi: float = 150) -> Rendered:
    """Render the area around a markup, including the markups grouped under it, plus a margin (points)."""
    rect = pymupdf.Rect(annot.rect)
    xref = annot.xref
    for other in page.annots():
        if other.xref != xref and _group_parent(doc, other) == xref:
            rect |= other.rect
    margin = max(0.0, float(margin))
    rect = pymupdf.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin)
    rect = _clip_rect(page, [rect.x0, rect.y0, rect.x1, rect.y1])
    return _render(page, rect, _fit_dpi(rect, HARD_MAX_PX, dpi), True)


def _group_parent(doc: pymupdf.Document, annot: pymupdf.Annot) -> Optional[int]:
    """xref of the markup this one is grouped under (/IRT with /RT /Group), else None."""
    if doc.xref_get_key(annot.xref, "RT")[1] != "/Group":
        return None
    kind, val = doc.xref_get_key(annot.xref, "IRT")
    return int(val.split()[0]) if kind == "xref" else None

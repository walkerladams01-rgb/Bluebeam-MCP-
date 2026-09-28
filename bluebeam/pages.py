"""Page-level PDF operations with PyMuPDF: info, rotate, delete, extract, merge, split, insert, flatten.

Every writer goes through core.open_for_write, so it either saves a NEW file (output_path; the
source stays byte-identical) or edits in place with a backup in .bb_backups. Pages are 1-based.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence, Union

import pymupdf

from bluebeam import core
from bluebeam.core import DocumentError, PageSpec

MAX_INFO_PAGES = 200


# --- helpers --------------------------------------------------------------------------

def _pdf_date(raw: Optional[str]) -> Optional[str]:
    """'D:20240131101500+01'00'' -> '2024-01-31T10:15:00' (time zone dropped); unparseable -> raw."""
    if not raw:
        return None
    m = re.match(r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?", raw)
    if not m:
        return raw
    y, mo, d, h, mi, s = (g or default for g, default in
                          zip(m.groups(), ("0001", "01", "01", "00", "00", "00")))
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _runs(indices: Sequence[int]) -> list[tuple[int, int]]:
    """Sorted 0-based indices -> inclusive (first, last) runs of consecutive pages."""
    runs: list[tuple[int, int]] = []
    for i in indices:
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _compact(indices: Sequence[int]) -> str:
    """0-based indices -> 1-based text like '1-3,5'."""
    return ",".join(str(lo + 1) if lo == hi else f"{lo + 1}-{hi + 1}" for lo, hi in _runs(indices))


def _require_new_file(path: str, output_path: Optional[str]) -> None:
    """Extract-style tools never touch the source: output_path is required and must differ."""
    if not output_path or not str(output_path).strip():
        raise DocumentError("output_path is required (this tool never changes the source PDF)")
    if core.check_pdf_path(output_path, must_exist=False) == core.check_pdf_path(path):
        raise DocumentError("output_path must be a different file from the source PDF")


# --- info -----------------------------------------------------------------------------

def document_info(path: str) -> dict:
    """File size, metadata, layers/labels flags and a per-page summary (first 200 pages)."""
    p = core.check_pdf_path(path)
    size = p.stat().st_size
    with core.open_readonly(str(p)) as doc:
        meta = doc.metadata or {}
        totals: Counter = Counter()
        pages = []
        for i, page in enumerate(doc):
            counts = Counter(a.type[1] for a in page.annots())
            totals.update(counts)
            if i < MAX_INFO_PAGES:
                rect = page.rect  # as displayed, i.e. after /Rotate
                w, h = rect.width / 72, rect.height / 72
                pages.append({
                    "number": i + 1,
                    "label": core.decode_label(page.get_label()),
                    "width_in": round(w, 2),
                    "height_in": round(h, 2),
                    "orientation": "landscape" if w > h else "portrait",
                    "rotation": page.rotation,
                    "annotations": dict(counts),
                })
        producer, creator = meta.get("producer") or "", meta.get("creator") or ""
        info = {
            "path": str(p),
            "file_size_bytes": size,
            "file_size_mb": round(size / 1_048_576, 2),
            "page_count": doc.page_count,
            "metadata": {
                "title": meta.get("title") or "",
                "author": meta.get("author") or "",
                "subject": meta.get("subject") or "",
                "creator": creator,
                "producer": producer,
                "created": _pdf_date(meta.get("creationDate")),
                "modified": _pdf_date(meta.get("modDate")),
            },
            "is_bluebeam": "bluebeam" in f"{producer} {creator}".lower(),
            "encrypted": bool(doc.is_encrypted),
            "has_layers": bool(doc.get_ocgs()),
            "has_page_labels": bool(doc.get_page_labels()),
            "annotation_counts": dict(totals),
            "pages": pages,
        }
        if doc.page_count > MAX_INFO_PAGES:
            info["pages_note"] = (f"Per-page details cover only the first {MAX_INFO_PAGES} of "
                                  f"{doc.page_count} pages; annotation_counts covers all pages.")
    return info


# --- page operations ------------------------------------------------------------------

def rotate_pages(path: str, pages: PageSpec, degrees: int, output_path: Optional[str] = None) -> dict:
    """Rotate pages clockwise by degrees (a non-zero multiple of 90), relative to their current rotation."""
    if not isinstance(degrees, int) or degrees % 90 != 0 or degrees % 360 == 0:
        raise DocumentError("degrees must be 90, 180, 270 or -90 (a non-zero multiple of 90)")
    with core.open_for_write(path, output_path) as (doc, result):
        idx = core.parse_page_range(pages, doc.page_count)
        for i in idx:
            page = doc[i]
            page.set_rotation((page.rotation + degrees) % 360)
        page_count = doc.page_count
    return {"pages_rotated": _compact(idx), "degrees": degrees,
            "page_count": page_count, "write": result.to_dict()}


def delete_pages(path: str, pages: PageSpec, output_path: Optional[str] = None) -> dict:
    """Delete pages; refuses to delete every page."""
    with core.open_for_write(path, output_path, full_save=True) as (doc, result):
        before = doc.page_count
        idx = core.parse_page_range(pages, before)
        if len(idx) == before:
            raise DocumentError("Refusing to delete every page of the document")
        doc.delete_pages(idx)
        after = doc.page_count
    return {"pages_deleted": _compact(idx), "pages_before": before, "pages_after": after,
            "write": result.to_dict()}


def extract_pages(path: str, pages: PageSpec, output_path: str) -> dict:
    """Copy pages into a NEW PDF; the source is never touched."""
    _require_new_file(path, output_path)
    with core.open_for_write(path, output_path) as (doc, result):
        before = doc.page_count
        idx = core.parse_page_range(pages, before)
        doc.select(idx)  # keeps metadata and the bookmarks that still point at kept pages
        after = doc.page_count
    return {"pages_extracted": _compact(idx), "pages_before": before, "pages_after": after,
            "write": result.to_dict()}


def merge_pdfs(paths: Sequence[str], output_path: str) -> dict:
    """Append the PDFs in order into a NEW file. The first file's metadata, layers and labels carry over;
    bookmarks of all files are kept (later files' pages are offset)."""
    if not paths or len(paths) < 2:
        raise DocumentError("Give at least two PDFs to merge")
    sources = [core.check_pdf_path(p) for p in paths]
    if core.check_pdf_path(output_path, must_exist=False) in sources:
        raise DocumentError("output_path must not be one of the input files")
    with core.open_for_write(str(sources[0]), output_path) as (doc, result):
        counts = [doc.page_count]
        extra_toc: list[list] = []
        for src_path in sources[1:]:
            with core.open_readonly(str(src_path)) as src:
                offset = doc.page_count
                doc.insert_pdf(src)  # pages with their markups, links and form fields
                counts.append(src.page_count)
                extra_toc += [[lvl, title, pg + offset] for lvl, title, pg in src.get_toc(simple=True) if pg >= 1]
        if extra_toc:
            try:
                doc.set_toc(doc.get_toc(simple=False) + extra_toc)
            except Exception as e:  # MuPDF raises several types for odd outlines; pages still merge
                core.log.warning("Merged without the later files' bookmarks: %s", e)
        after = doc.page_count
    return {"sources": [{"path": str(s), "pages": n} for s, n in zip(sources, counts)],
            "pages_after": after, "write": result.to_dict()}


def split_pdf(path: str, output_dir: str, ranges: Union[None, str, Sequence[Union[str, int]]] = None) -> dict:
    """Write one new PDF per range (default: one per page) into an existing folder; never overwrites."""
    src = core.check_pdf_path(path)
    out_dir = Path(os.path.expandvars(os.path.expanduser(str(output_dir).strip().strip('"')))).absolute()
    if not out_dir.is_dir():
        raise DocumentError(f"Output folder does not exist: {out_dir}")
    with core.open_readonly(str(src)) as doc:
        n = doc.page_count
    if ranges is None or (not isinstance(ranges, (str, int)) and len(ranges) == 0):
        groups = [[i] for i in range(n)]
    else:
        specs = [ranges] if isinstance(ranges, (str, int)) else list(ranges)
        groups = [core.parse_page_range(spec, n) for spec in specs]
    targets = [out_dir / f"{src.stem} p{_compact(g)}.pdf" for g in groups]
    dupes = sorted({t.name for t in targets if targets.count(t) > 1})
    if dupes:
        raise DocumentError(f"Ranges overlap into the same output file name: {', '.join(dupes)}")
    existing = [t.name for t in targets if t.exists()]
    if existing:
        shown = ", ".join(existing[:5]) + (" ..." if len(existing) > 5 else "")
        raise DocumentError(f"Refusing to overwrite existing files in {out_dir}: {shown}")
    files = []
    for group, target in zip(groups, targets):
        with core.open_for_write(str(src), str(target)) as (doc, _result):
            doc.select(group)
        files.append({"path": str(target), "pages": _compact(group), "page_count": len(group)})
    return {"source_pages": n, "files": files}


def insert_pages(path: str, source_path: str, at_page: int, source_pages: PageSpec = None,
                 output_path: Optional[str] = None) -> dict:
    """Insert pages of another PDF before at_page (page_count + 1 appends)."""
    source = core.check_pdf_path(source_path)
    with core.open_for_write(path, output_path, full_save=True) as (doc, result):
        before = doc.page_count
        if not isinstance(at_page, int) or at_page < 1 or at_page > before + 1:
            raise DocumentError(f"at_page {at_page} out of range (1 to {before + 1}; {before + 1} appends)")
        with core.open_readonly(str(source)) as src:
            idx = core.parse_page_range(source_pages, src.page_count)
            pos = at_page - 1
            for lo, hi in _runs(idx):
                doc.insert_pdf(src, from_page=lo, to_page=hi, start_at=pos)
                pos += hi - lo + 1
        after = doc.page_count
    return {"pages_inserted": len(idx), "inserted_at": at_page, "pages_before": before,
            "pages_after": after, "write": result.to_dict()}


def flatten(path: str, output_path: Optional[str] = None, in_place: bool = False,
            pages: PageSpec = None) -> dict:
    """Bake markups into the page content (they stay visible but are no longer editable).

    Links and form fields are left alone. With pages, only those pages are flattened: PyMuPDF's
    bake() is document-wide, so the other pages' /Annots are set aside during the bake and restored.
    """
    src = core.check_pdf_path(path)
    if in_place and output_path:
        raise DocumentError("Pass either in_place=True or output_path, not both")
    if in_place:
        target = None
    else:
        target = output_path or str(src.with_name(f"{src.stem} (flattened).pdf"))
        if core.check_pdf_path(target, must_exist=False) == src:
            raise DocumentError("output_path is the source file; pass in_place=True to overwrite it")
    with core.open_for_write(str(src), target, full_save=True) as (doc, result):
        idx = core.parse_page_range(pages, doc.page_count)
        before = sum(len(list(doc[i].annots())) for i in idx)
        if before == 0:
            raise DocumentError("Nothing to flatten: no markups on the selected pages")
        aside: dict[int, str] = {}
        for i in sorted(set(range(doc.page_count)) - set(idx)):
            xref = doc[i].xref
            kind, value = doc.xref_get_key(xref, "Annots")
            if kind != "null":
                aside[xref] = value
                doc.xref_set_key(xref, "Annots", "null")
        try:
            doc.bake(annots=True, widgets=False)
        finally:
            for xref, value in aside.items():
                doc.xref_set_key(xref, "Annots", value)
        remaining = sum(len(list(doc[i].annots())) for i in idx)
    return {"pages_flattened": _compact(idx), "markups_flattened": before - remaining,
            "markups_remaining": remaining, "write": result.to_dict()}

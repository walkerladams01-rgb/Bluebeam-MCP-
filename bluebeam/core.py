"""Shared contracts: errors, path checks, page ranges, markup ids and safe writes.

Owned by the orchestrator; the other modules import from here and never edit it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Sequence, Union

import pymupdf
from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger("bluebeam")  # server.py sends this to stderr; stdout is the MCP channel

BACKUP_DIR = ".bb_backups"
_PROTECTED = [Path(os.environ.get("SystemRoot", r"C:\Windows")),
              Path(r"C:\Program Files\Bluebeam Software")]


class BluebeamError(ToolError):
    """Base error; its message is shown to the calling LLM."""


class NotAvailable(BluebeamError):
    """A Revu feature is not usable here (license tier, Revu not running/installed)."""


class DocumentError(BluebeamError):
    """Bad path or PDF, page out of range, markup or layer not found, output exists."""


class FileLocked(BluebeamError):
    """The PDF is open in Revu or held by another program, so the write was refused."""


# --- paths ---------------------------------------------------------------

def check_pdf_path(path: str, must_exist: bool = True) -> Path:
    """Normalize a user path (~, %VARS%, relative) to an absolute .pdf Path.

    must_exist=True -> DocumentError unless it is an existing file.
    """
    if not path or not str(path).strip():
        raise DocumentError("No PDF path given")
    p = Path(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))).absolute()
    if p.suffix.lower() != ".pdf":
        raise DocumentError(f"Not a .pdf path: {p}")
    if must_exist and not p.is_file():
        raise DocumentError(f"File not found: {p}")
    return p


def check_writable_target(p: Path) -> None:
    """DocumentError if p lies inside Windows or the Bluebeam install folder."""
    for root in _PROTECTED:
        try:
            Path(p).relative_to(root)
        except ValueError:
            continue
        raise DocumentError(f"Refusing to write inside {root}")


# --- pages ---------------------------------------------------------------

PageSpec = Union[None, int, str, Sequence[int]]


def parse_page_range(spec: PageSpec, page_count: int) -> list[int]:
    """1-based page spec -> sorted unique 0-based indices.

    None / "" / "all" / "-1" / -1 = every page; 3 = page 3; "1,3-5" ; "4-" = 4 to end;
    [1, 2] = pages 1 and 2. DocumentError on anything out of range.
    """
    if spec is None or spec == -1 or (isinstance(spec, str) and spec.strip().lower() in ("", "all", "-1")):
        return list(range(page_count))
    if isinstance(spec, int):
        nums = [spec]
    elif isinstance(spec, str):
        nums = []
        for part in spec.replace(" ", "").split(","):
            if not part:
                continue
            m = re.fullmatch(r"(\d+)(?:-(\d*))?", part)
            if not m:
                raise DocumentError(f"Bad page range '{spec}' (use e.g. '1,3-5' or 'all')")
            lo = int(m.group(1))
            if m.group(2) is None:
                nums.append(lo)
            else:
                hi = int(m.group(2)) if m.group(2) else page_count
                if hi < lo:
                    raise DocumentError(f"Bad page range '{part}'")
                nums.extend(range(lo, hi + 1))
    else:
        nums = [int(n) for n in spec]
    bad = [n for n in nums if n < 1 or n > page_count]
    if bad:
        raise DocumentError(f"Page {bad[0]} out of range (document has {page_count} pages)")
    return sorted({n - 1 for n in nums})


def page_index(doc: pymupdf.Document, page: int) -> int:
    """1-based page number -> 0-based index, or DocumentError."""
    if not isinstance(page, int) or page < 1 or page > doc.page_count:
        raise DocumentError(f"Page {page} out of range (document has {doc.page_count} pages)")
    return page - 1


def decode_label(raw: Optional[str]) -> str:
    """Decode a PDF string PyMuPDF left as '<FEFF...>' hex (UTF-16 page labels); else return it."""
    if not raw:
        return ""
    s = raw.strip()
    if len(s) > 2 and s[0] == "<" and s[-1] == ">" and re.fullmatch(r"[0-9A-Fa-f\s]+", s[1:-1]):
        data = bytes.fromhex(re.sub(r"\s", "", s[1:-1]))
        if data[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return data.decode("utf-16").lstrip("\ufeff")
        return data.decode("latin-1")
    return s


# --- markup ids ----------------------------------------------------------

def new_markup_id() -> str:
    """A new /NM value (Bluebeam uses GUID-style names; they survive saves)."""
    return str(uuid.uuid4())


def markup_id(doc: pymupdf.Document, annot: pymupdf.Annot) -> str:
    """Stable id for a markup: its /NM string, else 'xref:<n>'.

    PyMuPDF's own default names ('fitz-A0', ...) repeat across pages, so they count as no name.
    """
    kind, val = doc.xref_get_key(annot.xref, "NM")
    if kind == "string" and val and not val.startswith("fitz-"):
        return val
    return f"xref:{annot.xref}"


def find_annot(doc: pymupdf.Document, mid: str) -> tuple[pymupdf.Page, pymupdf.Annot]:
    """Find a markup by id ('<NM>', 'xref:<n>' or a bare xref number).

    Returns (page, annot); keep the page referenced while using the annot.
    """
    mid = str(mid).strip()
    xref = None
    if mid.lower().startswith("xref:"):
        mid_x = mid[5:]
        if mid_x.isdigit():
            xref = int(mid_x)
    elif mid.isdigit():
        xref = int(mid)
    for pno in range(doc.page_count):
        page = doc[pno]
        for annot in page.annots():
            if xref is not None:
                if annot.xref == xref:
                    return page, annot
            elif markup_id(doc, annot) == mid:
                return page, annot
    raise DocumentError(f"Markup not found: {mid}")


# --- opening and saving --------------------------------------------------

def _open(p: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(str(p))
    except Exception as e:  # MuPDF raises several types for damaged files
        raise DocumentError(f"Could not open PDF {p}: {e}") from None
    if doc.needs_pass:
        doc.close()
        raise DocumentError(f"PDF is password protected: {p}")
    return doc


@contextmanager
def open_readonly(path: str) -> Iterator[pymupdf.Document]:
    """Open a PDF for reading; always closed on exit. Nothing is ever saved."""
    doc = _open(check_pdf_path(path))
    try:
        yield doc
    finally:
        doc.close()


def revu_active_title() -> str:
    """Title of Revu's main window ('<file stem>[*] - Bluebeam Revu x64'), or '' if Revu is not running.

    Revu does NOT lock the PDFs it has open and shows only the ACTIVE tab in its title,
    so this is a best-effort check, not a guarantee.
    """
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return ""
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value.endswith("Bluebeam Revu x64") or buf.value.endswith("Bluebeam Revu"):
                titles.append(buf.value)
        return True

    user32.EnumWindows(_cb, 0)
    return titles[0] if titles else ""


def is_locked(path: Path) -> bool:
    """True if another program holds the file (append-open or same-name rename fails),
    or Revu's active tab shows this file. Revu itself does not lock files."""
    p = Path(path)
    try:
        with open(p, "ab"):
            pass
        os.rename(p, p)
    except PermissionError:
        return True
    except OSError:
        return False
    title = revu_active_title()
    return bool(title) and title.split(" - Bluebeam Revu")[0].rstrip("*").strip().lower() == p.stem.lower()


@dataclass
class WriteResult:
    path: str                 # the file that was written
    backup: Optional[str]     # backup of the original (in-place writes), else None
    in_place: bool
    incremental: bool
    saved: bool = False       # False when an in-place call changed nothing (no backup, no save)
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["warning"] is None:
            del d["warning"]
        return d


@contextmanager
def open_for_write(path: str, output_path: Optional[str] = None, *, backup: bool = True,
                   full_save: bool = False, overwrite: bool = False
                   ) -> Iterator[tuple[pymupdf.Document, WriteResult]]:
    """Open a PDF, let the caller change it, then save safely when the block exits cleanly.

        with open_for_write(path, output_path) as (doc, result):
            ...change doc...
        return {"markup_id": mid, "write": result.to_dict()}   # result is filled in after the block

    output_path given (and different from path): full save to that new file; the source is
    untouched. DocumentError if it exists and overwrite is False.
    In place (output_path None): FileLocked if the file is held open (see is_locked); the
    original is copied to <dir>/.bb_backups/<stem>.<YYYYmmdd-HHMMSS>.pdf first (backup=True);
    incremental save, or with full_save=True (after bake/page ops) a full save to a temp file
    in the same folder + os.replace. An in-place call that changed nothing writes nothing
    (result.saved False). While Revu is running, in-place writes carry result.warning: Revu keeps
    its own copy of open files, so a later save in a Revu tab would discard these edits.
    Encryption is kept. On an exception nothing is written.
    """
    src = check_pdf_path(path)
    dst = check_pdf_path(output_path, must_exist=False) if output_path else src
    in_place = dst == src
    check_writable_target(dst)
    if in_place:
        if is_locked(src):
            raise FileLocked(f"{src.name} is open in Revu or another program. Close it there "
                             "first (Revu would overwrite these edits when it saves), or pass output_path.")
    else:
        if dst.exists() and not overwrite:
            raise DocumentError(f"Output already exists: {dst} (pass overwrite=True to replace it)")
        if dst.exists() and is_locked(dst):
            raise FileLocked(f"{dst.name} is open in Revu or another program; close it before overwriting.")
        if not dst.parent.is_dir():
            raise DocumentError(f"Output folder does not exist: {dst.parent}")
    result = WriteResult(path=str(dst), backup=None, in_place=in_place, incremental=False)
    if in_place and revu_active_title():
        result.warning = ("Revu is running. If this PDF is open in a Revu tab, reopen it there before "
                          "saving in Revu, or Revu's save will discard these edits.")
    doc = _open(src)
    try:
        yield doc, result
        if not in_place:
            doc.save(str(dst), garbage=1, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            result.saved = True
            return
        if not doc.is_dirty:
            return
        result.saved = True
        if backup:
            bdir = src.parent / BACKUP_DIR
            bdir.mkdir(exist_ok=True)
            stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
            bpath, n = bdir / f"{src.stem}.{stamp}.pdf", 1
            while bpath.exists():
                n += 1
                bpath = bdir / f"{src.stem}.{stamp}-{n}.pdf"
            shutil.copy2(src, bpath)
            result.backup = str(bpath)
        if not full_save and doc.can_save_incrementally():
            doc.save(str(src), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            result.incremental = True
            return
        tmp = src.with_name(f".{src.stem}.bbtmp-{uuid.uuid4().hex[:8]}.pdf")
        try:
            doc.save(str(tmp), garbage=1, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            doc.close()
            os.replace(tmp, src)
        finally:
            if tmp.exists():
                tmp.unlink()
    finally:
        if not doc.is_closed:
            doc.close()

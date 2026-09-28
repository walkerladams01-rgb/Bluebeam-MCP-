import os

import pymupdf
import pytest

from bluebeam import core
from bluebeam.core import DocumentError, FileLocked


def test_check_pdf_path(sample_pdf, tmp_path):
    assert core.check_pdf_path(sample_pdf) == core.Path(sample_pdf)
    with pytest.raises(DocumentError):
        core.check_pdf_path(str(tmp_path / "missing.pdf"))
    with pytest.raises(DocumentError):
        core.check_pdf_path(str(tmp_path / "x.txt"), must_exist=False)
    assert core.check_pdf_path(str(tmp_path / "new.pdf"), must_exist=False).name == "new.pdf"


@pytest.mark.parametrize("spec,expected", [
    (None, [0, 1, 2, 3, 4]), ("all", [0, 1, 2, 3, 4]), (-1, [0, 1, 2, 3, 4]),
    (2, [1]), ("1,3-4", [0, 2, 3]), ("4-", [3, 4]), ([5, 1], [0, 4]),
])
def test_parse_page_range(spec, expected):
    assert core.parse_page_range(spec, 5) == expected


@pytest.mark.parametrize("spec", ["6", "0", "3-1", "a-b", [9]])
def test_parse_page_range_bad(spec):
    with pytest.raises(DocumentError):
        core.parse_page_range(spec, 5)


def test_decode_label():
    raw = "<FEFF" + "7 SITE".encode("utf-16-be").hex().upper() + ">"
    assert core.decode_label(raw) == "7 SITE"
    assert core.decode_label("C-101") == "C-101"
    assert core.decode_label(None) == ""


def test_markup_ids_and_find(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    page = doc[0]
    a = page.add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    b = page.add_rect_annot(pymupdf.Rect(60, 60, 90, 90))
    nm = core.new_markup_id()
    doc.xref_set_key(a.xref, "NM", pymupdf.get_pdf_str(nm))
    doc.xref_set_key(b.xref, "NM", "null")
    assert core.markup_id(doc, a) == nm
    assert core.markup_id(doc, b) == f"xref:{b.xref}"
    assert core.find_annot(doc, nm)[1].xref == a.xref
    assert core.find_annot(doc, f"xref:{b.xref}")[1].xref == b.xref
    assert core.find_annot(doc, str(b.xref))[1].xref == b.xref
    with pytest.raises(DocumentError):
        core.find_annot(doc, "nope")
    doc.close()


def test_write_to_output_leaves_source(sample_pdf, tmp_path):
    before = open(sample_pdf, "rb").read()
    out = str(tmp_path / "out.pdf")
    with core.open_for_write(sample_pdf, out) as (doc, res):
        doc[0].add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    assert res.path == out and not res.in_place and res.backup is None
    assert open(sample_pdf, "rb").read() == before
    with core.open_readonly(out) as d:
        assert len(list(d[0].annots())) == 1
    with pytest.raises(DocumentError):
        with core.open_for_write(sample_pdf, out):
            pass


def test_write_in_place_backs_up(sample_pdf):
    before = open(sample_pdf, "rb").read()
    with core.open_for_write(sample_pdf) as (doc, res):
        doc[0].add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    assert res.in_place and res.incremental
    assert open(res.backup, "rb").read() == before
    assert os.path.dirname(res.backup).endswith(core.BACKUP_DIR)
    with core.open_readonly(sample_pdf) as d:
        assert len(list(d[0].annots())) == 1


def test_full_save_in_place(sample_pdf_3p):
    with core.open_for_write(sample_pdf_3p, full_save=True) as (doc, res):
        doc.delete_page(0)
    assert not res.incremental
    with core.open_readonly(sample_pdf_3p) as d:
        assert d.page_count == 2
    assert not [f for f in os.listdir(os.path.dirname(sample_pdf_3p)) if "bbtmp" in f]


def test_exception_writes_nothing(sample_pdf):
    before = open(sample_pdf, "rb").read()
    with pytest.raises(RuntimeError):
        with core.open_for_write(sample_pdf) as (doc, res):
            doc[0].add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
            raise RuntimeError("boom")
    assert open(sample_pdf, "rb").read() == before


def test_locked_file_refused(sample_pdf):
    win32file = pytest.importorskip("win32file")
    h = win32file.CreateFile(sample_pdf, win32file.GENERIC_READ, win32file.FILE_SHARE_READ,
                             None, win32file.OPEN_EXISTING, 0, None)
    try:
        assert core.is_locked(core.Path(sample_pdf))
        with pytest.raises(FileLocked):
            with core.open_for_write(sample_pdf):
                pass
    finally:
        h.Close()
    assert not core.is_locked(core.Path(sample_pdf))


def test_revu_active_tab_counts_as_locked(sample_pdf, monkeypatch):
    stem = core.Path(sample_pdf).stem
    monkeypatch.setattr(core, "revu_active_title", lambda: f"{stem}* - Bluebeam Revu x64")
    assert core.is_locked(core.Path(sample_pdf))
    monkeypatch.setattr(core, "revu_active_title", lambda: "Other Plans - Bluebeam Revu x64")
    assert not core.is_locked(core.Path(sample_pdf))


def test_refuses_protected_dirs(sample_pdf):
    with pytest.raises(DocumentError):
        with core.open_for_write(sample_pdf, r"C:\Windows\x.pdf"):
            pass


def test_in_place_noop_writes_nothing(sample_pdf):
    before = open(sample_pdf, "rb").read()
    with core.open_for_write(sample_pdf) as (doc, res):
        pass
    assert not res.saved and res.backup is None
    assert open(sample_pdf, "rb").read() == before


def test_backups_never_collide(sample_pdf):
    backups = set()
    for i in range(3):
        with core.open_for_write(sample_pdf) as (doc, res):
            doc[0].add_rect_annot(pymupdf.Rect(10 + i, 10, 50, 50))
        backups.add(res.backup)
    assert len(backups) == 3 and all(os.path.exists(b) for b in backups)


def test_pymupdf_default_names_are_not_ids(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    a = doc[0].add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    doc.xref_set_key(a.xref, "NM", "(fitz-A0)")
    assert core.markup_id(doc, a) == f"xref:{a.xref}"
    doc.close()


def test_warns_when_revu_running(sample_pdf, monkeypatch):
    monkeypatch.setattr(core, "revu_active_title", lambda: "Other Plans - Bluebeam Revu x64")
    with core.open_for_write(sample_pdf) as (doc, res):
        doc[0].add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
    assert "Revu" in res.to_dict()["warning"]
    monkeypatch.setattr(core, "revu_active_title", lambda: "")
    with core.open_for_write(sample_pdf) as (doc, res):
        doc[0].add_rect_annot(pymupdf.Rect(20, 20, 50, 50))
    assert "warning" not in res.to_dict()


def test_overwrite_refuses_locked_output(sample_pdf, make_pdf):
    win32file = pytest.importorskip("win32file")
    out = make_pdf("existing.pdf")
    h = win32file.CreateFile(out, win32file.GENERIC_READ, win32file.FILE_SHARE_READ,
                             None, win32file.OPEN_EXISTING, 0, None)
    try:
        with pytest.raises(FileLocked):
            with core.open_for_write(sample_pdf, out, overwrite=True):
                pass
    finally:
        h.Close()

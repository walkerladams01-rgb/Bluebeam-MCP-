import pytest
import pymupdf


def _make_pdf(path, pages=1, width=612, height=792):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"Sample page {i + 1}")
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def sample_pdf(tmp_path):
    """One 612x792 pt page with the text 'Sample page 1'. Returns a str path."""
    return _make_pdf(tmp_path / "sample.pdf")


@pytest.fixture
def sample_pdf_3p(tmp_path):
    """Three 612x792 pt pages, text 'Sample page N'. Returns a str path."""
    return _make_pdf(tmp_path / "sample3.pdf", pages=3)


@pytest.fixture
def make_pdf(tmp_path):
    """Factory: make_pdf(name, pages=1, width=612, height=792) -> str path in tmp_path."""
    def _f(name="doc.pdf", pages=1, width=612, height=792):
        return _make_pdf(tmp_path / name, pages, width, height)
    return _f


@pytest.fixture(autouse=True)
def _no_revu_title(request, monkeypatch):
    """Tests never see a real running Revu window."""
    if "real_revu_title" not in request.keywords:
        monkeypatch.setattr("bluebeam.core.revu_active_title", lambda: "")

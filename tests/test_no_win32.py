"""Without pywin32 (Mac, Linux, a cloud container) the server still loads; the Revu tools say why they can't run."""
import pytest

from bluebeam import revu_native
from bluebeam.core import NotAvailable


@pytest.fixture
def no_win32(monkeypatch):
    monkeypatch.setattr(revu_native, "pythoncom", None)


def test_revu_status_reports_no_windows(no_win32):
    st = revu_native.revu_status()
    assert st["revu_running"] is False and st["launcher_com_ok"] is False
    assert "needs Windows" in st["note"]


def test_open_in_revu_refuses_without_windows(no_win32, sample_pdf):
    with pytest.raises(NotAvailable, match="needs Windows"):
        revu_native.open_in_revu(sample_pdf)


def test_server_imports():
    import server  # noqa: F401  (would raise ImportError if pywin32 were still required)

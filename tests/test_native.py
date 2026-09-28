import asyncio
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pymupdf
import pytest
import pywintypes
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from bluebeam import core, revu_native
from bluebeam.core import DocumentError, NotAvailable
from bluebeam.tools_native import register_native_tools

SUBSCRIPTION_MSG = ("This feature requires a maximum subscription level. "
                    "Please upgrade to access advanced scripting capabilities.")


def com_error(hresult=-2147221005, text="Invalid class string"):
    return pywintypes.com_error(hresult, text, None, None)


class SyncCom:
    """Stand-in for the COM thread: runs the callable right here."""

    def run(self, fn, timeout=20):
        return fn()


@pytest.fixture(autouse=True)
def fresh_launcher(monkeypatch):
    """No cached Launcher leaks between tests."""
    monkeypatch.setattr(revu_native, "_launcher", None)


@pytest.fixture
def launcher(monkeypatch):
    """A mocked Revu.Launcher on a synchronous COM 'thread'."""
    mock = MagicMock(name="Revu.Launcher")
    dispatch = MagicMock(return_value=mock)
    monkeypatch.setattr("win32com.client.Dispatch", dispatch)
    monkeypatch.setattr(revu_native, "get_com_thread", lambda: SyncCom())
    mock.dispatch = dispatch
    return mock


def call(mcp, name, **args):
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


# --- COM thread --------------------------------------------------------------------------

def test_com_thread_runs_on_its_own_thread():
    com = revu_native.COMThread()
    try:
        assert com.run(lambda: threading.current_thread().name) == "revu-com"
        assert com.run(lambda: 41 + 1) == 42
        # one thread serves every call, so a COM object created in one call is usable in the next
        assert com.run(threading.get_ident) == com.run(threading.get_ident)
        with pytest.raises(ZeroDivisionError):
            com.run(lambda: 1 / 0)
        assert com.run(lambda: "still alive") == "still alive"  # an exception does not kill the loop
    finally:
        com.stop()


def test_com_thread_timeout():
    com = revu_native.COMThread()
    release = threading.Event()
    try:
        with pytest.raises(TimeoutError):
            com.run(lambda: release.wait(5), timeout=0.05)
    finally:
        release.set()
        com.stop()


def test_com_thread_is_a_lazy_singleton(monkeypatch):
    monkeypatch.setattr(revu_native, "_com", None)
    made = []

    class Fake:
        def __init__(self):
            made.append(self)

    monkeypatch.setattr(revu_native, "COMThread", Fake)
    assert made == []
    first = revu_native.get_com_thread()
    assert revu_native.get_com_thread() is first and len(made) == 1


def test_open_uses_the_real_com_thread(sample_pdf, monkeypatch):
    """Whole path with the real COMThread singleton; only win32com.client.Dispatch is faked."""
    mock = MagicMock()
    dispatch_threads = []

    def fake_dispatch(progid):
        dispatch_threads.append(threading.current_thread().name)
        return mock

    monkeypatch.setattr("win32com.client.Dispatch", fake_dispatch)
    res = revu_native.open_in_revu(sample_pdf)
    assert res["page_count"] == 1
    assert dispatch_threads == ["revu-com"]
    mock.EditDocument.assert_called_once_with(str(Path(sample_pdf).absolute()), "")


# --- open_in_revu -------------------------------------------------------------------------

def test_open_edit_calls_editdocument_with_abs_path_and_empty_progid(launcher, sample_pdf_3p):
    res = revu_native.open_in_revu(sample_pdf_3p)
    abs_path = str(Path(sample_pdf_3p).absolute())
    launcher.EditDocument.assert_called_once_with(abs_path, "")
    launcher.ViewDocument.assert_not_called()
    launcher.dispatch.assert_called_once_with("Revu.Launcher")
    assert res == {"opened": abs_path, "mode": "edit", "page_count": 3}


def test_open_view_mode(launcher, sample_pdf):
    res = revu_native.open_in_revu(sample_pdf, " VIEW ")
    launcher.ViewDocument.assert_called_once_with(str(Path(sample_pdf).absolute()), "")
    launcher.EditDocument.assert_not_called()
    assert res["mode"] == "view"


def test_open_relative_path_becomes_absolute(launcher, sample_pdf, monkeypatch):
    monkeypatch.chdir(Path(sample_pdf).parent)
    revu_native.open_in_revu("sample.pdf")
    launcher.EditDocument.assert_called_once_with(str(Path(sample_pdf).absolute()), "")


def test_open_validates_before_touching_revu(launcher, tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(DocumentError, match="File not found"):
        revu_native.open_in_revu(str(tmp_path / "missing.pdf"))
    with pytest.raises(DocumentError, match="Could not open PDF"):
        revu_native.open_in_revu(str(bad))
    with pytest.raises(DocumentError, match="Not a .pdf"):
        revu_native.open_in_revu(str(tmp_path / "x.txt"))
    with pytest.raises(DocumentError, match="mode"):
        revu_native.open_in_revu(str(bad), "delete")
    launcher.dispatch.assert_not_called()
    launcher.EditDocument.assert_not_called()


def test_open_password_protected_pdf_has_no_page_count(launcher, sample_pdf, tmp_path):
    src = pymupdf.open(sample_pdf)
    locked = str(tmp_path / "locked.pdf")
    src.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    src.close()
    res = revu_native.open_in_revu(locked)
    assert res["page_count"] is None
    launcher.EditDocument.assert_called_once()


def test_launcher_is_cached_between_calls(launcher, sample_pdf):
    revu_native.open_in_revu(sample_pdf)
    revu_native.open_in_revu(sample_pdf, "view")
    assert launcher.dispatch.call_count == 1


def test_not_available_when_revu_cannot_start(monkeypatch, sample_pdf):
    dispatch = MagicMock(side_effect=com_error())
    monkeypatch.setattr("win32com.client.Dispatch", dispatch)
    monkeypatch.setattr(revu_native, "get_com_thread", lambda: SyncCom())
    with pytest.raises(NotAvailable, match="not installed or could not start"):
        revu_native.open_in_revu(sample_pdf)
    assert dispatch.call_count == 2  # tried once, dropped the cache, retried once


def test_com_error_drops_cached_launcher_and_retries_once(monkeypatch, sample_pdf):
    dead, alive = MagicMock(name="dead"), MagicMock(name="alive")
    dead.EditDocument.side_effect = com_error(-2147023174, "The RPC server is unavailable")
    dispatch = MagicMock(side_effect=[dead, alive])
    monkeypatch.setattr("win32com.client.Dispatch", dispatch)
    monkeypatch.setattr(revu_native, "get_com_thread", lambda: SyncCom())
    res = revu_native.open_in_revu(sample_pdf)
    assert res["mode"] == "edit"
    assert dispatch.call_count == 2 and dead.EditDocument.call_count == 1 and alive.EditDocument.call_count == 1
    revu_native.open_in_revu(sample_pdf)  # the new launcher is the cached one now
    assert dispatch.call_count == 2 and alive.EditDocument.call_count == 2


def test_persistent_method_failure_is_not_available(launcher, sample_pdf):
    launcher.EditDocument.side_effect = com_error(-2147352567, "Exception occurred")
    with pytest.raises(NotAvailable, match="Exception occurred"):
        revu_native.open_in_revu(sample_pdf)
    assert launcher.EditDocument.call_count == 2


def test_com_timeout_becomes_not_available(monkeypatch, sample_pdf):
    class Hung:
        def run(self, fn, timeout=20):
            raise TimeoutError

    monkeypatch.setattr(revu_native, "get_com_thread", lambda: Hung())
    with pytest.raises(NotAvailable, match="did not answer"):
        revu_native.open_in_revu(sample_pdf)


# --- bb_revu_status -----------------------------------------------------------------------

CSV_RUNNING = '"Revu.exe","19768","Console","1","979,228 K"\r\n'
CSV_NONE = "INFO: No tasks are running which match the specified criteria.\r\n"


@pytest.fixture
def revu_tree(tmp_path, monkeypatch):
    """A fake install: <root>/21/Revu/{Revu.exe, ScriptEngine.exe, mcp/Bluebeam MCP Server.exe}."""
    root = tmp_path / "Bluebeam Revu"
    revu_dir = root / "21" / "Revu"
    (revu_dir / "mcp").mkdir(parents=True)
    for rel in ("Revu.exe", "ScriptEngine.exe", "mcp/Bluebeam MCP Server.exe"):
        (revu_dir / rel).write_bytes(b"MZ")
    monkeypatch.setattr(revu_native, "_INSTALL_ROOT", root)
    return SimpleNamespace(root=root, dir=revu_dir, exe=revu_dir / "Revu.exe")


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run: tasklist output and the ScriptEngine reply are set per test."""
    state = SimpleNamespace(tasklist=CSV_RUNNING, script_out=SUBSCRIPTION_MSG, script_err="",
                            script_exc=None, calls=[])

    def run(args, **kwargs):
        state.calls.append((list(args), kwargs))
        if args[0] == "tasklist":
            return subprocess.CompletedProcess(args, 0, state.tasklist, "")
        if Path(args[0]).name.lower() == "scriptengine.exe":
            if state.script_exc:
                raise state.script_exc
            return subprocess.CompletedProcess(args, 1, state.script_out, state.script_err)
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(revu_native.subprocess, "run", run)
    return state


def test_status_running_and_unlicensed(revu_tree, fake_run, monkeypatch):
    monkeypatch.setattr(revu_native, "_process_image_path", lambda pid: str(revu_tree.exe) if pid == 19768 else None)
    monkeypatch.setattr(revu_native.win32api, "GetFileVersionInfo",
                        lambda exe, sub: {"FileVersionMS": (21 << 16) | 11, "FileVersionLS": 23287})
    monkeypatch.setattr(core, "revu_active_title", lambda: "Plans (rev 2)* - Bluebeam Revu x64")
    monkeypatch.setattr(revu_native, "_launcher_registered", lambda: True)
    s = revu_native.revu_status()
    assert s["revu_running"] is True and s["revu_pid"] == 19768
    assert s["revu_exe"] == str(revu_tree.exe) and s["revu_version"] == "21.11.0.23287"
    assert s["active_tab"] == "Plans (rev 2)*"
    assert s["launcher_com_ok"] is True
    assert s["scriptengine"] == {"present": True, "licensed": False, "message": SUBSCRIPTION_MSG}
    mcp_exe = revu_tree.dir / "mcp" / "Bluebeam MCP Server.exe"
    assert s["official_mcp"]["present"] is True and s["official_mcp"]["path"] == str(mcp_exe)
    assert "Max subscription" in s["official_mcp"]["note"] and "MCP Enabled" in s["official_mcp"]["note"]
    # ScriptEngine ran once with -help, hidden, with a 15 s cap; nothing else (in particular not the MCP server)
    script_calls = [c for c in fake_run.calls if c[0][0] != "tasklist"]
    assert len(script_calls) == 1
    args, kwargs = script_calls[0]
    assert args == [str(revu_tree.dir / "ScriptEngine.exe"), "-help"]
    assert kwargs["timeout"] == 15 and kwargs["creationflags"] == revu_native._NO_WINDOW
    assert not any("MCP Server" in str(c[0]) for c in fake_run.calls)


def test_status_licensed_scriptengine(revu_tree, fake_run, monkeypatch):
    fake_run.script_out = "Usage: ScriptEngine.exe [options] script.bbscript\n  -help  show this text"
    monkeypatch.setattr(revu_native, "_process_image_path", lambda pid: str(revu_tree.exe))
    s = revu_native.revu_status()
    assert s["scriptengine"]["licensed"] is True and "Usage" in s["scriptengine"]["message"]
    fake_run.script_out = ""
    assert revu_native.revu_status()["scriptengine"]["licensed"] is None  # no output: cannot tell


def test_status_scriptengine_timeout(revu_tree, fake_run, monkeypatch):
    fake_run.script_exc = subprocess.TimeoutExpired("ScriptEngine.exe", 15)
    monkeypatch.setattr(revu_native, "_process_image_path", lambda pid: str(revu_tree.exe))
    se = revu_native.revu_status()["scriptengine"]
    assert se["present"] is True and se["licensed"] is None and "timed out" in se["message"]


def test_status_revu_not_running_falls_back_to_installed_exe(revu_tree, fake_run, monkeypatch):
    fake_run.tasklist = CSV_NONE
    older = revu_tree.root / "9" / "Revu"
    older.mkdir(parents=True)
    (older / "Revu.exe").write_bytes(b"MZ")
    monkeypatch.setattr(core, "revu_active_title", lambda: "")
    s = revu_native.revu_status()
    assert s["revu_running"] is False and s["revu_pid"] is None
    assert s["revu_exe"] == str(revu_tree.exe)  # newest major version (21 beats 9), not string order
    assert s["revu_version"] is None  # a stub exe has no version resource: no crash, just None
    assert s["active_tab"] == ""
    assert s["scriptengine"]["present"] is True


def test_status_nothing_installed(tmp_path, fake_run, monkeypatch):
    fake_run.tasklist = CSV_NONE
    monkeypatch.setattr(revu_native, "_INSTALL_ROOT", tmp_path / "nothing here")
    s = revu_native.revu_status()
    assert s["revu_exe"] is None and s["revu_version"] is None and s["revu_running"] is False
    assert s["scriptengine"] == {"present": False, "licensed": None, "message": ""}
    assert s["official_mcp"]["present"] is False and s["official_mcp"]["path"] is None
    assert [c[0][0] for c in fake_run.calls] == ["tasklist"]


def test_status_survives_tasklist_failure(revu_tree, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("tasklist")

    monkeypatch.setattr(revu_native.subprocess, "run", boom)
    s = revu_native.revu_status()
    assert s["revu_running"] is False and s["revu_exe"] == str(revu_tree.exe)
    assert s["scriptengine"]["present"] is True and s["scriptengine"]["licensed"] is None


def test_status_running_but_exe_path_unreadable(revu_tree, fake_run, monkeypatch):
    monkeypatch.setattr(revu_native, "_process_image_path", lambda pid: None)
    s = revu_native.revu_status()
    assert s["revu_running"] is True and s["revu_exe"] == str(revu_tree.exe)


@pytest.mark.parametrize("title,expected", [
    ("Plans* - Bluebeam Revu x64", "Plans*"),
    ("A - B - Bluebeam Revu", "A - B"),
    ("Bluebeam Revu x64", ""),
    ("", ""),
])
def test_active_tab_strips_the_suffix(monkeypatch, title, expected):
    monkeypatch.setattr(core, "revu_active_title", lambda: title)
    assert revu_native._active_tab() == expected


def test_launcher_registration_check_never_instantiates(monkeypatch):
    dispatch = MagicMock()
    monkeypatch.setattr("win32com.client.Dispatch", dispatch)
    monkeypatch.setattr(pywintypes, "IID", lambda progid: progid)
    assert revu_native._launcher_registered() is True

    def not_registered(progid):
        raise com_error()

    monkeypatch.setattr(pywintypes, "IID", not_registered)
    assert revu_native._launcher_registered() is False
    dispatch.assert_not_called()


# --- MCP tools ----------------------------------------------------------------------------

def test_native_tools_registered_with_annotations():
    mcp = FastMCP("t")
    register_native_tools(mcp)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"bb_revu_status", "bb_open_in_revu"}
    assert tools["bb_revu_status"].annotations.readOnlyHint is True
    assert tools["bb_open_in_revu"].annotations.readOnlyHint is False
    assert tools["bb_open_in_revu"].annotations.destructiveHint is False
    assert all(t.annotations.openWorldHint is False for t in tools.values())
    assert "cannot close" in tools["bb_open_in_revu"].description


def test_tool_open_in_revu_end_to_end(launcher, sample_pdf_3p):
    mcp = FastMCP("t")
    register_native_tools(mcp)
    res = call(mcp, "bb_open_in_revu", path=sample_pdf_3p, mode="view")
    assert res["page_count"] == 3 and res["mode"] == "view"
    launcher.ViewDocument.assert_called_once_with(str(Path(sample_pdf_3p).absolute()), "")
    res = call(mcp, "bb_open_in_revu", path=sample_pdf_3p)  # default mode = edit
    assert res["mode"] == "edit"
    launcher.EditDocument.assert_called_once_with(str(Path(sample_pdf_3p).absolute()), "")


def test_tool_errors_are_tool_errors(monkeypatch, sample_pdf, tmp_path):
    mcp = FastMCP("t")
    register_native_tools(mcp)
    with pytest.raises(ToolError, match="File not found"):
        call(mcp, "bb_open_in_revu", path=str(tmp_path / "nope.pdf"))
    monkeypatch.setattr("win32com.client.Dispatch", MagicMock(side_effect=com_error()))
    monkeypatch.setattr(revu_native, "get_com_thread", lambda: SyncCom())
    with pytest.raises(ToolError, match="not installed or could not start"):
        call(mcp, "bb_open_in_revu", path=sample_pdf)


def test_tool_status_end_to_end(revu_tree, fake_run, monkeypatch):
    monkeypatch.setattr(revu_native, "_process_image_path", lambda pid: str(revu_tree.exe))
    mcp = FastMCP("t")
    register_native_tools(mcp)
    s = call(mcp, "bb_revu_status")
    assert s["revu_running"] is True and s["scriptengine"]["licensed"] is False


# --- real Revu (never run by default: pytest.ini deselects 'integration') ------------------

@pytest.mark.integration
def test_real_revu_status_is_read_only():
    s = revu_native.revu_status()
    assert set(s) == {"revu_running", "revu_pid", "revu_exe", "revu_version", "active_tab",
                      "launcher_com_ok", "scriptengine", "official_mcp"}

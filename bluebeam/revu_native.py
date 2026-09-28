"""Talk to the running Bluebeam Revu: status, and opening files through Revu.Launcher (COM).

Revu's COM surface is tiny: only the ProgID "Revu.Launcher" exists (EditDocument, ViewDocument,
CreateNewDocument, IsStudioSupported). There is no document model, so nothing here can list,
close or save documents that are open in Revu. ScriptEngine.exe and Bluebeam's own MCP server
need a Bluebeam Max subscription, so they are only reported on, never driven.
"""
from __future__ import annotations

import csv
import ctypes
import io
import os
import queue
import re
import subprocess
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

import pymupdf
import pythoncom
import pywintypes
import win32api
import win32com.client

from bluebeam import core
from bluebeam.core import DocumentError, NotAvailable, log

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_INSTALL_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Bluebeam Software" / "Bluebeam Revu"
_COM_TIMEOUT = 20  # seconds a COM call may take before the tool gives up

_MCP_NOTE = ("Bluebeam's own MCP server ships with Revu 21.9+; it needs a Bluebeam Max subscription and "
             "Revu > Preferences > Admin > MCP > MCP Enabled. If enabled, register it in Claude Code directly.")


# --- COM thread ----------------------------------------------------------------------

class COMThread:
    """One dedicated STA thread for every COM call (Revu.Launcher is apartment-threaded)."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="revu-com", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                fn, future = item
                try:
                    future.set_result(fn())
                except Exception as exc:  # hand every failure to the waiting caller
                    future.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    def run(self, fn: Callable[[], Any], timeout: float = _COM_TIMEOUT) -> Any:
        """Run fn on the COM thread and wait for its result.

        On timeout the caller gets TimeoutError but fn keeps running; if Revu hangs, later calls
        queue behind it until it returns or the server restarts.
        """
        future: Future = Future()
        self._queue.put((fn, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)


_com: Optional[COMThread] = None
_com_lock = threading.Lock()
_launcher: Any = None  # cached Revu.Launcher dispatch; only ever touched on the COM thread


def get_com_thread() -> COMThread:
    """The lazily created COM thread singleton."""
    global _com
    with _com_lock:
        if _com is None:
            _com = COMThread()
        return _com


def _call_launcher(fn: Callable[[Any], Any]) -> Any:
    """Run fn(launcher) on the COM thread. A COM error drops the cached launcher and retries once."""
    def _do() -> Any:
        global _launcher
        last: Optional[pywintypes.com_error] = None
        for _attempt in range(2):
            try:
                if _launcher is None:
                    _launcher = win32com.client.Dispatch("Revu.Launcher")
                return fn(_launcher)
            except pywintypes.com_error as e:
                log.warning("Revu.Launcher COM error: %s", e)
                _launcher = None
                last = e
        detail = (getattr(last, "strerror", "") or str(last)).strip()
        raise NotAvailable(f"Bluebeam Revu is not installed or could not start ({detail})")

    try:
        return get_com_thread().run(_do, timeout=_COM_TIMEOUT)
    except TimeoutError:
        raise NotAvailable(f"Bluebeam Revu did not answer within {_COM_TIMEOUT} s "
                           "(it may be showing a dialog or busy); try again once it is idle") from None


# --- opening files --------------------------------------------------------------------

def _page_count(path: Path) -> Optional[int]:
    """Pages in the PDF, or None when it is password protected (Revu will ask for the password)."""
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:  # MuPDF raises several types for damaged files
        raise DocumentError(f"Could not open PDF {path}: {e}") from None
    try:
        return None if doc.needs_pass else doc.page_count
    finally:
        doc.close()


def open_in_revu(path: str, mode: str = "edit") -> dict:
    """Open a PDF in the running Revu (new tab). mode 'edit' -> EditDocument, 'view' -> ViewDocument."""
    mode = (mode or "").strip().lower()
    if mode not in ("edit", "view"):
        raise DocumentError("mode must be 'edit' or 'view'")
    p = core.check_pdf_path(path)
    page_count = _page_count(p)  # also proves the file is a readable PDF before Revu sees it
    method = "EditDocument" if mode == "edit" else "ViewDocument"
    _call_launcher(lambda launcher: getattr(launcher, method)(str(p), ""))  # varProgID is required
    log.info("Opened %s in Revu (%s)", p, mode)
    return {"opened": str(p), "mode": mode, "page_count": page_count}


# --- status ---------------------------------------------------------------------------

def _process_image_path(pid: int) -> Optional[str]:
    """Full exe path of a process (works with limited rights), or None."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private instance: leave the shared one alone
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
                                               ctypes.POINTER(ctypes.c_ulong)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(handle)


def _revu_process() -> tuple[Optional[int], Optional[str]]:
    """(pid, exe path) of the running Revu.exe, or (None, None)."""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Revu.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=10,
                           creationflags=_NO_WINDOW)
        rows = list(csv.reader(io.StringIO(r.stdout)))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("tasklist failed: %s", e)
        return None, None
    for row in rows:
        if len(row) >= 2 and row[0].lower() == "revu.exe" and row[1].isdigit():
            pid = int(row[1])
            try:
                return pid, _process_image_path(pid)
            except (OSError, AttributeError, ctypes.ArgumentError) as e:
                log.warning("Could not read Revu's exe path: %s", e)
                return pid, None
    return None, None


def _installed_revu_exe() -> Optional[Path]:
    """Newest Revu.exe under Program Files\\Bluebeam Software\\Bluebeam Revu\\<major>\\Revu."""
    def major(p: Path) -> int:
        name = p.parent.parent.name
        return int(name) if name.isdigit() else 0

    found = sorted(_INSTALL_ROOT.glob("*/Revu/Revu.exe"), key=major, reverse=True)
    return found[0] if found else None


def _file_version(exe: Path) -> Optional[str]:
    try:
        info = win32api.GetFileVersionInfo(str(exe), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (pywintypes.error, KeyError, OSError):
        return None


def _active_tab() -> str:
    """Title of Revu's active tab ('' if Revu is not running). A trailing '*' = unsaved changes."""
    title = core.revu_active_title()
    return re.sub(r"(?:\s+-\s+|^)Bluebeam Revu(?: x64)?\s*$", "", title).strip()


def _launcher_registered() -> bool:
    """True if the Revu.Launcher COM class is registered. Never instantiates it (that could start Revu)."""
    try:
        pywintypes.IID("Revu.Launcher")  # ProgID -> CLSID registry lookup
        return True
    except pywintypes.com_error:
        return False


def _scriptengine_status(revu_dir: Optional[Path]) -> dict:
    exe = revu_dir / "ScriptEngine.exe" if revu_dir else None
    if exe is None or not exe.is_file():
        return {"present": False, "licensed": None, "message": ""}
    try:
        r = subprocess.run([str(exe), "-help"], capture_output=True, text=True, errors="replace",
                           timeout=15, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"present": True, "licensed": None, "message": "ScriptEngine.exe -help timed out"}
    except OSError as e:
        return {"present": True, "licensed": None, "message": f"Could not run ScriptEngine.exe: {e}"}
    message = " ".join(f"{r.stdout} {r.stderr}".split())[:300]
    licensed = ("subscription level" not in message.lower()) if message else None
    return {"present": True, "licensed": licensed, "message": message}


def _official_mcp_status(revu_dir: Optional[Path]) -> dict:
    exe = revu_dir / "mcp" / "Bluebeam MCP Server.exe" if revu_dir else None
    present = bool(exe and exe.is_file())
    return {"present": present, "path": str(exe) if present else None, "note": _MCP_NOTE}


def revu_status() -> dict:
    """Snapshot of Revu and its optional scripting surfaces. Caches nothing; never starts Revu."""
    pid, exe = _revu_process()
    exe_path = Path(exe) if exe else _installed_revu_exe()
    revu_dir = exe_path.parent if exe_path else None
    return {
        "revu_running": pid is not None,
        "revu_pid": pid,
        "revu_exe": str(exe_path) if exe_path else None,
        "revu_version": _file_version(exe_path) if exe_path else None,
        "active_tab": _active_tab(),
        "launcher_com_ok": _launcher_registered(),
        "scriptengine": _scriptengine_status(revu_dir),
        "official_mcp": _official_mcp_status(revu_dir),
    }

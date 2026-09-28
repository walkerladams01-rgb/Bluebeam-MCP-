"""MCP tools that talk to the running Bluebeam Revu (status and opening files)."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from bluebeam import revu_native


def register_native_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                          idempotentHint=True, openWorldHint=False))
    def bb_revu_status() -> dict:
        """Report on Bluebeam Revu and its scripting options; read-only, Revu is not started or touched.

        Runs 'ScriptEngine.exe -help' once (a few seconds at most) to learn whether it is licensed.
        Returns revu_running, revu_pid, revu_exe, revu_version, active_tab (title of Revu's
        active tab; a trailing '*' means unsaved changes; '' if Revu is not running),
        launcher_com_ok (the Revu.Launcher COM class is registered), scriptengine
        {present, licensed, message} (licensed is false without a Bluebeam Max subscription),
        and official_mcp {present, path, note} (Bluebeam's own MCP server, only reported).
        Use it to tell whether a PDF might be open in Revu before writing to it.
        """
        return revu_native.revu_status()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                          idempotentHint=False, openWorldHint=False))
    def bb_open_in_revu(path: str, mode: str = "edit") -> dict:
        """Open a PDF in the running Bluebeam Revu as a new tab (starts Revu if needed).

        path: an existing .pdf file. mode: 'edit' (default) or 'view' (read-only viewing).
        Revu's COM interface cannot close or save documents afterwards, so nothing can be
        closed or saved from here; the file on disk is not touched.
        Returns {opened, mode, page_count} (page_count is null for password-protected PDFs).
        Fails with NotAvailable if Revu cannot be started or does not answer within 20 s.
        """
        return revu_native.open_in_revu(path, mode)

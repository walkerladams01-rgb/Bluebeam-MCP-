"""Bluebeam MCP server: Bluebeam-aware PDF tools for Claude (stdio)."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymupdf  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from bluebeam.tools_native import register_native_tools  # noqa: E402
from bluebeam.tools_pages import register_page_tools  # noqa: E402
from bluebeam.tools_read import register_read_tools  # noqa: E402
from bluebeam.tools_write import register_write_tools  # noqa: E402

# stdout is the MCP JSON-RPC channel: logs and MuPDF's own messages go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
pymupdf.set_messages(stream=sys.stderr)
pymupdf.set_log(stream=sys.stderr)

INSTRUCTIONS = """\
Tools for PDFs marked up in Bluebeam Revu (plans, takeoffs, redlines). They work on PDF FILES by path;
Revu itself can only be asked to open a file (bb_open_in_revu).
- Understand a set: bb_document_info, bb_sheet_index, bb_render_page (see a sheet), bb_search_text.
- Quantities: bb_takeoff_summary (areas, lengths, counts, volumes grouped by subject, layer, page...),
  bb_list_markups, bb_export_markups (csv / xlsx / json, like Revu's Markups List).
- Markups: bb_add_text, bb_add_shape (Cloud+ and shapes), bb_add_highlight, bb_add_measurement,
  bb_add_stamp, bb_edit_markups, bb_delete_markups, bb_reply_to_markup. Coordinates are PDF points,
  origin top-left, pages 1-based; find positions with bb_render_page, bb_search_text, bb_get_page_text.
- Writes: pass output_path to write a new file; without it the file is edited in place after the
  original is copied to .bb_backups beside it. Revu does not lock open files and would overwrite
  outside edits when it saves, so writing to the file in Revu's active tab is refused - close it first.
"""

mcp = FastMCP("bluebeam", instructions=INSTRUCTIONS)
register_native_tools(mcp)
register_page_tools(mcp)
register_read_tools(mcp)
register_write_tools(mcp)

if __name__ == "__main__":
    mcp.run()

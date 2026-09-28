"""MCP tools for page-level PDF work (info, rotate, delete, extract, merge, split, insert, flatten).

Pages are 1-based; a page spec is 'all', a number, or text like '1,3-5' or '4-' (4 to the end).
Writers either save a NEW file when output_path is given (the source is left untouched) or edit
the PDF in place after copying the original to a .bb_backups folder beside it. In-place writes are
refused while the file is open in Revu's active tab or locked by another program; use output_path.
Returned page lists (pages_rotated, pages_deleted, ...) are 1-based text such as '1-3,5'.
"""
from __future__ import annotations

from typing import Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from bluebeam import pages as page_ops

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# Writers can replace the PDF in place (with a backup), so they are marked destructive.
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

PageArg = Union[int, str]


def register_page_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations=_READ)
    def bb_document_info(path: str) -> dict:
        """Describe a PDF without changing it.

        Returns file_size_bytes / file_size_mb, page_count, metadata (title, author, subject, creator,
        producer, created, modified), is_bluebeam, encrypted, has_layers, has_page_labels,
        annotation_counts (markups by type, whole document) and pages: one entry per page
        {number, label (page label such as 'C-101', '' if none), width_in, height_in (as displayed),
        orientation, rotation, annotations {type: count}}. Per-page detail stops at 200 pages
        (pages_note says so).
        """
        return page_ops.document_info(path)

    @mcp.tool(annotations=_WRITE)
    def bb_rotate_pages(path: str, pages: PageArg, degrees: int, output_path: Optional[str] = None) -> dict:
        """Rotate pages clockwise by degrees (90, 180, 270 or -90), added to each page's current rotation.

        pages: 'all', 3, or '1,3-5'. Markups stay attached. Writes a new file at output_path, or edits
        path in place (backup in .bb_backups). Returns {pages_rotated, degrees, page_count, write}.
        """
        return page_ops.rotate_pages(path, pages, degrees, output_path)

    @mcp.tool(annotations=_WRITE)
    def bb_delete_pages(path: str, pages: PageArg, output_path: Optional[str] = None) -> dict:
        """Delete pages (refuses to delete every page).

        pages: a number or '1,3-5' style range. Writes a new file at output_path, or edits path in
        place (backup in .bb_backups). Returns {pages_deleted, pages_before, pages_after, write}.
        Bookmarks that pointed at deleted pages may stay in the outline as dead entries.
        """
        return page_ops.delete_pages(path, pages, output_path)

    @mcp.tool(annotations=_WRITE)
    def bb_extract_pages(path: str, pages: PageArg, output_path: str) -> dict:
        """Copy pages into a NEW PDF at output_path (required, must not exist); path is never changed.

        pages: 'all', 3, or '1,3-5'. Markups, links and metadata of the kept pages carry over; page
        labels do not. Returns {pages_extracted, pages_before, pages_after, write}.
        """
        return page_ops.extract_pages(path, pages, output_path)

    @mcp.tool(annotations=_WRITE)
    def bb_merge_pdfs(paths: list[str], output_path: str) -> dict:
        """Merge two or more PDFs, in the order given, into a NEW PDF at output_path (must not exist).

        The sources are never changed. The first file's metadata and layers carry over; bookmarks from
        every file are kept. Returns {sources: [{path, pages}], pages_after, write}.
        """
        return page_ops.merge_pdfs(paths, output_path)

    @mcp.tool(annotations=_WRITE)
    def bb_split_pdf(path: str, output_dir: str, ranges: Union[list[str], str, None] = None) -> dict:
        """Split a PDF into new files inside output_dir (an existing folder); path is never changed.

        ranges None: one file per page, named '<stem> p<N>.pdf'. Otherwise a list like ['1-3', '4-6']
        gives '<stem> p1-3.pdf', '<stem> p4-6.pdf'. Nothing is overwritten: if any target already exists
        the call fails before writing. Returns {source_pages, files: [{path, pages, page_count}]}.
        """
        return page_ops.split_pdf(path, output_dir, ranges)

    @mcp.tool(annotations=_WRITE)
    def bb_insert_pages(path: str, source_path: str, at_page: int, source_pages: Optional[PageArg] = None,
                        output_path: Optional[str] = None) -> dict:
        """Insert pages from source_path into path, before at_page (page_count + 1 appends).

        source_pages: pages of the source to insert ('all' or omitted = every page; '2-4' etc.), kept in
        source order. Writes a new file at output_path, or edits path in place (backup in .bb_backups).
        source_path is never changed. Returns {pages_inserted, inserted_at, pages_before, pages_after, write}.
        """
        return page_ops.insert_pages(path, source_path, at_page, source_pages, output_path)

    @mcp.tool(annotations=_WRITE)
    def bb_flatten(path: str, output_path: Optional[str] = None, in_place: bool = False,
                   pages: Optional[PageArg] = None) -> dict:
        """Flatten markups into the page content: they stay visible but can no longer be edited (irreversible).

        Default output is '<stem> (flattened).pdf' next to the source (fails if it exists); the original is
        changed only with in_place=True (backup in .bb_backups). pages: limit flattening to those pages
        ('all' or omitted = every page). Links and form fields stay live. Fails if there is nothing to flatten.
        Returns {pages_flattened, markups_flattened, markups_remaining, write}.
        """
        return page_ops.flatten(path, output_path, in_place, pages)

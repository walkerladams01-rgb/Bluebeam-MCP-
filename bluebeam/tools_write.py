"""MCP tools that WRITE markups and layers into PDFs. Thin wrappers over markups_write.py.

Every tool goes through core.open_for_write: refused while the PDF is open in Revu, backup in
.bb_backups for in-place writes, or a new file when output_path is given.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from mcp.types import ToolAnnotations

from bluebeam import core, markups_write as mw
from bluebeam.core import BluebeamError, DocumentError

_WRITE = ToolAnnotations(destructiveHint=True, readOnlyHint=False, openWorldHint=False)


def _write(path: str, output_path: Optional[str], fn: Callable[..., dict], *args: Any, **kwargs: Any) -> dict:
    """Open `path` for writing, run fn(doc, *args), save, and return fn's dict plus the write report."""
    try:
        with core.open_for_write(path, output_path) as (doc, result):
            info = fn(doc, *args, **kwargs)
    except BluebeamError:
        raise
    except Exception as e:  # PyMuPDF/MuPDF errors: give the LLM a clean message, keep the traceback in the log
        core.log.exception("write failed: %s", fn.__name__)
        raise DocumentError(f"{type(e).__name__}: {e}") from None
    core.log.info("%s wrote %s", fn.__name__, result.path)
    return {**info, "write": result.to_dict()}


def register_write_tools(mcp) -> None:
    """Register the 10 markup/layer writing tools on a FastMCP server."""

    @mcp.tool(annotations=_WRITE)
    def bb_add_text(path: str, page: int, text: str, kind: str = "box", rect: Optional[list[float]] = None,
                    point: Optional[list[float]] = None, leader_point: Optional[list[float]] = None,
                    font_size: float = 12, color: str = "#FF0000", fill_color: Optional[str] = None,
                    opacity: float = 1.0, line_width: Optional[float] = None, subject: Optional[str] = None,
                    author: str = "Claude", layer: Optional[str] = None,
                    output_path: Optional[str] = None) -> dict:
        """Add a text box, callout or sticky note to a PDF page.

        kind: 'box' = text in rect; 'callout' = text box (rect) plus a leader arrow whose tip is
        leader_point; 'note' = sticky-note icon at point. Give rect=[x0, y0, x1, y1], or only
        point=[x, y] to auto-size a box (box/callout) there. color = text and border color
        ('#RRGGBB' or red, blue, green, yellow, orange, magenta, black); fill_color fills the box
        (default none); line_width defaults to 0 (no border) for boxes, 1 for callouts. subject
        defaults to 'Text Box' / 'Callout' / 'Note'. layer = layer name (created if missing).

        Coordinates are PDF points (1/72 in), origin TOP-LEFT, page 1-based; find positions with
        bb_render_page, bb_search_text or bb_get_page_text. Writes in place (backup in .bb_backups
        next to the file) unless output_path names a new PDF; refused while Revu has the file open.
        Returns {markup_id, page, type, rect, ids, write}.
        """
        return _write(path, output_path, mw.add_text, page, text, kind=kind, rect=rect, point=point,
                      leader_point=leader_point, font_size=font_size, color=color, fill_color=fill_color,
                      opacity=opacity, line_width=line_width, subject=subject, author=author, layer=layer)

    @mcp.tool(annotations=_WRITE)
    def bb_add_shape(path: str, page: int, kind: str = "rectangle", rect: Optional[list[float]] = None,
                     points: Optional[list[list[float]]] = None, text: Optional[str] = None,
                     text_rect: Optional[list[float]] = None, cloudy: bool = False, color: str = "#FF0000",
                     fill_color: Optional[str] = None, opacity: float = 1.0, line_width: float = 1.5,
                     font_size: float = 12, subject: Optional[str] = None, author: str = "Claude",
                     comment: Optional[str] = None, layer: Optional[str] = None,
                     output_path: Optional[str] = None) -> dict:
        """Add a rectangle, ellipse, cloud, polygon, polyline, line or arrow to a PDF page.

        kind: 'rectangle' / 'ellipse' take rect=[x0, y0, x1, y1] (cloudy=True gives a cloudy
        border); 'polygon' needs points=[[x, y], ...] (3+), 'polyline' 2+; 'line' and 'arrow' need
        exactly two points (arrowhead at the second). 'cloud' = Bluebeam Cloud+ from rect or points;
        with text it also adds a grouped callout text box next to the cloud (or at text_rect) with a
        leader arrow - markup_id is then the callout (group parent), cloud_id the cloud. color = line
        color ('#RRGGBB' or red, blue, green, yellow, orange, magenta, black), fill_color fills
        closed shapes (default none), opacity 0-1, line_width in points, comment = the markup's
        comment text, layer = layer name (created if missing), subject defaults to the shape name.

        Coordinates are PDF points (1/72 in), origin TOP-LEFT, page 1-based; find positions with
        bb_render_page, bb_search_text or bb_get_page_text. Writes in place (backup in .bb_backups
        next to the file) unless output_path names a new PDF; refused while Revu has the file open.
        Returns {markup_id, page, type, rect, ids, write} (+ cloud_id, callout_id for clouds).
        """
        return _write(path, output_path, mw.add_shape, page, kind=kind, rect=rect, points=points, text=text,
                      text_rect=text_rect, cloudy=cloudy, color=color, fill_color=fill_color, opacity=opacity,
                      line_width=line_width, font_size=font_size, subject=subject, author=author,
                      comment=comment, layer=layer)

    @mcp.tool(annotations=_WRITE)
    def bb_add_highlight(path: str, page: int, text: Optional[str] = None, rect: Optional[list[float]] = None,
                         color: str = "yellow", opacity: float = 1.0, subject: Optional[str] = None,
                         author: str = "Claude", comment: Optional[str] = None, layer: Optional[str] = None,
                         output_path: Optional[str] = None) -> dict:
        """Highlight text on a PDF page.

        Give text to highlight EVERY occurrence of it on the page (case-insensitive; one highlight
        markup per hit; error if not found - scanned pages have no text, use rect), or rect=[x0, y0,
        x1, y1] to highlight that area. color = '#RRGGBB' or a name (default yellow); highlights
        multiply over the page so text stays readable. comment = comment text on each highlight.

        Coordinates are PDF points (1/72 in), origin TOP-LEFT, page 1-based; find text positions
        with bb_search_text. Writes in place (backup in .bb_backups next to the file) unless
        output_path names a new PDF; refused while Revu has the file open.
        Returns {markup_id (first), ids, count, highlights: [{markup_id, rect}], write}.
        """
        return _write(path, output_path, mw.add_highlight, page, text=text, rect=rect, color=color,
                      opacity=opacity, subject=subject, author=author, comment=comment, layer=layer)

    @mcp.tool(annotations=_WRITE)
    def bb_add_measurement(path: str, page: int, kind: str, points: list[list[float]],
                           scale: Optional[str] = None, units_per_point: Optional[float] = None,
                           unit: str = "ft", depth: Optional[float] = None, depth_unit: str = "in",
                           color: str = "#FF0000", fill_color: Optional[str] = None, opacity: float = 1.0,
                           line_width: float = 1.5, subject: Optional[str] = None, author: str = "Claude",
                           label: Optional[str] = None, layer: Optional[str] = None,
                           output_path: Optional[str] = None) -> dict:
        """EXPERIMENTAL: add a Bluebeam measurement markup (area, perimeter, length or count).

        Writes Revu's full measurement structure so Revu lists and totals it; verify the result by
        rendering the page and opening it in Revu. kind: 'area' (3+ points, closed polygon, e.g.
        '1,736.11 sf'), 'perimeter' (2+ points polyline; repeat the first point to close it),
        'length' (2 points = line, more = polyline; feet-inches like 81'-8 1/2" for ft scales),
        'count' (one checkmark symbol per point; the value is the number of points).
        scale = the DRAWING scale: '1 in = 30 ft', '1/4 in = 1 ft', '1" = 20\'', '1:100' (ratio uses
        `unit`), '1 cm = 5 m'; or units_per_point (real units per PDF point) with unit
        ft/in/yd/mi/m/cm/mm/km. There is NO default: if you pass neither, the page's own scale is
        used (its Revu viewport, else its existing measurements) when it has exactly one; otherwise
        the call fails, because a wrong scale silently scales every quantity. The result's
        scale_source says which was used ('argument', 'page viewport', 'existing markups'). depth
        (+ depth_unit, default in) on an area also records a volume (cu yd for ft scales).
        label = free text label.

        Coordinates are PDF points (1/72 in), origin TOP-LEFT, page 1-based. Writes in place (backup
        in .bb_backups next to the file) unless output_path names a new PDF; refused while Revu has
        the file open. Returns {markup_id, ids, kind, value, unit, formatted, scale, scale_source,
        page, rect, write} (+ volume, volume_unit, depth for volumes; count for counts).
        """
        return _write(path, output_path, mw.add_measurement, page, kind=kind, points=points, scale=scale,
                      units_per_point=units_per_point, unit=unit, depth=depth, depth_unit=depth_unit,
                      color=color, fill_color=fill_color, opacity=opacity, line_width=line_width,
                      subject=subject, author=author, label=label, layer=layer)

    @mcp.tool(annotations=_WRITE)
    def bb_add_stamp(path: str, page: int, stamp: str, rect: list[float], opacity: float = 1.0,
                     subject: Optional[str] = None, author: str = "Claude", comment: Optional[str] = None,
                     layer: Optional[str] = None, output_path: Optional[str] = None) -> dict:
        """Place a stamp on a PDF page.

        stamp = a built-in name (Approved, AsIs, Confidential, Departmental, Draft, Experimental,
        Expired, Final, ForComment, ForPublicRelease, NotApproved, NotForPublicRelease, Sold,
        TopSecret; case and spaces ignored), OR the path of a PNG/JPG image, OR the path of a 1-page
        stamp PDF. Revu's custom stamps are PDFs, typically under
        %APPDATA%\\Bluebeam Software\\Revu\\21\\Stamps - this tool does not browse that folder, pass
        the file path. Image/PDF stamps keep their aspect ratio, centred in rect (PDFs are rasterized
        at up to 200 dpi); built-in stamps fill rect. Suggested size for built-ins: about 150 x 50.
        subject defaults to the stamp name / file name; opacity 0-1.

        rect=[x0, y0, x1, y1] in PDF points (1/72 in), origin TOP-LEFT, page 1-based. Writes in
        place (backup in .bb_backups next to the file) unless output_path names a new PDF; refused
        while Revu has the file open. Returns {markup_id, page, type, rect, ids, stamp, source, write}.
        """
        return _write(path, output_path, mw.add_stamp, page, stamp, rect, opacity=opacity, subject=subject,
                      author=author, comment=comment, layer=layer)

    @mcp.tool(annotations=_WRITE)
    def bb_edit_markups(path: str, markup_ids: list[str], changes: dict[str, Any],
                        output_path: Optional[str] = None) -> dict:
        """Change properties of existing markups (ids come from the markup-listing tools).

        changes is an object with any of: text (or contents), subject, author, color, fill_color
        (null or 'none' removes the fill), opacity (0-1), line_width, layer (name, created if
        missing; null removes the markup from its layer), locked (true/false), hidden (true/false),
        rect ([x0, y0, x1, y1] to move/resize; callouts can only move), custom_columns ({"Column
        name": "value"}; only if the PDF already defines those columns). color, opacity, line_width,
        layer, hidden and locked also apply to the other members of a group (a Cloud+ callout and
        its cloud, the symbols of a count); they are listed in 'edited' with group_of.
        Unknown keys are refused. Keys that do not apply to a markup type (e.g. fill_color on a
        highlight) are reported under 'ignored' for that markup. The same changes apply to every id.
        Locked markups are refused unless the same call unlocks them (locked=false).
        Resizing a measurement (rect) recomputes its value and comment, returned as 'measurement'.

        Coordinates are PDF points, origin TOP-LEFT. Writes in place (backup in .bb_backups next to
        the file) unless output_path names a new PDF; refused while Revu has the file open.
        Returns {markup_id, count, edited: [{markup_id, page, applied, ignored, rect, measurement?}], write}.
        """
        return _write(path, output_path, mw.edit_markups, markup_ids, changes)

    @mcp.tool(annotations=_WRITE)
    def bb_delete_markups(path: str, markup_ids: list[str], delete_replies: bool = True,
                          output_path: Optional[str] = None) -> dict:
        """Delete markups by id (ids come from the markup-listing tools).

        Deleting a group's parent deletes the whole group (a Cloud+ callout takes its cloud, the first
        count symbol takes the count); deleting another count symbol removes just that symbol and
        renumbers the rest (see 'recounted'). Review statuses of a deleted markup are always deleted.
        With delete_replies=True (default) its replies go too; with False they are KEPT as standalone
        notes at the old spot (listed in detached_replies). An unknown id or a locked markup aborts
        the whole call and nothing is deleted. Writes in place (backup in .bb_backups next to the
        file) unless output_path names a new PDF; refused while Revu has the file open.
        Returns {deleted: [ids], count, detached_replies, recounted: [{group, count}], write}.
        """
        return _write(path, output_path, mw.delete_markups, markup_ids, delete_replies=delete_replies)

    @mcp.tool(annotations=_WRITE)
    def bb_reply_to_markup(path: str, markup_id: str, text: Optional[str] = None,
                           status: Optional[str] = None, author: str = "Claude",
                           output_path: Optional[str] = None) -> dict:
        """Reply to a markup and/or set its review status (like the Revu Markups list).

        text adds a reply (shown in the markup's thread); status is one of Accepted, Rejected,
        Cancelled, Completed, None (a review-status entry from `author`). Give text, status or both.
        Both are stored on the parent's page as annotations answering the parent (/IRT). Writes in
        place (backup in .bb_backups next to the file) unless output_path names a new PDF; refused
        while Revu has the file open. Returns {markup_id, parent_id, page, reply_id, status_id,
        status, write} (reply_id / status_id only for what was created).
        """
        return _write(path, output_path, mw.reply_to_markup, markup_id, text=text, status=status, author=author)

    @mcp.tool(annotations=_WRITE)
    def bb_add_layer(path: str, name: str, visible: bool = True, output_path: Optional[str] = None) -> dict:
        """Create a layer (optional content group) in a PDF.

        If a layer with that name already exists (case-insensitive) nothing is written and the
        result says created=false. Markups are put on a layer with the layer parameter of the
        bb_add_* tools or bb_edit_markups. Writes in place (backup in .bb_backups next to the file)
        unless output_path names a new PDF; refused while Revu has the file open.
        Returns {layer, created, visible, write} (write is null when nothing was written).
        """
        with core.open_readonly(path) as doc:
            existing = mw.find_layer(doc, name)
            if existing is not None and output_path is None:
                return {**mw.add_layer(doc, name, visible), "write": None}
        return _write(path, output_path, mw.add_layer, name, visible)

    @mcp.tool(annotations=_WRITE)
    def bb_set_layer_visibility(path: str, name: str, visible: bool, output_path: Optional[str] = None) -> dict:
        """Show or hide a layer in the PDF's default view.

        name is the layer name (case-insensitive); error if it does not exist. If it already has
        the requested state nothing is written (changed=false). Writes in place (backup in
        .bb_backups next to the file) unless output_path names a new PDF; refused while Revu has
        the file open. Returns {layer, visible, changed, write} (write is null when unchanged).
        """
        with core.open_readonly(path) as doc:
            xref = mw.layer_xref(doc, name, create=False)
            if mw.layer_is_visible(doc, xref) == bool(visible) and output_path is None:
                return {"layer": doc.get_ocgs()[xref]["name"], "visible": bool(visible), "changed": False,
                        "write": None}
        return _write(path, output_path, mw.set_layer_visibility, name, visible)

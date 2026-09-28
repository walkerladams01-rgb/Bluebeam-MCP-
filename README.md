# Bluebeam MCP

**Give Claude (or any MCP client) a working knowledge of Bluebeam Revu PDFs:** read takeoff
quantities, see drawings, search sheets, and add Bluebeam-style markups - without a Bluebeam Max
subscription.

[![tests](https://github.com/walkerladams01-rgb/Bluebeam-MCP-/actions/workflows/tests.yml/badge.svg)](https://github.com/walkerladams01-rgb/Bluebeam-MCP-/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![MCP](https://img.shields.io/badge/MCP-stdio-8A2BE2)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Bluebeam stores a lot inside the PDF: measurement markups carry their scale and quantity, Cloud+
markups are grouped callouts, replies and review statuses hang off their parent, layers are
optional content groups. This server reads and writes that structure directly with
[PyMuPDF](https://pymupdf.readthedocs.io), so it works on any Bluebeam-marked PDF on disk - open in
Revu or not, one file or a whole folder of sets.

## What you can ask

> *"Give me the takeoff totals on the site plan, grouped by subject."*
> *"Export every markup in this set to Excel like Revu's Markups List."*
> *"Show me sheet C-101 and cloud the headwall at station 12+50 with a note to verify grade."*
> *"Which sheets mention 'riprap'? Build me a sheet index."*
> *"Compare the markups in rev 2 and rev 3 - what was added or closed out?"*
> *"Flatten the redlines into a copy and extract sheets 7-9."*

## Example

`bb_takeoff_summary` on a plan with area, length and count takeoffs (trimmed):

```json
{
  "rows": [
    {"group": {"subject": "Riprap Area"},   "kind": "area",   "markups": 2, "total": 10121.53, "unit": "sf"},
    {"group": {"subject": "Riprap Area"},   "kind": "volume", "markups": 1, "total": 96.45,    "unit": "cu yd"},
    {"group": {"subject": "Concrete Slab"}, "kind": "area",   "markups": 1, "total": 3333.33,  "unit": "sf"},
    {"group": {"subject": "Inlet Count"},   "kind": "count",  "markups": 1, "total": 3,        "unit": "count"}
  ],
  "scales": [{"page": 2, "scale": "1 in = 30 ft' in\"", "units_per_point": 0.4166667}],
  "warnings": []
}
```

Quantities come from the value Bluebeam itself displays; the geometry is recomputed as a
cross-check, and any markup where the two disagree (cutouts, arcs, edited captions) is flagged.

## Features

| | |
|---|---|
| **Takeoff** | Areas, lengths, perimeters, counts and volumes from Bluebeam measurement markups, grouped by subject, layer, page, author, color or custom column, with per-page scales and mismatch warnings |
| **Markups List export** | CSV, Excel (with a Takeoff sheet) or JSON with Revu's columns: subject, page label, author, date, status, layer, comments, measurement, depth, color, custom columns |
| **See the drawing** | Render a sheet, a region or a single markup to an image the model can look at; pixel-to-point mapping included, rotated sheets handled |
| **Find things** | Sheet index from page labels, bookmarks and title blocks; text search with page labels and locations; page text by block or word |
| **Mark up** | Text boxes, callouts, notes, Cloud+ (grouped like Revu's), rectangles, ellipses, polygons, polylines, arrows, highlights, area/length/count measurements, built-in, image or PDF stamps |
| **Review** | Edit or delete markups (groups and replies follow), reply, set review status (Accepted / Rejected / Completed ...), manage layers, diff the markups of two revisions |
| **Pages** | Document info, rotate, delete, extract, merge, split, insert, flatten (all or some pages) |
| **Revu** | Open a file in the running Revu; report Revu's version and which scripting features the local license allows |

## Quick start

Requirements: Windows, Python 3.11+. Bluebeam Revu is only needed for `bb_open_in_revu`.

```powershell
git clone https://github.com/walkerladams01-rgb/Bluebeam-MCP-.git bluebeam-mcp
cd bluebeam-mcp
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Claude Code**

```powershell
claude mcp add --scope user bluebeam -- "$PWD\.venv\Scripts\python.exe" "$PWD\server.py"
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "bluebeam": {
      "command": "C:\\path\\to\\bluebeam-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\bluebeam-mcp\\server.py"]
    }
  }
}
```

Restart the client, then ask for `bb_revu_status` to check the connection.

## Tools

| Area | Tool | What it does |
|---|---|---|
| Revu | `bb_revu_status` | Revu version, running state, active tab, what the license allows |
| | `bb_open_in_revu` | Open a PDF in the running Revu (edit or view) |
| Read | `bb_takeoff_summary` | Quantities grouped and totalled, with scales and warnings |
| | `bb_list_markups` / `bb_get_markup` | Markups with Bluebeam fields; filters by page, type, subject, author, layer |
| | `bb_export_markups` | CSV / XLSX / JSON Markups List |
| | `bb_compare_markups` | Added / removed / changed markups between two files |
| | `bb_list_layers` | Layers with visibility and markup counts |
| Sheets | `bb_sheet_index` | Per page: label, bookmark, size, scale, title-block text |
| | `bb_search_text` / `bb_get_page_text` | Find text with locations; page text as text, blocks or words |
| | `bb_render_page` / `bb_render_markup` | PNG of a sheet, a region or one markup |
| Write | `bb_add_text` | Text box, callout or sticky note |
| | `bb_add_shape` | Cloud+, rectangle, ellipse, polygon, polyline, line, arrow |
| | `bb_add_highlight` | Highlight every hit of a phrase, or a rectangle |
| | `bb_add_measurement` | Area, perimeter, length or count at the sheet's scale *(experimental)* |
| | `bb_add_stamp` | Built-in stamp, image or a Revu PDF stamp |
| | `bb_edit_markups` / `bb_delete_markups` | Change or remove markups by id |
| | `bb_reply_to_markup` | Reply and/or review status |
| | `bb_add_layer` / `bb_set_layer_visibility` | Layers |
| Pages | `bb_document_info` | Metadata, page sizes, labels, markup counts |
| | `bb_rotate_pages` / `bb_delete_pages` / `bb_insert_pages` | Page edits |
| | `bb_extract_pages` / `bb_merge_pdfs` / `bb_split_pdf` | New files from pages |
| | `bb_flatten` | Burn markups into the page (to a new file by default) |

Conventions: pages are 1-based and ranges look like `"1,3-5"`; coordinates are PDF points
(1/72 in) from the top-left corner; markup ids are Bluebeam's own `/NM` GUIDs.

## Safety

- **Nothing is overwritten silently.** Every write takes `output_path` (a new file; the source is
  untouched). Without it the file is edited in place *after* the original is copied to
  `.bb_backups\<name>.<timestamp>.pdf` beside it. A call that changes nothing writes nothing.
- **Revu does not lock the PDFs it has open** and would overwrite outside edits when it saves. The
  server refuses to edit the file in Revu's active tab (or one another program holds open) and adds a
  warning to in-place writes whenever Revu is running.
- `bb_flatten` writes `<name> (flattened).pdf` unless told to flatten in place.
- Read tools never write. Logs go to stderr; stdout is reserved for the MCP protocol.

## Bluebeam's official MCP server

Revu 21.9+ ships its own MCP server, which drives Revu directly. It requires a **Bluebeam Max**
subscription and an admin setting (*Preferences > Admin > MCP*). This project needs neither and is
complementary: it works on files rather than the live Revu session, handles many files at once, and
focuses on quantities. `bb_revu_status` tells you whether the official server is available.

## Limitations

- Bluebeam's annotation format is undocumented; the parser follows what Revu writes into real
  takeoff sets. **Volumes** (depth units), **custom columns** and how Revu displays markups *written*
  here (especially measurements) should be spot-checked in Revu. Volume rows are marked
  `volume_unverified` until then.
- Revu itself can only be asked to open a file (`Revu.Launcher` COM); it cannot be told to close,
  save or refresh one. Reopen a file in Revu to see changes made on disk.
- Windows only (COM and file locking); the PDF tools themselves are pure Python.

## Development

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q                  # no Revu needed
.venv\Scripts\python.exe -m pytest -m integration      # talks to the local Revu
```

Tests build synthetic PDFs with `tests/bb_synth.py`, which reproduces the structures Revu writes
(indirect `/Measure` dictionaries, Cloud+ groups, count groups, replies, review states, layers,
UTF-16 page labels, viewport scales), so no drawings are needed or committed.

```
server.py                  FastMCP entry point (stdio)
bluebeam/core.py           errors, paths, page ranges, markup ids, safe writes
bluebeam/markups_read.py   Bluebeam markup parser            -> tools_read.py
bluebeam/takeoff.py        quantity math and grouping
bluebeam/render.py         page and markup rendering
bluebeam/markups_write.py  markup writers                    -> tools_write.py
bluebeam/pages.py          page operations, document info    -> tools_pages.py
bluebeam/revu_native.py    Revu.Launcher COM and status      -> tools_native.py
```

## Credits

Started from [mblakebaugh-hub/bluebeam-mcp](https://github.com/mblakebaugh-hub/bluebeam-mcp) and
rebuilt with [Claude Code](https://claude.com/claude-code). Not affiliated with or endorsed by
Bluebeam, Inc.; Bluebeam and Revu are trademarks of Bluebeam, Inc.

## License

[MIT](LICENSE)

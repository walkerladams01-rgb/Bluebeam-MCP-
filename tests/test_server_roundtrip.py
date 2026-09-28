"""End to end over stdio: the real server, writers (C) read back by readers (B) and page ops (A)."""
import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests import bb_synth

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data(result):
    assert not result.isError, result.content[0].text if result.content else result
    texts = [c.text for c in result.content if c.type == "text"]
    return json.loads(texts[0]) if texts else None


async def _session_run(fn):
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "server.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def test_server_roundtrip(tmp_path):
    pdf = str(tmp_path / "takeoff.pdf")
    expected = bb_synth.build_takeoff_pdf(pdf)

    async def body(s):
        call = s.call_tool
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"bb_takeoff_summary", "bb_add_measurement", "bb_render_page", "bb_flatten",
                "bb_revu_status", "bb_add_shape"} <= names
        assert not any(n in names for n in ("bb_close_document", "bb_list_open_documents"))

        summary = _data(await call("bb_takeoff_summary", {"path": pdf}))
        assert summary, "empty takeoff summary"

        # C writes an area measurement, B reads the same value back.
        square = [[100, 100], [200, 100], [200, 200], [100, 200]]
        added = _data(await call("bb_add_measurement", {
            "path": pdf, "page": 2, "kind": "area", "points": square,
            "scale": "1 in = 30 ft", "subject": "Roundtrip Area"}))
        assert added["write"]["backup"] and added["write"]["in_place"]
        got = _data(await call("bb_get_markup", {"path": pdf, "markup_id": added["markup_id"]}))
        m = got.get("measurement") or got["markup"]["measurement"]
        assert m["kind"] == "area" and abs(m["value"] - 1736.11) < 0.05, m

        # Cloud+ with text: callout is the group parent, B sees the cloud as its child.
        cloud = _data(await call("bb_add_shape", {
            "path": pdf, "page": 3, "kind": "cloud", "rect": [300, 300, 420, 380],
            "text": "Verify grade", "text_rect": [430, 300, 560, 340]}))
        reply = _data(await call("bb_reply_to_markup", {
            "path": pdf, "markup_id": cloud["markup_id"], "text": "Checked", "status": "Accepted"}))
        assert reply["write"]["saved"]
        listed = _data(await call("bb_list_markups", {"path": pdf, "pages": "3", "detail": "full"}))
        rows = listed["markups"] if isinstance(listed, dict) else listed
        parent = next(r for r in rows if r["id"] == cloud["markup_id"])
        assert (parent.get("status") or {}).get("state") == "Accepted", parent
        child = next(r for r in rows if r["id"] == cloud["cloud_id"])
        assert child.get("group_parent_id") == cloud["markup_id"], child

        # Summary now includes the new area; totals grew by ~1,736 sf.
        after = _data(await call("bb_takeoff_summary", {"path": pdf}))
        assert "Roundtrip Area" in json.dumps(after)

        # See the page, then flatten to a new file (source keeps its markups).
        img = await call("bb_render_page", {"path": pdf, "page": 2, "max_px": 800})
        assert not img.isError and any(c.type == "image" for c in img.content)
        flat = _data(await call("bb_flatten", {"path": pdf, "output_path": str(tmp_path / "flat.pdf")}))
        assert flat["markups_remaining"] == 0 and flat["markups_flattened"] > 0
        still = _data(await call("bb_list_markups", {"path": pdf}))
        assert still, "source lost its markups"

        # Errors come back as tool errors, not crashes.
        bad = await call("bb_takeoff_summary", {"path": str(tmp_path / "missing.pdf")})
        assert bad.isError and "not found" in bad.content[0].text.lower()
        return expected

    anyio.run(_session_run, body)

"""test_zed_a2a_mcp.py — unit tests for zed_a2a_mcp.py (no live bridge required)."""

import asyncio
import json
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Import the module under test.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import zed_a2a_mcp as mcp  # noqa: E402


# ---------------------------------------------------------------------------
# Fake websocket capture for testing sent frames
# ---------------------------------------------------------------------------


class _FakeWs:
    """Captures the single send call and returns a pre-programmed reply."""

    def __init__(self, reply: dict):
        self.last_send: str | None = None
        self._reply = reply

    async def send(self, data: str) -> None:
        self.last_send = data

    async def recv(self) -> str:
        return json.dumps(self._reply, ensure_ascii=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolsList(unittest.TestCase):
    EXPECTED_NAMES = sorted(
        [
            "bridge_health",
            "a2a_agents",
            "a2a_inbox",
            "a2a_send",
            "coord_prepare",
            "coord_stats",
            "mesh_create_project",
            "mesh_run_project",
            "mesh_status",
            "mesh_send_agent",
        ]
    )

    def test_tools_list_returns_exact_names(self):
        names = sorted(t["name"] for t in mcp.TOOLS)
        self.assertEqual(names, self.EXPECTED_NAMES)

    def test_tools_count_is_ten(self):
        self.assertEqual(len(mcp.TOOLS), 10)

    def test_each_tool_has_required_keys(self):
        for tool in mcp.TOOLS:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)


class TestInitialize(unittest.TestCase):
    def test_initialize_response(self):
        req = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        result = asyncio.get_event_loop().run_until_complete(mcp.handle_request(req))
        self.assertEqual(result["jsonrpc"], "2.0")
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["result"]["protocolVersion"], mcp.MCP_PROTOCOL_VERSION)
        self.assertEqual(result["result"]["capabilities"], {"tools": {}})
        self.assertEqual(result["result"]["serverInfo"]["name"], mcp.SERVER_NAME)
        self.assertIsInstance(result["result"]["serverInfo"]["version"], str)


class TestUnknownMethod(unittest.TestCase):
    def test_unknown_method_returns_32601(self):
        req = {"jsonrpc": "2.0", "method": "foo/bar", "id": 42}
        result = asyncio.get_event_loop().run_until_complete(mcp.handle_request(req))
        self.assertEqual(result["error"]["code"], -32601)


class TestNotificationReturnsNone(unittest.TestCase):
    def test_notification_no_response(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        result = asyncio.get_event_loop().run_until_complete(mcp.handle_request(req))
        self.assertIsNone(result)


class TestBridgeHealthNoBridge(unittest.TestCase):
    def test_bridge_health_unreachable(self):
        # Point the probe at a closed port so the negative path is exercised
        # regardless of whether a live bridge is running on the default port.
        unreachable = "http://127.0.0.1:1/health"
        previous = os.environ.get("SNAPGENT_BRIDGE_HEALTH_URL")
        os.environ["SNAPGENT_BRIDGE_HEALTH_URL"] = unreachable
        try:
            result = asyncio.get_event_loop().run_until_complete(
                mcp._tool_bridge_health()
            )
        finally:
            if previous is None:
                os.environ.pop("SNAPGENT_BRIDGE_HEALTH_URL", None)
            else:
                os.environ["SNAPGENT_BRIDGE_HEALTH_URL"] = previous
        self.assertFalse(result.get("ok", True))
        self.assertIn("error", result)


class TestA2aSendFrameCapture(unittest.TestCase):
    """Verify the exact JSON frame sent for a2a_send and that the reply is wrapped."""

    def setUp(self):
        # Patch _get_secret so we don't need config.json.
        self.secret_patcher = patch.object(
            mcp, "_get_secret", return_value="test-secret"
        )
        self.secret_patcher.start()
        self.fake_ws = _FakeWs({"type": "a2a_ack", "id": 1, "ok": True})
        self.ws_connect_patcher = patch(
            "websockets.asyncio.client.connect", return_value=self.fake_ws
        )
        self.mock_connect = self.ws_connect_patcher.start()
        self.addCleanup(self.secret_patcher.stop)
        self.addCleanup(self.ws_connect_patcher.stop)

    def test_a2a_send_sends_correct_frame(self):
        result = asyncio.get_event_loop().run_until_complete(
            mcp._tool_a2a_send(recipient="alice", note="hello")
        )
        self.assertTrue(result.get("ok", False))
        sent = self.fake_ws.last_send
        self.assertIsNotNone(sent)
        frame = json.loads(sent)
        self.assertEqual(frame["type"], "a2a_send")
        self.assertEqual(frame["sender"], "zed")
        self.assertEqual(frame["recipient"], "alice")
        self.assertEqual(frame["messageType"], "note")
        self.assertEqual(frame["payload"], {"text": "hello"})
        self.assertIsInstance(frame["id"], int)


class TestBridgeErrorMessageFrame(unittest.TestCase):
    """A bridge error frame must become {'ok':False,'error':'...'}."""

    def setUp(self):
        self.secret_patcher = patch.object(mcp, "_get_secret", return_value="s")
        self.secret_patcher.start()
        self.addCleanup(self.secret_patcher.stop)

    def test_error_frame_becomes_ok_false(self):
        captured_frames = []

        class _EchoFakeWs:
            async def send(self, data):
                captured_frames.append(data)

            async def recv(self):
                # Return error frame with matching id
                req = json.loads(captured_frames[-1])
                return json.dumps(
                    {
                        "type": "mesh_error",
                        "id": req["id"],
                        "error": "project not found",
                    },
                    ensure_ascii=False,
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                pass

        with patch("websockets.asyncio.client.connect", return_value=_EchoFakeWs()):
            result = asyncio.get_event_loop().run_until_complete(
                mcp._tool_mesh_status(project_id="xyz")
            )
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error"], "project not found")


class TestSecretNeverInOutput(unittest.TestCase):
    """The auth secret must never appear in any tool result string."""

    def test_secret_not_in_tool_results(self):
        with patch.object(mcp, "_get_secret", return_value="super-secret-value"):
            result = asyncio.get_event_loop().run_until_complete(
                mcp._tool_bridge_health()
            )
        result_str = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("super-secret-value", result_str)


class TestJsonLineFormat(unittest.TestCase):
    def test_json_line_compact_no_spaces(self):
        val = {"a": [1, 2]}
        line = mcp._json_line(val)
        self.assertEqual(line, '{"a":[1,2]}')

    def test_json_line_non_ascii(self):
        line = mcp._json_line({"note": "héllo"})
        self.assertIn("héllo", line)


class TestToolNotFound(unittest.TestCase):
    def test_call_unknown_tool(self):
        req = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 7,
            "params": {"name": "does_not_exist", "arguments": {}},
        }
        result = asyncio.get_event_loop().run_until_complete(mcp.handle_request(req))
        self.assertEqual(result["error"]["code"], -32601)


class TestStdioMainLoopRoundTrip(unittest.TestCase):
    """Simulate the full stdio loop by feeding request strings into stdout capture."""

    def _run_requests(self, requests: list[str]) -> list[str]:
        old_stdout = sys.stdout
        captured: list[str] = []
        sys.stdout = StringIO()
        try:

            async def feed():
                for r in requests:
                    # Inject directly into handle_request via a mock readline
                    pass

            # Manually drive handle_request for each JSON line
            async def drive():
                for r in requests:
                    obj = json.loads(r)
                    resp = await mcp.handle_request(obj)
                    if resp is not None:
                        sys.stdout.write(mcp._json_line(resp) + "\n")
                        sys.stdout.flush()

            asyncio.get_event_loop().run_until_complete(drive())
        finally:
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout
        return [line for line in output.splitlines() if line.strip()]

    def test_initialize_and_tools_list_round_trip(self):
        lines = self._run_requests(
            [
                json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
                json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 2}),
            ]
        )
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["result"]["protocolVersion"], mcp.MCP_PROTOCOL_VERSION)
        second = json.loads(lines[1])
        self.assertEqual(len(second["result"]["tools"]), 10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()

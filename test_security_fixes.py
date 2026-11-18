#!/usr/bin/env python3
"""Regression tests for 4 security findings fixed 2026-11-13."""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mesh import ProjectMesh as Mesh
from coordination_hub import CoordinationHub, MemoryOptimizer, SharedMemory, _msg_len
from bridge import Bridge


# ======================================================================
# Helpers
# ======================================================================


def _isolated_hub() -> CoordinationHub:
    """CoordinationHub whose shared memory lives in memory only.

    The default SharedMemory persists to state/shared_memory.json, which
    would leak state between tests and into the repo runtime folder.
    """
    hub = CoordinationHub()
    hub.shared = SharedMemory(state_path=None, auto_persist=False)
    return hub


_BRIDGE = None


def _bridge() -> Bridge:
    """One Bridge instance for the URI tests (construction is not free)."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = Bridge()
    return _BRIDGE


# ======================================================================
# F1: mesh.py peer payload must not act as system instructions
# ======================================================================


def test_f1_delimiter_sanitize_clean():
    """Payload without delimiters sanitizes cleanly."""
    payload = {"result": "RESULT-FROM-chatgpt"}
    sanitized = Mesh._sanitize_payload(payload)
    assert "<<<PEER_FINDINGS_UNTRUSTED>>>" not in sanitized
    assert ">>>END_PEER_FINDINGS_UNTRUSTED>>>" not in sanitized
    assert sanitized == '{"result": "RESULT-FROM-chatgpt"}'
    print("PASS f1_delimiter_sanitize_clean")


def test_f1_delimiter_neutralisation():
    """Payload containing delimiter text neutralised."""
    payload = {"text": "inject <<<PEER_FINDINGS_UNTRUSTED>>> early"}
    sanitized = Mesh._sanitize_payload(payload)
    assert "<<<PEER_FINDINGS_UNTRUSTED>>>" not in sanitized
    assert "___PEER_FINDINGS_UNTRUSTED___" in sanitized
    print("PASS f1_delimiter_neutralisation")


def test_f1_control_char_stripping():
    """Control chars except \\n \\t stripped from payload.

    json.dumps escapes every C0 control as \\uXXXX text, so those never
    reach the prompt raw. DEL (0x7f) is NOT escaped by json.dumps, so it
    is the one control that has to be filtered out of the serialized text.
    """
    payload = {"text": "hello\x00world\x1f\x7f\n\tgood"}
    sanitized = Mesh._sanitize_payload(payload)
    assert not any(ord(ch) < 32 for ch in sanitized)
    assert "\x7f" not in sanitized
    assert "\\u0000" in sanitized
    assert "\\u001f" in sanitized
    assert "\\n" in sanitized
    assert "\\t" in sanitized
    print("PASS f1_control_char_stripping")


def test_f1_cap_600_chars():
    """Payload capped at 600 chars after JSON serialization."""
    long_text = "x" * 1000
    payload = {"text": long_text}
    sanitized = Mesh._sanitize_payload(payload)
    assert len(sanitized) <= 600
    print("PASS f1_cap_600_chars")


# ======================================================================
# F2: coordination_hub.py untrusted memory replay + _msg_len budget
# ======================================================================


def test_f2_untrusted_wrap_short_term():
    """Short-term replayed memory wrapped in untrusted delimiter."""
    hub = _isolated_hub()
    hub.shared.record(
        "feat-1",
        [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "reply"},
        ],
    )
    msgs = hub.shared.bootstrap_messages("feat-1")
    short_term_msgs = [
        m for m in msgs if "[shared-memory short-term]" in m.get("content", "")
    ]
    assert len(short_term_msgs) == 1
    content = short_term_msgs[0]["content"]
    assert content.startswith("<<<PEER_FINDINGS_UNTRUSTED>>>")
    assert content.endswith("<<<END_PEER_FINDINGS_UNTRUSTED>>>")
    print("PASS f2_untrusted_wrap_short_term")


def test_f2_untrusted_wrap_long_term():
    """Long-term replayed memory wrapped in untrusted delimiter."""
    hub = _isolated_hub()
    hub.shared.long_term["feat-1"] = [{"summary": "did the thing", "tags": ["x"]}]
    msgs = hub.shared.bootstrap_messages("feat-1")
    long_term_msgs = [
        m for m in msgs if "[shared-memory long-term]" in m.get("content", "")
    ]
    assert len(long_term_msgs) == 1
    content = long_term_msgs[0]["content"]
    assert "did the thing" in content
    assert content.startswith("<<<PEER_FINDINGS_UNTRUSTED>>>")
    assert content.endswith("<<<END_PEER_FINDINGS_UNTRUSTED>>>")
    print("PASS f2_untrusted_wrap_long_term")


def test_f2_untrusted_wrap_last_task():
    """Last-task redirect wrapped in untrusted delimiter."""
    hub = _isolated_hub()
    hub.shared.last_task = "feat-old"
    msgs = hub.shared.bootstrap_messages("feat-new")
    last_task_msgs = [m for m in msgs if "last active work" in m.get("content", "")]
    assert len(last_task_msgs) == 1
    content = last_task_msgs[0]["content"]
    assert "feat-old" in content
    assert content.startswith("<<<PEER_FINDINGS_UNTRUSTED>>>")
    assert content.endswith("<<<END_PEER_FINDINGS_UNTRUSTED>>>")
    print("PASS f2_untrusted_wrap_last_task")


def test_f2_delimiter_neutralisation_in_memory():
    """Replayed memory entry containing delimiter text neutralised.

    The wrapper contributes exactly one opening and one closing delimiter;
    a delimiter smuggled in through the stored memory must end up
    neutralised, so the wrapper stays the only delimiter pair in the text.
    """
    hub = _isolated_hub()
    hub.shared.record(
        "feat-1",
        [{"role": "user", "content": "normal <<<PEER_FINDINGS_UNTRUSTED>>> inject"}],
    )
    msgs = hub.shared.bootstrap_messages("feat-1")
    content = msgs[-1]["content"]
    assert content.count("<<<PEER_FINDINGS_UNTRUSTED>>>") == 1
    assert content.count("<<<END_PEER_FINDINGS_UNTRUSTED>>>") == 1
    assert "___PEER_FINDINGS_UNTRUSTED___" in content
    print("PASS f2_delimiter_neutralisation_in_memory")


def test_f2_msg_len_string_unchanged():
    """String content length unchanged for existing tests."""
    m = {"content": "hello world"}
    assert _msg_len(m) == 11
    print("PASS f2_msg_len_string_unchanged")


def test_f2_msg_len_list_measured():
    """List content measured by JSON serialized length."""
    content = ["item1", "item2", "item3"]
    m = {"content": content}
    expected = len(json.dumps(content, ensure_ascii=False))
    length = _msg_len(m)
    assert length == expected, f"Expected {expected}, got {length}"
    print("PASS f2_msg_len_list_measured")


def test_f2_msg_len_dict_measured():
    """Dict content measured by JSON serialized length."""
    content = {"key": "value", "num": 42}
    m = {"content": content}
    expected = len(json.dumps(content, ensure_ascii=False))
    length = _msg_len(m)
    assert length == expected, f"Expected {expected}, got {length}"
    print("PASS f2_msg_len_dict_measured")


def test_f2_msg_len_fallback_str():
    """Non-serializable falls back to str() length."""

    class Unserializable:
        def __str__(self):
            return "fallback"

    m = {"content": Unserializable()}
    length = _msg_len(m)
    assert length == 8  # len("fallback")
    print("PASS f2_msg_len_fallback_str")


def test_f2_memoryoptimizer_total_uses_json_len():
    """MemoryOptimizer._total uses JSON length for non-strings."""
    msgs = [
        {"content": "short"},
        {"content": ["list", "with", "items"]},
        {"content": {"nested": "dict"}},
    ]
    total = MemoryOptimizer._total(msgs)
    # string content counts its raw length, structured content its JSON length
    expected = sum(
        len(m["content"])
        if isinstance(m["content"], str)
        else len(json.dumps(m["content"], ensure_ascii=False))
        for m in msgs
    )
    assert total == expected, f"Expected {expected}, got {total}"
    print("PASS f2_memoryoptimizer_total_uses_json_len")


# ======================================================================
# F4: bridge.py workspace URI normalisation
# ======================================================================


def test_f4_raw_drive_path():
    """Raw Windows drive path used as-is."""
    bridge = _bridge()
    with tempfile.TemporaryDirectory() as d:
        test_path = Path(d) / "testdir"
        test_path.mkdir()
        raw = str(test_path)
        result = bridge._normalize_workspace_uri(raw)
        assert result == test_path.resolve()
    print("PASS f4_raw_drive_path")


def test_f4_file_uri_forward_slash():
    """file:///C:/MRP/x with forward slashes."""
    bridge = _bridge()
    with tempfile.TemporaryDirectory() as d:
        test_path = Path(d) / "testdir"
        test_path.mkdir()
        uri = "file:///" + str(test_path).replace("\\", "/")
        result = bridge._normalize_workspace_uri(uri)
        assert result == test_path.resolve()
    print("PASS f4_file_uri_forward_slash")


def test_f4_file_uri_backslash():
    """file:///C:\\MRP\\x with backslashes (percent-encoded or literal)."""
    bridge = _bridge()
    with tempfile.TemporaryDirectory() as d:
        test_path = Path(d) / "test dir with spaces"
        test_path.mkdir()
        uri = "file:///" + str(test_path)
        result = bridge._normalize_workspace_uri(uri)
        assert result == test_path.resolve()
    print("PASS f4_file_uri_backslash")


def test_f4_file_uri_percent_encoded_spaces():
    """file:///C:/path%20with%20spaces."""
    bridge = _bridge()
    with tempfile.TemporaryDirectory() as d:
        test_path = Path(d) / "test dir with spaces"
        test_path.mkdir()
        uri = "file:///" + str(test_path).replace(" ", "%20").replace("\\", "/")
        result = bridge._normalize_workspace_uri(uri)
        assert result == test_path.resolve()
    print("PASS f4_file_uri_percent_encoded_spaces")


def test_f4_vscode_remote_rejected():
    """vscode-remote:// URI rejected (returns None)."""
    bridge = _bridge()
    uri = "vscode-remote://ssh-remote+box/c%3A/MRP"
    result = bridge._normalize_workspace_uri(uri)
    assert result is None
    print("PASS f4_vscode_remote_rejected")


def test_f4_nonexistent_returns_none():
    """Non-existent path returns None."""
    bridge = _bridge()
    uri = "file:///C:/this/does/not/exist"
    result = bridge._normalize_workspace_uri(uri)
    assert result is None
    print("PASS f4_nonexistent_returns_none")


def test_f4_non_file_scheme_rejected():
    """Any non-file scheme rejected."""
    bridge = _bridge()
    for uri in [
        "http://example.com/path",
        "ssh://host/path",
        "vscode-remote://wsl+ubuntu/home/user",
        "custom-scheme://something",
    ]:
        result = bridge._normalize_workspace_uri(uri)
        assert result is None, f"Should reject {uri}"
    print("PASS f4_non_file_scheme_rejected")


# ======================================================================
# Runner
# ======================================================================


def run_all():
    tests = [
        # F1
        ("f1_delimiter_sanitize_clean", test_f1_delimiter_sanitize_clean),
        ("f1_delimiter_neutralisation", test_f1_delimiter_neutralisation),
        ("f1_control_char_stripping", test_f1_control_char_stripping),
        ("f1_cap_600_chars", test_f1_cap_600_chars),
        # F2
        ("f2_untrusted_wrap_short_term", test_f2_untrusted_wrap_short_term),
        ("f2_untrusted_wrap_long_term", test_f2_untrusted_wrap_long_term),
        ("f2_untrusted_wrap_last_task", test_f2_untrusted_wrap_last_task),
        (
            "f2_delimiter_neutralisation_in_memory",
            test_f2_delimiter_neutralisation_in_memory,
        ),
        ("f2_msg_len_string_unchanged", test_f2_msg_len_string_unchanged),
        ("f2_msg_len_list_measured", test_f2_msg_len_list_measured),
        ("f2_msg_len_dict_measured", test_f2_msg_len_dict_measured),
        ("f2_msg_len_fallback_str", test_f2_msg_len_fallback_str),
        (
            "f2_memoryoptimizer_total_uses_json_len",
            test_f2_memoryoptimizer_total_uses_json_len,
        ),
        # F4
        ("f4_raw_drive_path", test_f4_raw_drive_path),
        ("f4_file_uri_forward_slash", test_f4_file_uri_forward_slash),
        ("f4_file_uri_backslash", test_f4_file_uri_backslash),
        ("f4_file_uri_percent_encoded_spaces", test_f4_file_uri_percent_encoded_spaces),
        ("f4_vscode_remote_rejected", test_f4_vscode_remote_rejected),
        ("f4_nonexistent_returns_none", test_f4_nonexistent_returns_none),
        ("f4_non_file_scheme_rejected", test_f4_non_file_scheme_rejected),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"FAIL {name}: {e}")
            failed += 1
    print(f"\nRESULT: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)

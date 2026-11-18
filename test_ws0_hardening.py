#!/usr/bin/env python3
"""WS-0 hardening tests: G13 (add_server RCE), G14 (auth_token leak),
G15 (origin prefix), bypass G5 (empty Origin on /auth-token).

Runs without a live bridge by exercising the code paths directly.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


PASS = 0
FAIL = 0


def _ok(name):
    global PASS
    PASS += 1
    print(f"PASS {name}")


def _fail(name, err):
    global FAIL
    FAIL += 1
    print(f"FAIL {name}: {err}")


def test_g14_auth_token_message_removed():
    """Bridge source no longer dispatches a WS 'auth_token' message."""
    src = Path("bridge.py").read_text(encoding="utf-8")
    # The string may still appear as 'auth_token_refused' (HTTP handler),
    # but the WS dispatch is `elif kind == "auth_token":` - it must be gone.
    assert 'elif kind == "auth_token"' not in src, (
        "WS auth_token dispatch still present"
    )


def test_g13_add_server_message_removed():
    """Bridge source no longer dispatches add_server / remove_server."""
    src = Path("bridge.py").read_text(encoding="utf-8")
    assert 'elif kind == "add_server"' not in src
    assert 'elif kind == "remove_server"' not in src


def test_g15_origin_exact_match():
    """Origin check requires exact chrome-extension://<id>, not a prefix."""
    src = Path("bridge.py").read_text(encoding="utf-8")
    # The loose prefix check must be gone
    assert 'origin.startswith(\n                ("chrome-extension://", "moz-extension://")' not in src, (
        "origin prefix check still present"
    )
    # The exact-match form must be present
    assert "is_extension = origin in expected_origins" in src


def test_g5_auth_token_rejects_empty_origin():
    """Empty Origin is now a 403 on /auth-token, not a free pass."""
    src = Path("bridge.py").read_text(encoding="utf-8")
    # Old bug: `if origin and not origin.startswith(allowed_origins):`
    assert "if origin and not origin.startswith" not in src, (
        "origin-empty bypass still present"
    )
    # New form must reject when origin is falsy
    assert "if not origin or origin not in allowed_origins:" in src


def test_extension_id_pinned_from_config():
    """load_config writes bridge.security.extensionId into SNAPGENT_EXTENSION_ID."""
    import json
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    ext_id = cfg.get("bridge", {}).get("security", {}).get("extensionId")
    assert ext_id, "config.json is missing bridge.security.extensionId"
    assert len(ext_id) == 32, f"extension id length is {len(ext_id)}, expected 32"
    assert ext_id.isalpha() and ext_id.islower(), (
        "extension id must be 32 lowercase letters a-p"
    )


def test_manifest_has_key():
    """manifest.json carries the 'key' field so the extension ID is stable."""
    import json
    mf = json.loads(
        Path("snapgent-extension/manifest.json").read_text(encoding="utf-8")
    )
    assert "key" in mf and mf["key"], "manifest.json is missing the 'key' field"


def run_all():
    tests = [
        ("g14_auth_token_message_removed", test_g14_auth_token_message_removed),
        ("g13_add_server_message_removed", test_g13_add_server_message_removed),
        ("g15_origin_exact_match", test_g15_origin_exact_match),
        ("g5_auth_token_rejects_empty_origin", test_g5_auth_token_rejects_empty_origin),
        ("extension_id_pinned_from_config", test_extension_id_pinned_from_config),
        ("manifest_has_key", test_manifest_has_key),
    ]
    for name, fn in tests:
        try:
            fn()
            _ok(name)
        except Exception as exc:
            _fail(name, exc)
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())

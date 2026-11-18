#!/usr/bin/env python3
"""Generate a stable Chrome extension key + ID for origin pinning."""
import base64
import hashlib
import json
import sys
from pathlib import Path


def main():
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        sys.stderr.write("cryptography not installed. pip install cryptography\n")
        return 2

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_b64 = base64.b64encode(pub).decode()
    digest = hashlib.sha256(pub).hexdigest()[:32]
    ext_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)

    root = Path(__file__).resolve().parent.parent
    manifest_path = root / "snapgent-extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["key"] = key_b64
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    config_path = root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    bridge = config.setdefault("bridge", {})
    security = bridge.setdefault("security", {})
    security["extensionId"] = ext_id
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"extension_id={ext_id}")
    print(f"manifest updated: {manifest_path}")
    print(f"config updated:   {config_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

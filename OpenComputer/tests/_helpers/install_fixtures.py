"""Shared helpers for install-related tests.

Lives outside `tests/conftest.py` because conftest is auto-loaded; we
want explicit imports so the helper's surface is discoverable.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile


def make_tarball(
    plugin_id: str,
    plugin_py_body: str = "def register(api):\n    pass\n",
) -> bytes:
    """Return raw gzipped tar bytes containing a minimal single-file plugin."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "entry": "plugin.py",
            }
        ).encode()
        info = tarfile.TarInfo(name="plugin.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        body = plugin_py_body.encode()
        info2 = tarfile.TarInfo(name="plugin.py")
        info2.size = len(body)
        tar.addfile(info2, io.BytesIO(body))

    return buf.getvalue()


def fake_catalog(plugin_id: str, raw_tarball: bytes) -> dict:
    sha = hashlib.sha256(raw_tarball).hexdigest()
    return {
        "schema_version": 1,
        "plugins": [
            {
                "id": plugin_id,
                "version": "0.1.0",
                "tarball_url": f"https://example.test/{plugin_id}.tgz",
                "tarball_sha256": sha,
            }
        ],
    }

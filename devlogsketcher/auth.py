"""Access-key auth for the web app.

A deliberately simple scheme: the operator generates keys (`devlog keys add`) and
hands them out; users present a key once to sign in. Only SHA-256 *hashes* of the keys
are stored (in the config dir), so the file on disk never contains a usable secret —
the raw key is shown once at creation and thereafter lives only in the user's browser
cookie. Auth is enforced by the web server whenever at least one key exists; with no
keys configured the app stays open (its original localhost-only behavior).

Revocation is immediate: every request re-checks the presented key against the file,
so removing a key locks out anyone holding it on their next request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import config_dir

KEY_PREFIX = "dlk_"  # so a leaked key is recognizable as a DevlogSketcher access key


def auth_path() -> Path:
    return config_dir() / "auth.json"


@dataclass
class AccessKey:
    id: str
    label: str
    created_at: str
    hash: str  # sha256 hex of the raw key; the raw key itself is never stored


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def load_keys() -> list[AccessKey]:
    path = auth_path()
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [AccessKey(**k) for k in raw.get("keys", [])]


def save_keys(keys: list[AccessKey]) -> None:
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"keys": [k.__dict__ for k in keys]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)
    try:
        path.chmod(0o600)  # auth data — keep it owner-only even though it's hashed
    except OSError:
        pass


def auth_enabled() -> bool:
    """Whether the web server should require a key (true once any key exists)."""
    return len(load_keys()) > 0


def generate_key(label: str = "") -> tuple[AccessKey, str]:
    """Create and persist a new key. Returns (record, raw_key); the raw key is the
    only time the usable secret is available."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    key = AccessKey(
        id=secrets.token_hex(4),
        label=(label or "key").strip(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        hash=_hash_key(raw),
    )
    keys = load_keys()
    keys.append(key)
    save_keys(keys)
    return key, raw


def verify_key(raw: str) -> AccessKey | None:
    """Return the matching key record for a presented raw key, or None."""
    if not raw:
        return None
    presented = _hash_key(raw)
    for k in load_keys():
        if hmac.compare_digest(k.hash, presented):
            return k
    return None


def revoke_key(needle: str) -> AccessKey | None:
    """Remove a key matched by id, exact label, or hash prefix. Returns it, or None."""
    keys = load_keys()
    for k in keys:
        if needle == k.id or needle == k.label or k.hash.startswith(needle):
            keys.remove(k)
            save_keys(keys)
            return k
    return None

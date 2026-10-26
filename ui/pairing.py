"""Phones paired with JARVIS (the mobile app, through Tailscale): one-time pairing codes and device tokens.

The PC's pages use the per-launch token. A phone can't: it would need a new one every start. So Settings ->
Phone shows a QR code that opens the phone's page with a one-time pairing code in the address's #fragment
(never sent in a request line). The phone trades the code for a long-lived device token (POST /pair) and
keeps it in its own storage; afterwards it sends that token in a header, never in a URL.

- A code is 10 characters (about 50 bits), works once, for 5 minutes, and dies after 5 wrong tries.
  Only one code exists at a time: a new one replaces the old one.
- A device token is 32 random bytes. data/devices.json keeps only its SHA-256 (a random token needs no slow
  hash), with the device's name and dates; a stolen file can't be used to connect.
- Remove (Settings -> Phone) deletes a device: its token stops working at once.
Neither codes nor tokens are ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # no 0/O, 1/I/L, U: easy to type from the PC's screen
CODE_LENGTH = 10
CODE_TTL_S = 300
CODE_TRIES = 5
MAX_DEVICES = 10
LAST_SEEN_EVERY_S = 3600  # how often a device's "last used" is written back to the file


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    """What the user typed, as the code: upper case, without spaces or dashes."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


class DeviceStore:
    """Paired devices (data/devices.json) and the current pairing code (in memory only)."""

    def __init__(self, path: Path, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._code: dict | None = None  # {"hash", "expires", "tries"}
        self._devices: list[dict] = self._load()

    # ------------------------------------------------------------ the file

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("paired devices: couldn't read %s (%s); none are paired", self.path.name, type(exc).__name__)
            return []
        devices = data.get("devices") if isinstance(data, dict) else None
        keep = []
        for d in devices or []:
            if isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("hash"), str):
                keep.append({"id": d["id"], "name": str(d.get("name") or "Phone")[:40], "hash": d["hash"],
                             "created": float(d.get("created") or 0), "last_seen": float(d.get("last_seen") or 0)})
        return keep

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": self._devices}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ pairing codes

    def new_code(self) -> dict:
        """A fresh one-time code (replacing any earlier one): {"code", "expires_in"}."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock:
            self._code = {"hash": _digest(code), "expires": self.clock() + CODE_TTL_S, "tries": 0}
        log.info("paired devices: a pairing code was made (valid %d s)", CODE_TTL_S)
        return {"code": code, "expires_in": CODE_TTL_S}

    def cancel_code(self) -> None:
        with self._lock:
            self._code = None

    def redeem(self, code: str, name: str = "Phone") -> dict | None:
        """Trade a valid code for a new device: {"id", "name", "token"} (the token's only appearance), else None."""
        given = normalize_code(code)
        with self._lock:
            current = self._code
            if current is None or self.clock() > current["expires"]:
                self._code = None
                log.warning("paired devices: refused a pairing (no valid code)")
                return None
            if not (len(given) == CODE_LENGTH and hmac.compare_digest(_digest(given), current["hash"])):
                current["tries"] += 1
                if current["tries"] >= CODE_TRIES:
                    self._code = None
                    log.warning("paired devices: too many wrong pairing codes; the code was cancelled")
                else:
                    log.warning("paired devices: refused a wrong pairing code")
                return None
            self._code = None  # works once
            if len(self._devices) >= MAX_DEVICES:
                log.warning("paired devices: refused a pairing (already %d devices)", MAX_DEVICES)
                return None
            token = secrets.token_urlsafe(32)
            now = self.clock()
            device = {"id": secrets.token_hex(6), "name": (" ".join(str(name or "").split()) or "Phone")[:40],
                      "hash": _digest(token), "created": now, "last_seen": now}
            self._devices.append(device)
            self._save()
        log.info("paired devices: paired %r (%s)", device["name"], device["id"])
        return {"id": device["id"], "name": device["name"], "token": token}

    # ------------------------------------------------------------ devices

    def check(self, token: str) -> dict | None:
        """The device this token belongs to ({"id", "name"}), or None."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        digest = _digest(token)
        with self._lock:
            found = None
            for d in self._devices:  # compare every one, in constant time each
                if hmac.compare_digest(d["hash"], digest):
                    found = d
            if found is None:
                return None
            now = self.clock()
            if now - found["last_seen"] > LAST_SEEN_EVERY_S:
                found["last_seen"] = now
                try:
                    self._save()
                except OSError:
                    pass
            return {"id": found["id"], "name": found["name"]}

    def devices(self) -> list[dict]:
        """The paired devices for Settings: id, name, created, last_seen (never a hash)."""
        with self._lock:
            return [{k: d[k] for k in ("id", "name", "created", "last_seen")} for d in self._devices]

    def remove(self, device_id: str) -> bool:
        with self._lock:
            before = len(self._devices)
            self._devices = [d for d in self._devices if d["id"] != device_id]
            if len(self._devices) == before:
                return False
            self._save()
        log.info("paired devices: removed %s", device_id)
        return True

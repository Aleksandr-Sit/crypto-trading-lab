"""Leased, process-acknowledged pause for deploying the two CryptosMX services."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

PROTOCOL = 1
ROOT = Path("/tmp/lab")


def process_identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


class Control:
    def __init__(self, service: str, root: Path = ROOT):
        if service not in ("paper", "forward"):
            raise ValueError("unknown service")
        self.request_path = root / f"crash-{service}.drain.request.json"
        self.status_path = root / f"crash-{service}.drain.status.json"
        self.boot_id = uuid.uuid4().hex
        self.token = None

    def requested(self, now: float) -> dict:
        request = read(self.request_path)
        if request.get("protocol") == PROTOCOL and (
                request.get("committed") is True or request.get("expires_at", 0) > now):
            return request
        return {}

    def request(self, token: str, now: float, ttl: float = 120) -> None:
        old = self.requested(now)
        if old and old["token"] != token:
            raise ValueError("another deployment owns the pause")
        write(self.request_path, {"protocol": PROTOCOL, "token": token,
                                  "expires_at": now + ttl,
                                  "committed": old.get("committed", False)})

    def commit(self, token: str, now: float) -> None:
        request = self.requested(now)
        if not request or request.get("token") != token:
            raise ValueError("no owned pause to commit")
        request["committed"] = True
        write(self.request_path, request)

    def cancel(self, token: str) -> None:
        request = read(self.request_path)
        if request and request.get("token") != token:
            raise ValueError("pause belongs to another deployment")
        self.request_path.unlink(missing_ok=True)

    def tick(self, open_positions: int, now: float, books: dict | None = None,
             busy: bool = False) -> bool:
        request = self.requested(now)
        self.token = request.get("token")
        write(self.status_path, {"protocol": PROTOCOL, "pid": os.getpid(),
                                "boot_id": self.boot_id, "token": self.token,
                                "process_identity": process_identity(os.getpid()),
                                "checked_at": now, "expires_at": request.get("expires_at", 0),
                                "paused": bool(request), "open_positions": open_positions,
                                "ready": bool(request) and open_positions == 0 and not busy,
                                "busy": busy,
                                "committed": request.get("committed", False),
                                "books": books})
        return bool(request)

    def ready(self, token: str, now: float, committed: bool = False) -> bool:
        request, status = self.requested(now), read(self.status_path)
        return bool(request) and (
            request.get("token") == token == status.get("token")
            and status.get("protocol") == PROTOCOL
            and status.get("ready") is True
            and status.get("paused") is True
            and status.get("open_positions") == 0
            and status.get("process_identity") is not None
            and process_identity(status.get("pid", -1)) == status["process_identity"]
            and 0 <= now - status.get("checked_at", 0) <= 3
            and (not committed or status.get("committed") is True)
            and (status.get("committed") is True if request.get("committed")
                 else min(request["expires_at"], status.get("expires_at", 0)) > now + 30)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("protocol", "request", "status", "commit", "cancel"))
    parser.add_argument("--service", choices=("paper", "forward"), required=True)
    parser.add_argument("--token")
    parser.add_argument("--committed", action="store_true")
    a = parser.parse_args()
    if a.action == "protocol":
        print(PROTOCOL)
        return 0
    if not a.token:
        parser.error("--token is required")
    control = Control(a.service)
    if a.action == "request":
        control.request(a.token, time.time())
    elif a.action == "cancel":
        control.cancel(a.token)
    elif a.action == "commit":
        control.commit(a.token, time.time())
    else:
        if not control.ready(a.token, time.time(), a.committed):
            return 1
        print(json.dumps(read(control.status_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

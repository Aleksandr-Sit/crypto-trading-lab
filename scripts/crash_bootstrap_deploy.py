"""One-time Linux/x86_64 migration of the audited e446abe legacy processes.

Freeze first, then verify Python's main thread is idle and its durable journal is flat.
This is deliberately separate from the normal, process-acknowledged drain deploy.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import signal
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import crash_bounce_paper as paper

LEGACY = "e446abebae12efd9776784926c8e63482a474e47"
SERVICES = ("crash-paper", "crash-forward")


def run(*args: str, capture: bool = True, data: str | None = None) -> str:
    result = subprocess.run(args, check=True, text=True, input=data,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def inspect(cid: str) -> dict:
    return json.loads(run("docker", "inspect", cid))[0]


def python_pid(cid: str, service: str) -> int:
    rows = run("docker", "top", cid, "-eo", "pid,comm,args").splitlines()[1:]
    script = service.replace("crash-", "crash_bounce_") + ".py"
    matches = [int(r.split()[0]) for r in rows if len(r.split()) >= 3
               and r.split()[1].startswith("python") and script in r]
    if len(matches) != 1:
        raise RuntimeError("cannot identify the unique Python process")
    return matches[0]


def idle_syscall(pid: int, service: str) -> bool:
    raw = Path(f"/proc/{pid}/syscall").read_text().split()
    # x86_64: epoll_wait/epoll_pwait/epoll_pwait2; clock_nanosleep in legacy loop only.
    allowed = {"232", "281", "441"} if service == "crash-paper" else {"230"}
    children = Path(f"/proc/{pid}/task/{pid}/children").read_text().strip()
    return bool(raw and raw[0] in allowed and not children)


def verify_journals(journal: Path) -> dict:
    manifest = {}
    for path in sorted(journal.glob("*.jsonl")):
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for line in fh:
                if not line.endswith(b"\n"):
                    raise RuntimeError("partial journal tail; resume old process and retry")
                json.loads(line)
                digest.update(line)
        manifest[path.name] = digest.hexdigest()
    if not manifest:
        raise RuntimeError("journal missing")
    return manifest


def verify_source(repo: Path, cid: str, service: str) -> None:
    script = service.replace("crash-", "crash_bounce_") + ".py"
    expected = subprocess.check_output(["git", "-C", str(repo), "show",
                                        f"{LEGACY}:scripts/{script}"])
    archive = subprocess.check_output(["docker", "cp", f"{cid}:/app/scripts/{script}", "-"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        member = tf.extractfile(next(m for m in tf.getmembers() if m.isfile()))
        if member is None or member.read() != expected:
            raise RuntimeError(f"legacy source differs from audited commit: {service}")


def main() -> int:
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repo", type=Path, default=Path("/opt/crypto-trading-lab"))
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=900)
    a = parser.parse_args()
    if platform.machine() != "x86_64" or os.geteuid() != 0:
        raise RuntimeError("audited bootstrap requires Linux x86_64 root")
    os.chdir(a.repo)
    lock = Path("/tmp/cryptosmx-deploy.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if run("git", "rev-parse", "HEAD") != a.commit or run("git", "status", "--porcelain"):
        raise RuntimeError("expected a clean checkout at the deployment commit")
    dc = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
    baseline = [json.loads(line) for line in run(
        "docker", "ps", "-a", "--format", "json").splitlines()]
    baseline_started = {r["ID"]: inspect(r["ID"])["State"]["StartedAt"] for r in baseline}
    old = {s: inspect(run(*dc, "ps", "-q", s)) for s in SERVICES}
    for service, record in old.items():
        if f"LAB_CODE_VERSION={LEGACY}" not in record["Config"]["Env"]:
            raise RuntimeError("bootstrap only supports the audited legacy version")
        verify_source(a.repo, record["Id"], service)
        run("docker", "image", "tag", record["Image"], f"{service}:pre-cryptosmx-audit")
    volume = next(m["Source"] for m in old["crash-paper"]["Mounts"]
                  if m["Destination"] == "/app/data")
    data_root = Path(volume)
    os.environ["LAB_CODE_VERSION"] = a.commit
    run(*dc, "build", *SERVICES, capture=False)
    a.evidence.mkdir(parents=True, exist_ok=False)
    (a.evidence / "before.json").write_text(json.dumps(baseline, indent=2))
    (a.evidence / "old-images.json").write_text(json.dumps(
        {s: {"id": r["Id"], "image": r["Image"]} for s, r in old.items()}, indent=2))
    frozen = set()
    committed = False
    checkpoint = None
    attempt = 0
    try:
        deadline = time.monotonic() + a.wait_seconds
        while time.monotonic() < deadline:
            attempt += 1
            for service in SERVICES:
                cid = old[service]["Id"]
                if run(*dc, "ps", "-q", service) != cid:
                    raise RuntimeError("legacy container identity changed")
                run("docker", "pause", cid)
                frozen.add(service)
            idle_states = {s: idle_syscall(python_pid(old[s]["Id"], s), s) for s in SERVICES}
            idle = all(idle_states.values())
            print(f"freeze attempt {attempt}: idle={idle_states}", flush=True)
            if idle:
                manifest = verify_journals(data_root / "crash-bounce-paper/journal")
                books = {n: paper.LiveBook(size=size) for n, size, _, _ in paper.ALL_X1}
                restored = paper.restore_state(data_root / "crash-bounce-paper/journal", books)
                print(f"durable pending positions: {restored['open_lost']}", flush=True)
                if restored["open_lost"] == 0:
                    checkpoint = {"books": {n: paper._book_state(b) for n, b in books.items()},
                                  "journal_sha256": manifest, "restored": restored,
                                  "frozen_at": datetime.now(UTC).isoformat(),
                                  "legacy_commit": LEGACY, "new_commit": a.commit}
                    break
            for service in tuple(frozen):
                run("docker", "unpause", old[service]["Id"])
                frozen.remove(service)
            time.sleep(2)
        if checkpoint is None:
            raise RuntimeError("no frozen idle/flat state before deadline; old bots retained")
        (a.evidence / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2))
        with tarfile.open(a.evidence / "data-before.tar.gz", "w:gz") as tf:
            tf.add(data_root / "crash-bounce-paper", arcname="crash-bounce-paper")
            tf.add(data_root / "crash-bounce-forward", arcname="crash-bounce-forward")
        paper.Journal(data_root / "crash-bounce-paper").write(
            {"k": "drain", "paused": True, "reason": "frozen_idle_legacy_bootstrap"})
        # Validate every process belongs to the frozen namespace before sending any signal.
        all_pids = []
        for service in SERVICES:
            record = old[service]
            namespace = Path(f"/proc/{record['State']['Pid']}/ns/pid").readlink()
            pids = [int(line.split()[0]) for line in run(
                "docker", "top", record["Id"], "-eo", "pid").splitlines()[1:]]
            if not pids or any(Path(f"/proc/{pid}/ns/pid").readlink() != namespace for pid in pids):
                raise RuntimeError("process namespace mismatch")
            all_pids.extend(pids)
        for service in SERVICES:
            run("docker", "update", "--restart=no", old[service]["Id"])
        committed = True
        # Every process is frozen; pending SIGKILL prevents any Python from resuming on thaw.
        for pid in all_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for service in tuple(frozen):
            cid = old[service]["Id"]
            state = inspect(cid)["State"]
            if state["Running"] and state["Paused"]:
                run("docker", "unpause", cid)
            frozen.remove(service)
        for service in SERVICES:
            run("docker", "wait", old[service]["Id"])
        run(*dc, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
            "--force-recreate", *SERVICES, capture=False)
        for _ in range(90):
            new = {s: inspect(run(*dc, "ps", "-q", s)) for s in SERVICES}
            good = all(r["State"]["Running"] and r["Id"] != old[s]["Id"]
                       and f"LAB_CODE_VERSION={a.commit}" in r["Config"]["Env"]
                       for s, r in new.items())
            if good and new["crash-paper"]["State"].get("Health", {}).get("Status") == "healthy":
                break
            time.sleep(2)
        else:
            raise RuntimeError("new processes not healthy; no automatic rollback")
        run(*dc, "exec", "-T", "crash-paper", "/app/.venv/bin/python",
            "scripts/crash_bounce_paper.py", "startup", "--out", "/app/data/crash-bounce-paper",
            data=json.dumps(checkpoint), capture=False)
        after = [json.loads(line) for line in run(
            "docker", "ps", "-a", "--format", "json").splitlines()]
        exempt = {old[s]["Id"][:12] for s in SERVICES}
        remaining_ids = {r["ID"] for r in after}
        if any(r["ID"] not in remaining_ids for r in baseline if r["ID"] not in exempt):
            raise RuntimeError("an unrelated container identity changed")
        if any(inspect(cid)["State"]["StartedAt"] != started
               for cid, started in baseline_started.items() if cid not in exempt):
            raise RuntimeError("an unrelated container restarted")
        (a.evidence / "other-started-at.json").write_text(json.dumps(
            {cid: stamp for cid, stamp in baseline_started.items() if cid not in exempt}, indent=2))
        (a.evidence / "after.json").write_text(json.dumps(after, indent=2))
        print(f"DEPLOYED {a.commit}: restored accounts match, open_lost=0; other IDs retained")
    finally:
        if not committed:
            for service in tuple(frozen):
                run("docker", "unpause", old[service]["Id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

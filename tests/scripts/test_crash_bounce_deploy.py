"""Deployment protocol and shell integration with fake Docker, never a live restart."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


control = load("crash_bounce_control")
paper = load("crash_bounce_paper")
audit = load("crash_bounce_audit")
T = 1_791_590_400_000


def test_ack_requires_pause_zero_positions_fresh_status_and_live_process(tmp_path):
    c = control.Control("paper", tmp_path)
    c.request("owner", 100)
    assert not c.ready("owner", 100)
    assert c.tick(2, 100)
    assert not c.ready("owner", 100)
    assert c.tick(0, 101)
    assert c.ready("owner", 101)
    assert not c.ready("other", 101)
    assert not c.ready("owner", 105)
    status = control.read(c.status_path)
    status["process_identity"] = "old-process"
    control.write(c.status_path, status)
    assert not c.ready("owner", 101)


def test_pause_lease_expires_and_cannot_be_cancelled_by_other_owner(tmp_path):
    c = control.Control("paper", tmp_path)
    c.request("owner", 100)
    with pytest.raises(ValueError, match="owns"):
        c.request("other", 101)
    with pytest.raises(ValueError, match="another"):
        c.cancel("other")
    assert not c.tick(0, 221)
    assert not c.ready("owner", 221)
    c.request("new", 221)
    c.cancel("new")
    assert not c.tick(0, 222)


def test_archive_busy_never_acknowledges_ready(tmp_path):
    c = control.Control("forward", tmp_path)
    c.request("owner", 100)
    c.tick(0, 100, busy=True)
    assert not c.ready("owner", 100)
    c.tick(0, 101)
    assert c.ready("owner", 101)


def test_committed_pause_needs_ack_and_cannot_expire_during_stop(tmp_path):
    c = control.Control("paper", tmp_path)
    c.request("owner", 100)
    c.tick(0, 101)
    c.commit("owner", 102)
    assert not c.ready("owner", 102, committed=True)
    c.tick(0, 103)
    assert c.ready("owner", 103, committed=True)
    assert c.tick(0, 1000)
    assert c.ready("owner", 1000, committed=True)
    c.cancel("owner")
    assert not c.tick(0, 1001)


def test_archive_loop_finishes_current_work_before_acknowledgement(tmp_path, monkeypatch):
    forward = load("crash_bounce_forward")
    c = control.Control("forward", tmp_path)
    monkeypatch.setattr(control, "Control", lambda service: c)
    monkeypatch.setattr(forward, "RUN_AFTER_UTC", (0, 0))
    completed = []

    def work(out, today):
        c.request("owner", time.time())
        assert not c.ready("owner", time.time())
        completed.append(True)
        return True

    class Finished(Exception):
        pass

    def sleep(seconds):
        assert completed and c.ready("owner", time.time())
        raise Finished

    monkeypatch.setattr(forward, "once", work)
    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(Finished):
        forward.loop(tmp_path)


def test_archive_loop_does_not_start_work_while_paused(tmp_path, monkeypatch):
    forward = load("crash_bounce_forward")
    c = control.Control("forward", tmp_path)
    c.request("owner", time.time())
    monkeypatch.setattr(control, "Control", lambda service: c)
    monkeypatch.setattr(forward, "once", lambda *args: pytest.fail("started while paused"))

    class Finished(Exception):
        pass

    def sleep(seconds):
        assert c.ready("owner", time.time())
        raise Finished

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(Finished):
        forward.loop(tmp_path)


def test_drain_closes_positions_but_cannot_open_new_ones(tmp_path, monkeypatch):
    events = []
    engine = paper.Engine(events.append, ["A"])
    engine.st["A"] = paper.SymState(last_a=1, last_px=100, minute=T - 60_000)
    pos = paper.Pos("x1", "A", T - 60_000, 100, T - 1000, T - 1000, 100)
    engine.main.pos["A"] = pos
    engine.main.book.take(pos, False)
    engine.mirror["A"].append(paper.Pos("m", "A", T - 60_000, 100,
                                       T - 1000, T - 1000, 100))
    c = control.Control("paper", tmp_path)
    runner = paper.Runner.__new__(paper.Runner)
    runner.control, runner.engine = c, engine
    runner.journal = SimpleNamespace(write=events.append)
    monkeypatch.setattr(time, "time", lambda: 100.0)
    c.request("owner", 100)
    runner.sync_control(T)
    assert engine.entries_paused
    assert control.read(c.status_path)["open_positions"] == 2
    engine.on_trade(paper.Trade("A", 2, 102, T + 100, True, T + 100))
    engine.on_trade(paper.Trade("A", 3, 90, T + 200, True, T + 200))
    runner.sync_control(T + 250)
    assert c.ready("owner", 100)
    assert len([e for e in events if e["k"] == "exit"]) == 2
    assert not [e for e in events if e["k"] == "fill"]
    c.cancel("owner")
    runner.sync_control(T + 500)
    assert not engine.entries_paused


@pytest.mark.parametrize("lost,changed,expected_code", [(0, False, 0), (1, False, 1),
                                                       (0, True, 1)])
def test_startup_compares_restored_accounts_not_current_positions(
        tmp_path, monkeypatch, capsys, lost, changed, expected_code):
    journal = tmp_path / "journal"
    journal.mkdir()
    books = {"x1": {"equity": .99, "withdrawn": .01}}
    record = {"k": "start", "open_lost": lost, "restored": {"books": books}}
    (journal / "2026-11-09.jsonl").write_text(json.dumps(record) + "\n")
    expected = {"books": {"x1": {"equity": 1 if changed else .99, "withdrawn": .01}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(expected)))
    monkeypatch.setattr(sys, "argv", ["paper", "startup", "--out", str(tmp_path)])
    assert paper.main() == expected_code
    assert json.loads(capsys.readouterr().out)["restored_matches"] == (expected_code == 0)


def test_deployment_pause_is_separate_from_network_gap():
    rows = [{"k": "drain", "paused": True, "recv": T + 100},
            {"k": "start", "recv": T + 500}]
    q = audit.quality(rows, T, T + 1000)
    assert q["deployment_pause_ms"] == 400
    assert q["affected_union_ms"] == 400
    assert q["gap_union_ms"] == 0


SHIM = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
root=pathlib.Path(os.environ['FAKE_ROOT'])
scenario=os.environ.get('FAKE_SCENARIO','success')
with (root/'calls.jsonl').open('a') as f:
    f.write(json.dumps([name,*args])+'\n')
if name=='flock': sys.exit(1 if scenario=='locked' else 0)
if name=='df': print('Avail\n10000000'); sys.exit(0)
if name=='git':
    if args[0]=='diff' and scenario=='config': sys.exit(1)
    if args[0]=='rev-parse': print('abc123')
    sys.exit(0)
if name=='sleep':
    if scenario=='interrupt': os.kill(os.getppid(),signal.SIGTERM)
    sys.exit(0)
if name!='docker': sys.exit(2)
if args[0]=='compose':
    cmd=args[3:]
    if cmd[0]=='ps': print(('new-' if (root/'started').exists() else 'old-')+cmd[-1])
    elif cmd[0]=='stop':
        if scenario=='stop-fails': sys.exit(1)
        (root/'stopped').touch()
    elif cmd[0]=='up': (root/'started').touch()
    elif cmd[0]=='exec':
        expected=json.load(sys.stdin)
        assert expected['books']['x1']['equity']==1
        print('restored_matches=true,open_lost=0')
    sys.exit(0)
if args[0]=='inspect':
    print('healthy' if args[-1].endswith('paper') else 'running'); sys.exit(0)
if args[0]=='exec':
    if 'printenv' in args: print('abc123')
    elif 'protocol' in args:
        if scenario=='legacy': sys.exit(2)
        print('1')
    elif 'status' in args:
        if scenario=='interrupt': sys.exit(1)
        print(json.dumps({'books':{'x1':{'equity':1}}}))
    elif 'request' in args and scenario=='request-fails': sys.exit(1)
    elif 'commit' in args and scenario=='commit-fails': sys.exit(1)
    sys.exit(0)
sys.exit(2)
'''


@pytest.mark.skipif(os.name == "nt", reason="production deployment is Linux Bash")
@pytest.mark.parametrize("scenario,code", [("success", 0), ("dry", 0), ("legacy", 5),
                                         ("request-fails", 1), ("stop-fails", 1),
                                         ("interrupt", 143), ("locked", 1), ("config", 1),
                                         ("commit-fails", 1)])
def test_targeted_deployment_with_fake_docker(tmp_path, scenario, code):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "git", "df", "flock", "sleep"):
        path = bin_dir / name
        path.write_text(SHIM)
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "FAKE_ROOT": str(tmp_path), "FAKE_SCENARIO": scenario,
           "CRASH_DEPLOY_REPO": str(tmp_path), "CRASH_DEPLOY_LOCK": str(tmp_path / "lock")}
    args = ["bash", str(SCRIPTS / "crash-paper-deploy.sh")]
    if scenario == "dry":
        args.append("--dry-run")
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == code, result.stdout + result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    mutations = [c for c in calls if c[0] == "docker" and "compose" in c
                 and any(v in c for v in ("build", "stop", "up"))]
    for call in mutations:
        assert call[-2:] == ["crash-paper", "crash-forward"]
        assert not set(call) & {"worker", "bot", "web", "db", "migrate", "forward-report"}
    if scenario in ("legacy", "dry", "locked", "config"):
        assert not mutations
        assert not any("request" in c for c in calls)
    if scenario in ("request-fails", "commit-fails", "stop-fails", "interrupt"):
        assert sum("cancel" in c for c in calls) == 2
        assert not (tmp_path / "started").exists()
    if scenario == "success":
        assert (tmp_path / "started").exists()
        up = next(c for c in mutations if "up" in c)
        assert {"--no-deps", "--no-build", "--force-recreate"} <= set(up)

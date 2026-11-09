"""Safety gates for the explicitly scoped legacy bootstrap; no real Docker calls."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("crash_bootstrap_deploy",
                                             SCRIPTS / "crash_bootstrap_deploy.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.mark.parametrize("line,valid", [(b'{"k":"start"}\n', True),
                                      (b'{"k":"start"}', False), (b'{bad}\n', False)])
def test_bootstrap_refuses_partial_or_corrupt_journal(tmp_path, line, valid):
    (tmp_path / "2026-11-09.jsonl").write_bytes(line)
    if valid:
        result = bootstrap.verify_journals(tmp_path)
        assert len(result["2026-11-09.jsonl"]) == 64
    else:
        with pytest.raises((RuntimeError, json.JSONDecodeError)):
            bootstrap.verify_journals(tmp_path)


@pytest.mark.parametrize("call,service,children,expected", [
    ("232 0x0", "crash-paper", "", True),
    ("441 0x0", "crash-paper", "", True),
    ("1 0x0", "crash-paper", "", False),
    ("running", "crash-paper", "", False),
    ("230 0x0", "crash-forward", "", True),
    ("61 0x0", "crash-forward", "", False),
    ("230 0x0", "crash-forward", "123", False),
])
def test_only_audited_idle_syscalls_without_children_are_accepted(
        monkeypatch, call, service, children, expected):
    def read(path, *args, **kwargs):
        return children if path.name == "children" else call
    monkeypatch.setattr(Path, "read_text", read)
    assert bootstrap.idle_syscall(123, service) is expected


def test_unique_python_process_required(monkeypatch):
    monkeypatch.setattr(bootstrap, "run", lambda *args: (
        "PID COMM ARGS\n1 uv uv run python scripts/crash_bounce_paper.py run\n"
        "2 python3 python scripts/crash_bounce_paper.py run"))
    assert bootstrap.python_pid("container", "crash-paper") == 2
    monkeypatch.setattr(bootstrap, "run", lambda *args: "PID COMM ARGS\n")
    with pytest.raises(RuntimeError, match="unique"):
        bootstrap.python_pid("container", "crash-paper")


@pytest.mark.parametrize("still_paused", [False, True])
def test_auto_thaw_race_only_ignored_if_container_is_no_longer_frozen(monkeypatch, still_paused):
    states = iter([{"Running": True, "Paused": True},
                   {"Running": still_paused, "Paused": still_paused}])
    monkeypatch.setattr(bootstrap, "inspect", lambda cid: {"State": next(states)})

    def fail(*args):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(bootstrap, "run", fail)
    if still_paused:
        with pytest.raises(subprocess.CalledProcessError):
            bootstrap.thaw_after_kill("container")
    else:
        bootstrap.thaw_after_kill("container")

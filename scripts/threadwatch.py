#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

APP_NAME = "CodexAutoSupervisor"
STATE_VERSION = 1
DEFAULT_EVENT_TYPES = ("completed", "failed", "question", "approval")
EVENT_TYPE_ALIASES = {
    "complete": "completed",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "question": "question",
    "approval": "approval",
}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ThreadWatchError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def data_dir() -> Path:
    override = os.environ.get("THREADWATCH_HOME")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) / APP_NAME if base else Path.home() / "AppData" / "Local" / APP_NAME
        legacy = Path(base) / "CodexThreadWatch" if base else Path.home() / "AppData" / "Local" / "CodexThreadWatch"
        if not root.exists() and legacy.exists():
            try:
                shutil.copytree(legacy, root)
            except OSError:
                pass
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "codex-auto-supervisor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = 10.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b"\0")
            os.fsync(self.fd)
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._lock_once()
                return True
            except (OSError, BlockingIOError):
                if not blocking or time.monotonic() >= deadline:
                    self.close()
                    return False
                time.sleep(0.05)

    def _lock_once(self) -> None:
        assert self.fd is not None
        os.lseek(self.fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            self.close()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "FileLock":
        if not self.acquire(blocking=True):
            raise ThreadWatchError(f"Could not lock {self.path}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


def state_path(root: Path) -> Path:
    return root / "state.json"


def config_path(root: Path) -> Path:
    return root / "config.json"


def daemon_log_path(root: Path) -> Path:
    return root / "daemon.log"


def pid_path(root: Path) -> Path:
    return root / "daemon.pid"


def stop_path(root: Path) -> Path:
    return root / "daemon.stop"


def state_lock(root: Path) -> FileLock:
    return FileLock(root / "state.lock")


def daemon_lock(root: Path) -> FileLock:
    return FileLock(root / "daemon.lock")


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "watches": {},
        "seen": [],
        "stats": {"events_detected": 0, "wakeups_queued": 0, "wakeups_failed": 0},
        "last_error": None,
    }


def load_state(root: Path) -> dict[str, Any]:
    value = read_json(state_path(root), default_state())
    if not isinstance(value, dict):
        return default_state()
    base = default_state()
    base.update(value)
    if not isinstance(base.get("watches"), dict):
        base["watches"] = {}
    if not isinstance(base.get("seen"), list):
        base["seen"] = []
    if not isinstance(base.get("stats"), dict):
        base["stats"] = default_state()["stats"]
    return base


def save_state(root: Path, state: dict[str, Any]) -> None:
    seen = state.get("seen", [])
    if len(seen) > 2000:
        state["seen"] = seen[-2000:]
    atomic_write_json(state_path(root), state)


def default_config() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "poll_interval_seconds": 1.0,
        "queue_retries": 3,
        "max_summary_chars": 1200,
    }


def load_config(root: Path) -> dict[str, Any]:
    value = read_json(config_path(root), default_config())
    if not isinstance(value, dict):
        return default_config()
    result = default_config()
    result.update(value)
    return result


def ensure_config(root: Path) -> dict[str, Any]:
    config = load_config(root)
    atomic_write_json(config_path(root), config)
    return config


def log_event(root: Path, level: str, event: str, message: str, **data: Any) -> None:
    record: dict[str, Any] = {
        "ts": now_iso(),
        "level": level,
        "event": event,
        "message": message,
    }
    if data:
        record["data"] = data
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    try:
        with daemon_log_path(root).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def validate_uuid(value: str, label: str) -> str:
    if not UUID_RE.match(value):
        raise ThreadWatchError(f"{label} must be a UUID: {value}")
    return value


def normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return os.path.normcase(os.path.realpath(os.path.expandvars(os.path.expanduser(value))))
    except OSError:
        return os.path.normcase(os.path.abspath(value))


def parse_event_types(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_EVENT_TYPES)
    result: list[str] = []
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        mapped = EVENT_TYPE_ALIASES.get(item)
        if not mapped:
            raise ThreadWatchError(f"Unknown event type: {raw}")
        if mapped not in result:
            result.append(mapped)
    if not result:
        raise ThreadWatchError("At least one event type is required.")
    return result


def find_session_file(thread_id: str, home: Path | None = None) -> Path | None:
    root = (home or codex_home()) / "sessions"
    if not root.exists():
        return None
    patterns = [
        str(root / "**" / f"*-{thread_id}.jsonl"),
        str(root / "**" / f"*{thread_id}*.jsonl"),
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        for item in glob.glob(pattern, recursive=True):
            path = Path(item)
            key = str(path)
            if key not in seen:
                seen.add(key)
                found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def session_metadata(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(4):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return None
    return None


def event_key(target_id: str, event: dict[str, Any]) -> str:
    return f"{target_id}|{event['type']}|{event.get('turn_id') or event.get('call_id') or 'unknown'}"


def _error_summary(error: Any) -> str:
    if not error:
        return ""
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error, ensure_ascii=False))
    return str(error)


def _http_status(error: Any) -> int | None:
    if not isinstance(error, dict):
        return None
    info = error.get("codex_error_info") or error.get("codexErrorInfo")
    if not isinstance(info, dict):
        return None
    retry = info.get("response_too_many_failed_attempts") or info.get("responseTooManyFailedAttempts")
    if isinstance(retry, dict):
        value = retry.get("http_status_code") or retry.get("httpStatusCode")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def parse_record(record: Any) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    payload_type = payload.get("type")

    if record_type == "event_msg" and payload_type == "task_complete":
        error = payload.get("error")
        event_type = "failed" if error else "completed"
        summary = _error_summary(error) if error else str(payload.get("last_agent_message") or "")
        return [{
            "type": event_type,
            "turn_id": str(payload.get("turn_id") or payload.get("root_turn_id") or "unknown"),
            "summary": summary,
            "error": error,
            "http_status": _http_status(error),
            "detected_at": now_iso(),
        }]

    if record_type == "event_msg" and payload_type in ("approval_requested", "approval_request"):
        return [{
            "type": "approval",
            "call_id": str(payload.get("call_id") or payload.get("id") or payload.get("turn_id") or "unknown"),
            "summary": str(payload.get("message") or payload.get("reason") or ""),
            "detected_at": now_iso(),
        }]

    if record_type == "response_item" and payload_type in ("function_call", "custom_tool_call"):
        if payload.get("name") in ("request_user_input", "request_user_input_async"):
            return [{
                "type": "question",
                "call_id": str(payload.get("call_id") or payload.get("id") or "unknown"),
                "summary": str(payload.get("arguments") or ""),
                "detected_at": now_iso(),
            }]

    return []


def parse_complete_lines(data: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in data.split(b"\n"):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace")
        if not any(token in line for token in ("task_complete", "request_user_input", "approval_request")):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.extend(parse_record(record))
    return events


def read_appended(path: Path, offset: int, partial_b64: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[list[dict[str, Any]], int, str]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, partial_b64
    if size < offset:
        offset = 0
        partial_b64 = ""
    if size == offset:
        return [], offset, partial_b64
    remaining = size - offset
    read_size = min(remaining, max_bytes)
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(read_size)
    try:
        partial = base64.b64decode(partial_b64.encode("ascii")) if partial_b64 else b""
    except Exception:
        partial = b""
    blob = partial + chunk
    pieces = blob.split(b"\n")
    if blob.endswith(b"\n"):
        complete = b"\n".join(pieces[:-1])
        new_partial = b""
    else:
        complete = b"\n".join(pieces[:-1])
        new_partial = pieces[-1]
    events = parse_complete_lines(complete)
    return events, offset + len(chunk), base64.b64encode(new_partial).decode("ascii")


def find_last_event(path: Path, allowed: Iterable[str]) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
        start = max(0, size - 16 * 1024 * 1024)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    events = [event for event in parse_complete_lines(data) if event["type"] in set(allowed)]
    return events[-1] if events else None


def resolve_codex_command() -> list[str]:
    explicit = os.environ.get("CODEX_CLI_PATH") or os.environ.get("THREADWATCH_CODEX_COMMAND")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for name in ("codex.exe", "codex"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.extend(sorted(Path(local_appdata, "OpenAI", "Codex", "bin").glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if not candidate.exists():
            continue
        if candidate.suffix.lower() == ".ps1":
            shell = shutil.which("pwsh") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(candidate)]
        return [str(candidate)]
    raise ThreadWatchError("Could not find codex. Set CODEX_CLI_PATH or add the Codex CLI to PATH.")


def build_wake_message(rule: dict[str, Any], event: dict[str, Any], max_summary: int = 1200) -> str:
    summary = str(event.get("summary") or "").strip()
    if len(summary) > max_summary:
        summary = summary[:max_summary] + "... [truncated]"
    details = [
        "[threadwatch] external thread event, not a user message.",
        f"event: {event['type']}",
        f"target_thread: {rule['target_id']}",
        f"wake_thread: {rule['wake_thread_id']}",
        f"turn_or_call: {event.get('turn_id') or event.get('call_id') or 'unknown'}",
    ]
    if rule.get("project"):
        details.append(f"project: {rule['project']}")
    if event.get("http_status"):
        details.append(f"http_status: {event['http_status']}")
    if summary:
        details.append(f"summary: {summary}")
    details.append("Inspect the target thread, determine whether this is a completion, quota failure, question, or approval request, and take the next action defined by the active task.")
    return "\n".join(details)


def queue_wake(
    rule: dict[str, Any],
    event: dict[str, Any],
    root: Path,
    config: dict[str, Any],
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    message = build_wake_message(rule, event, int(config.get("max_summary_chars", 1200)))
    attempts = max(1, int(config.get("queue_retries", 3)))
    run = runner or subprocess.run
    last_output = ""
    for attempt in range(1, attempts + 1):
        try:
            command = resolve_codex_command() + [
                "queue",
                "--thread",
                rule["wake_thread_id"],
                "--message",
                message,
                "--resume",
            ]
            kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": 120,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = run(command, **kwargs)
            last_output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0:
                log_event(root, "INFO", "wake_queued", "Coordinator wake queued.", target_id=rule["target_id"], event_type=event["type"], attempt=attempt)
                return True
        except Exception as exc:
            last_output = str(exc)
        log_event(root, "WARN", "wake_attempt_failed", "Coordinator wake attempt failed.", target_id=rule["target_id"], event_type=event["type"], attempt=attempt, output=last_output[-2000:])
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    log_event(root, "ERROR", "wake_failed", "Coordinator wake failed after retries.", target_id=rule["target_id"], event_type=event["type"], output=last_output[-2000:])
    return False


def process_cycle(
    root: Path,
    config: dict[str, Any],
    home: Path | None = None,
    queue_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, int]:
    events_to_send: list[tuple[dict[str, Any], dict[str, Any]]] = []
    counts = {"detected": 0, "queued": 0, "failed": 0}
    with state_lock(root):
        state = load_state(root)
        session_root = (home or codex_home())
        dirty = False
        for target_id, rule in list(state["watches"].items()):
            if rule.get("disabled"):
                continue
            try:
                original_offset = rule.get("offset")
                original_partial = rule.get("partial_b64")
                path = Path(rule["session_path"]) if rule.get("session_path") else None
                if path is None or not path.exists():
                    path = find_session_file(target_id, session_root)
                    if path is None:
                        continue
                    meta = session_metadata(path)
                    session_cwd = normalize_path((meta or {}).get("cwd"))
                    expected = normalize_path(rule.get("project"))
                    if expected and session_cwd and expected != session_cwd:
                        rule["error"] = f"session cwd {session_cwd} does not match project {expected}"
                        continue
                    rule["session_path"] = str(path)
                    rule["session_cwd"] = session_cwd
                    if rule.get("offset") is None:
                        rule["offset"] = path.stat().st_size if rule.get("start_mode", "from-now") == "from-now" else 0
                    if rule.get("error", "").startswith("session cwd"):
                        rule.pop("error", None)
                    dirty = True

                pending = rule.pop("pending_events", []) or []
                new_events, offset, partial = read_appended(path, int(rule.get("offset") or 0), str(rule.get("partial_b64") or ""))
                rule["offset"] = offset
                rule["partial_b64"] = partial
                if original_offset != offset or original_partial != partial or pending:
                    rule["last_checked_at"] = now_iso()
                    dirty = True
                allowed = set(rule.get("events") or DEFAULT_EVENT_TYPES)
                for event in pending + new_events:
                    if event.get("type") not in allowed:
                        continue
                    key = event_key(target_id, event)
                    if key in state["seen"]:
                        continue
                    state["seen"].append(key)
                    event["key"] = key
                    rule["last_event_at"] = now_iso()
                    events_to_send.append((rule.copy(), event))
                    counts["detected"] += 1
                    dirty = True
            except Exception as exc:
                rule["error"] = str(exc)
                dirty = True
                log_event(root, "ERROR", "watch_cycle_failed", "Failed to process watch.", target_id=target_id, error=str(exc))
        if dirty:
            save_state(root, state)

    for rule, event in events_to_send:
        ok = queue_wake(rule, event, root, config, runner=queue_runner)
        counts["queued" if ok else "failed"] += 1
        with state_lock(root):
            state = load_state(root)
            stats = state["stats"]
            stats["events_detected"] = int(stats.get("events_detected", 0)) + 1
            if ok:
                stats["wakeups_queued"] = int(stats.get("wakeups_queued", 0)) + 1
            else:
                stats["wakeups_failed"] = int(stats.get("wakeups_failed", 0)) + 1
                state["last_error"] = f"wake failed for {event.get('key')}"
            save_state(root, state)
    return counts


def daemon_running(root: Path) -> bool:
    lock = daemon_lock(root)
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
        return False
    return True


def command_add(args: argparse.Namespace, root: Path) -> int:
    target = validate_uuid(args.target, "target")
    wake = validate_uuid(args.wake, "wake")
    events = parse_event_types(args.events)
    home = codex_home()
    session = find_session_file(target, home)
    session_cwd = normalize_path((session_metadata(session) or {}).get("cwd")) if session else None
    project = normalize_path(args.project) if args.project else session_cwd
    if project and session_cwd and project != session_cwd:
        raise ThreadWatchError(f"target session cwd does not match --project: {session_cwd}")
    with state_lock(root):
        state = load_state(root)
        if args.replay_last:
            start_mode = "replay-last"
        else:
            start_mode = "from-now"
        rule: dict[str, Any] = {
            "target_id": target,
            "wake_thread_id": wake,
            "label": args.label or "",
            "project": project,
            "session_path": str(session) if session else None,
            "session_cwd": session_cwd,
            "events": events,
            "start_mode": start_mode,
            "offset": session.stat().st_size if session else 0,
            "partial_b64": "",
            "registered_at": now_iso(),
            "last_checked_at": None,
            "last_event_at": None,
            "pending_events": [],
            "disabled": False,
            "error": None,
        }
        if args.replay_last and session:
            last = find_last_event(session, events)
            if last:
                rule["pending_events"] = [last]
        state["watches"][target] = rule
        save_state(root, state)
    result = {
        "target_id": target,
        "wake_thread_id": wake,
        "project": project,
        "events": events,
        "session_path": str(session) if session else None,
        "start_mode": start_mode,
        "pending_replay": bool(rule.get("pending_events")),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_remove(args: argparse.Namespace, root: Path) -> int:
    target = validate_uuid(args.target, "target")
    with state_lock(root):
        state = load_state(root)
        removed = state["watches"].pop(target, None)
        save_state(root, state)
    if not removed:
        raise ThreadWatchError(f"Watch not found: {target}")
    print(f"Removed watch {target}")
    return 0


def _watch_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for target, rule in sorted(state["watches"].items()):
        rows.append({
            "target_id": target,
            "wake_thread_id": rule.get("wake_thread_id"),
            "label": rule.get("label"),
            "project": rule.get("project"),
            "events": rule.get("events"),
            "session_path": rule.get("session_path"),
            "offset": rule.get("offset"),
            "last_event_at": rule.get("last_event_at"),
            "error": rule.get("error"),
        })
    return rows


def command_list(args: argparse.Namespace, root: Path) -> int:
    state = load_state(root)
    rows = _watch_rows(state)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No watches registered.")
        return 0
    for row in rows:
        print(f"{row['target_id']} -> {row['wake_thread_id']}  {row.get('label') or ''}")
        print(f"  project: {row.get('project') or '(unknown)'}")
        print(f"  events: {', '.join(row.get('events') or [])}")
        print(f"  session: {row.get('session_path') or '(waiting for log)'}")
        if row.get("error"):
            print(f"  error: {row['error']}")
    return 0


def command_start(args: argparse.Namespace, root: Path) -> int:
    config = ensure_config(root)
    if daemon_running(root):
        print("Daemon is already running.")
        return 0
    with state_lock(root):
        state = load_state(root)
        if not state["watches"]:
            print("Warning: starting with no registered watches.")
    stop_path(root).unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "run"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(root),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if daemon_running(root):
            pid = pid_path(root).read_text(encoding="utf-8").strip() if pid_path(root).exists() else "unknown"
            print(f"Daemon started (pid {pid}).")
            return 0
        time.sleep(0.1)
    raise ThreadWatchError("Daemon did not start. Check daemon.log.")


def command_stop(args: argparse.Namespace, root: Path) -> int:
    if not daemon_running(root):
        pid_path(root).unlink(missing_ok=True)
        stop_path(root).unlink(missing_ok=True)
        print("Daemon is not running.")
        return 0
    stop_path(root).write_text("stop\n", encoding="ascii")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not daemon_running(root):
            stop_path(root).unlink(missing_ok=True)
            pid_path(root).unlink(missing_ok=True)
            print("Daemon stopped.")
            return 0
        time.sleep(0.1)
    raise ThreadWatchError("Daemon did not stop within 10 seconds.")


def command_run(args: argparse.Namespace, root: Path) -> int:
    config = ensure_config(root)
    if args.once:
        counts = process_cycle(root, config)
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    lock = daemon_lock(root)
    if not lock.acquire(blocking=False):
        print("Another daemon is already running.", file=sys.stderr)
        return 1
    lock_held = True
    pid_path(root).write_text(str(os.getpid()), encoding="ascii")
    stop_path(root).unlink(missing_ok=True)
    log_event(root, "INFO", "daemon_started", "ThreadWatch daemon started.", pid=os.getpid())
    try:
        while not stop_path(root).exists():
            try:
                process_cycle(root, config)
            except Exception as exc:
                log_event(root, "ERROR", "daemon_cycle_failed", "Daemon cycle failed.", error=str(exc))
            time.sleep(max(0.2, float(config.get("poll_interval_seconds", 1.0))))
    except KeyboardInterrupt:
        pass
    finally:
        log_event(root, "INFO", "daemon_stopped", "ThreadWatch daemon stopped.", pid=os.getpid())
        try:
            if pid_path(root).exists() and pid_path(root).read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path(root).unlink(missing_ok=True)
        except OSError:
            pass
        stop_path(root).unlink(missing_ok=True)
        if lock_held:
            lock.release()
    return 0


def command_status(args: argparse.Namespace, root: Path) -> int:
    state = load_state(root)
    pid = None
    if pid_path(root).exists():
        try:
            pid = int(pid_path(root).read_text(encoding="ascii").strip())
        except ValueError:
            pid = None
    result = {
        "data_dir": str(root),
        "daemon_running": daemon_running(root),
        "pid": pid,
        "watch_count": len(state["watches"]),
        "watches": _watch_rows(state),
        "stats": state.get("stats"),
        "last_error": state.get("last_error"),
        "log": str(daemon_log_path(root)),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Data: {root}")
        print(f"Daemon: {'running' if result['daemon_running'] else 'stopped'} (pid {pid or 'n/a'})")
        print(f"Watches: {result['watch_count']}")
        for row in result["watches"]:
            print(f"  {row['target_id']} -> {row['wake_thread_id']}")
        print(f"Stats: {json.dumps(result['stats'], ensure_ascii=False)}")
        if result["last_error"]:
            print(f"Last error: {result['last_error']}")
    return 0


def command_logs(args: argparse.Namespace, root: Path) -> int:
    path = daemon_log_path(root)
    if not path.exists():
        print(f"No log yet: {path}")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    if args.follow:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            try:
                while True:
                    line = handle.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    return 0


def command_doctor(args: argparse.Namespace, root: Path) -> int:
    checks: list[dict[str, Any]] = []
    ok = True

    def add(name: str, status: str, detail: str) -> None:
        nonlocal ok
        checks.append({"name": name, "status": status, "detail": detail})
        if status == "fail":
            ok = False

    add("python", "ok" if sys.version_info >= (3, 11) else "fail", sys.version.split()[0])
    try:
        command = resolve_codex_command()
        help_result = subprocess.run(command + ["queue", "--help"], capture_output=True, text=True, timeout=20)
        add("codex", "ok" if help_result.returncode == 0 else "fail", " ".join(command))
    except Exception as exc:
        add("codex", "fail", str(exc))
    sessions = codex_home() / "sessions"
    add("sessions", "ok" if sessions.exists() else "fail", str(sessions))
    try:
        probe = root / ".write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        add("data_dir", "ok", str(root))
    except OSError as exc:
        add("data_dir", "fail", str(exc))
    state = load_state(root)
    for target, rule in state["watches"].items():
        path = Path(rule["session_path"]) if rule.get("session_path") else find_session_file(target)
        if not path or not path.exists():
            add(f"watch:{target}", "warn", "session log not found yet")
            continue
        meta = session_metadata(path)
        expected = normalize_path(rule.get("project"))
        actual = normalize_path((meta or {}).get("cwd"))
        if expected and actual and expected != actual:
            add(f"watch:{target}", "fail", f"cwd mismatch: {actual}")
        else:
            add(f"watch:{target}", "ok", str(path))
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_hits = list(startup.glob("*ThreadWatch*")) + list(startup.glob("*threadwatch*")) if startup.exists() else []
    add("startup", "ok" if not startup_hits else "warn", "no ThreadWatch startup entry" if not startup_hits else str(startup_hits))
    result = {"ok": ok, "checks": checks}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
    return 0 if ok else 1


def command_test_event(args: argparse.Namespace, root: Path) -> int:
    target = validate_uuid(args.target, "target")
    state = load_state(root)
    rule = state["watches"].get(target)
    if not rule:
        raise ThreadWatchError(f"Watch not found: {target}")
    event = {
        "type": args.type,
        "turn_id": f"test-{uuid.uuid4()}",
        "summary": args.summary or "Synthetic threadwatch event.",
        "http_status": 429 if args.type == "failed" else None,
        "detected_at": now_iso(),
    }
    message = build_wake_message(rule, event, int(load_config(root).get("max_summary_chars", 1200)))
    if not args.send:
        print(message)
        return 0
    ok = queue_wake(rule, event, root, load_config(root))
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threadwatch", description="External event watcher for Codex threads.")
    parser.add_argument("--version", action="version", version="codex-auto-supervisor 1.0.0")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Register a target thread.")
    add.add_argument("--target", required=True)
    add.add_argument("--wake", required=True)
    add.add_argument("--project")
    add.add_argument("--label", default="")
    add.add_argument("--events")
    add.add_argument("--replay-last", action="store_true")
    add.set_defaults(func=command_add)

    remove = sub.add_parser("remove", help="Remove a target thread.")
    remove.add_argument("--target", required=True)
    remove.set_defaults(func=command_remove)

    list_cmd = sub.add_parser("list", help="List registered targets.")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=command_list)

    start = sub.add_parser("start", help="Start the background daemon.")
    start.set_defaults(func=command_start)

    stop = sub.add_parser("stop", help="Stop the background daemon.")
    stop.set_defaults(func=command_stop)

    run = sub.add_parser("run", help="Run the daemon in the foreground.")
    run.add_argument("--once", action="store_true")
    run.set_defaults(func=command_run)

    status = sub.add_parser("status", help="Show daemon and watch status.")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)

    logs = sub.add_parser("logs", help="Show the daemon log.")
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(func=command_logs)

    doctor = sub.add_parser("doctor", help="Check the local environment.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)

    test_event = sub.add_parser("test-event", help="Render or send a synthetic event.")
    test_event.add_argument("--target", required=True)
    test_event.add_argument("--type", required=True, choices=sorted(DEFAULT_EVENT_TYPES))
    test_event.add_argument("--summary")
    test_event.add_argument("--send", action="store_true")
    test_event.set_defaults(func=command_test_event)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = data_dir()
    ensure_config(root)
    try:
        return int(args.func(args, root))
    except ThreadWatchError as exc:
        print(f"threadwatch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

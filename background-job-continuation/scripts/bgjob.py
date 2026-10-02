#!/usr/bin/env python3
"""Linux tmux jobs with durable, owner-verified Codex continuation (stdlib only)."""
import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

VERSION = "2.1.0"
FINAL = {"followup_completed", "followup_finished_with_error", "dispatch_failed", "configuration_mismatch", "owner_conflict"}


class Boundary(RuntimeError):
    pass


class RpcError(Boundary):
    def __init__(self, error):
        super().__init__(json.dumps(error, ensure_ascii=False))
        self.error = error


class ConfigurationMismatch(Boundary):
    pass


class WriterConflict(Boundary):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise Boundary(f"Already monitored: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def home():
    return str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve())


def scope(codex_home):
    try:
        host = Path("/etc/machine-id").read_text().strip()
    except OSError:
        host = socket.gethostname()
    return hashlib.sha256(f"{host}:{os.getuid()}:{codex_home}".encode()).hexdigest()


class Rpc:
    """WebSocket JSON-RPC over the existing server's raw stdio proxy."""
    def __init__(self, codex, endpoint, logs=None, timeout=15):
        self.timeout = timeout
        self.logs = Path(logs) if logs else None
        self.stderr = self.proc = None
        self.sel = selectors.DefaultSelector()
        self.buffer, self.replies = b"", {}
        self.completed_events = {}
        self.pending_requests = {}
        try:
            if self.logs:
                self.logs.mkdir(parents=True, exist_ok=True)
                self.stderr = (self.logs / "continuation.stderr.log").open("ab")
            self.proc = subprocess.Popen([codex, "app-server", "proxy", "--sock", endpoint],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr or subprocess.DEVNULL, bufsize=0)
            self.sel.register(self.proc.stdout, selectors.EVENT_READ)
            self.handshake()
            self.info = self.call("initialize", {
                "clientInfo": {"name": "background_job_continuation", "version": VERSION},
                "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def log(self, direction, message):
        if self.logs:
            with (self.logs / "continuation.rpc.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"at": now(), "direction": direction, "message": message}, ensure_ascii=False) + "\n")

    def write(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.proc.stdin.fileno(), view):]

    def fill(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.sel.select(remaining):
            raise Boundary("App-server reply timed out")
        chunk = os.read(self.proc.stdout.fileno(), 65536)
        if not chunk:
            raise Boundary(f"Proxy disconnected (exit={self.proc.poll()})")
        self.buffer += chunk

    def take(self, count, deadline):
        while len(self.buffer) < count:
            self.fill(deadline)
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        self.write((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        deadline = time.monotonic() + self.timeout
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > 65536:
                raise Boundary("Oversized WebSocket handshake")
            self.fill(deadline)
        headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = headers.decode("ascii").split("\r\n")
        fields = {k.lower().strip(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if " 101 " not in lines[0] or fields.get("sec-websocket-accept") != expected or fields.get("upgrade", "").lower() != "websocket":
            raise Boundary("App-server rejected WebSocket upgrade")

    def frame(self, opcode, payload, final=True):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([(0x80 if final else 0) | opcode])
        header += bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack("!H", size) if size < 65536 else bytes([0x80 | 127]) + struct.pack("!Q", size)
        self.write(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, message):
        self.log("send", message)
        data = json.dumps(message).encode()
        # Stay below the server's unfragmented-message limit.
        for start in range(0, len(data), 1048576):
            self.frame(1 if start == 0 else 0, data[start:start + 1048576], start + 1048576 >= len(data))

    def receive(self, deadline):
        fragments = bytearray()
        started = False
        while True:
            first, second = self.take(2, deadline)
            opcode, final, size = first & 15, bool(first & 0x80), second & 127
            if first & 0x70 or second & 0x80:
                raise Boundary("Invalid server WebSocket frame flags")
            if size == 126:
                size = struct.unpack("!H", self.take(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.take(8, deadline))[0]
            if size > 67108864 or len(fragments) + size > 67108864:
                raise Boundary("WebSocket message exceeds 64 MiB observation limit")
            payload = self.take(size, deadline)
            if opcode == 8:
                raise Boundary("App-server closed WebSocket")
            if opcode in (9, 10):
                if not final or size > 125:
                    raise Boundary("Invalid WebSocket control frame")
                if opcode == 9:
                    self.frame(10, payload)
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise Boundary("Expected WebSocket text or continuation frame")
            fragments.extend(payload)
            if final:
                try:
                    message = json.loads(fragments)
                except (ValueError, UnicodeError) as exc:
                    raise Boundary("Non-JSON WebSocket message") from exc
                self.log("receive", message)
                return message

    def call(self, method, params=None):
        request_id = uuid.uuid4().hex
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self.replies.pop(request_id, None)
            if message is None:
                message = self.receive(deadline)
            if message.get("id") != request_id:
                if "id" in message:
                    if "method" in message:
                        # Keep approval/client-tool requests pending. Never reject or approve
                        # merely because the SSH UI is absent.
                        self.pending_requests[message["id"]] = message
                    else:
                        self.replies[message["id"]] = message
                elif message.get("method") == "turn/completed":
                    params = message.get("params", {})
                    turn = params.get("turn", {})
                    if params.get("threadId") and turn.get("id"):
                        self.completed_events[(params["threadId"], turn["id"])] = message
                continue
            if "error" in message:
                raise RpcError(message["error"])
            return message.get("result", {})

    def close(self):
        self.sel.close()
        if self.proc:
            if self.proc.stdin:
                self.proc.stdin.close()
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            if self.proc.stdout:
                self.proc.stdout.close()
        if self.stderr:
            self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def verify(rpc, thread_id, cwd):
    thread = rpc.call("thread/read", {"threadId": thread_id})["thread"]
    if thread.get("id") != thread_id:
        raise Boundary("Thread ID mismatch")
    if not thread.get("cwd") or Path(thread["cwd"]).resolve() != Path(cwd).resolve():
        raise Boundary("Workspace mismatch")
    if not isinstance(thread.get("status"), dict) or "type" not in thread["status"]:
        raise Boundary("Missing live thread status")
    loaded, cursor = set(), None
    for _ in range(100):
        page = rpc.call("thread/loaded/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
        loaded.update(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    else:
        raise Boundary("Loaded-thread pagination limit reached")
    return {"thread_id": thread_id, "cwd": str(Path(cwd).resolve()),
            "loaded": thread_id in loaded, "status": thread["status"], "server": rpc.info}


def turns(rpc, thread_id):
    result, cursor = [], None
    try:
        for _ in range(100):
            page = rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 100,
                "itemsView": "full", **({"cursor": cursor} if cursor else {})})
            result.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return result
        raise Boundary("Turn pagination limit reached; refusing to guess")
    except RpcError as exc:
        if exc.error.get("code") != -32601:
            raise
        return rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]["turns"]


def matches(turn, marker, dispatch_id):
    for item in turn.get("items", []):
        if item.get("type") != "userMessage":
            continue
        if item.get("clientId") == dispatch_id:
            return True
        if any(p.get("type") == "text" and marker in p.get("text", "") for p in item.get("content", [])):
            return True
    return False


def candidates(explicit, codex_home):
    if explicit:
        return [str(Path(explicit).absolute())]
    paths = [os.environ.get("BACKGROUND_JOB_APP_SERVER_SOCKET"),
             str(Path(codex_home) / "app-server-control/app-server-control.sock")]
    # Retain the daemon's stable symlink, whose target can change after restart.
    result, seen = [], set()
    for path in filter(None, paths):
        canonical = str(Path(path).resolve())
        if canonical not in seen:
            seen.add(canonical)
            result.append(str(Path(path).absolute()))
    return result


def settings_snapshot(response):
    required = ("model", "modelProvider", "approvalPolicy", "approvalsReviewer", "sandbox", "cwd")
    if any(key not in response for key in required):
        raise ConfigurationMismatch("Server did not supply effective thread settings")
    keys = required + ("reasoningEffort", "serviceTier", "activePermissionProfile", "disabledPluginIds")
    result = {key: response[key] for key in keys if key in response}
    result["cwd"] = str(Path(result["cwd"]).resolve())
    if "collaborationMode" in response:
        # Preserve comparisons without copying developer instructions into the manifest.
        result["collaborationModeDigest"] = hashlib.sha256(json.dumps(
            response["collaborationMode"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def subscribe(rpc, thread_id, cwd, expected=None):
    try:
        response = rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    except RpcError as exc:
        if "active writer" in str(exc).lower():
            raise WriterConflict("Original server could not obtain its thread writer; no external fallback") from exc
        raise
    if response.get("thread", {}).get("id") != thread_id:
        raise ConfigurationMismatch("Resumed thread ID differs from saved target")
    settings = settings_snapshot(response)
    if settings["cwd"] != str(Path(cwd).resolve()):
        raise ConfigurationMismatch("Resumed workspace differs from saved target")
    if expected is not None and settings != expected:
        changed = sorted(key for key in set(settings) | set(expected) if settings.get(key) != expected.get(key))
        raise ConfigurationMismatch("Effective settings changed; no turn dispatched: " + ", ".join(changed))
    return settings


class OwnerLease:
    """A durable worker's persistent subscription to the original app-server."""
    def __init__(self, jobdir, job):
        self.jobdir, self.job, self.rpc = jobdir, job, None
        self.subscribed = False
        self.blocked = None

    def close(self):
        if self.rpc:
            self.rpc.close()
        self.rpc, self.subscribed = None, False

    def ensure(self, refresh=False):
        if self.blocked:
            raise self.blocked
        if self.rpc is None:
            self.rpc = Rpc(self.job["codex"], self.job["socket"], logs=self.jobdir,
                           timeout=self.job["rpc_timeout"])
        identity = verify(self.rpc, self.job["thread_id"], self.job["cwd"])
        if not identity["loaded"] and not self.job.get("settings"):
            raise Boundary("Legacy job has no verified settings snapshot; cannot reload an unloaded thread")
        if not self.subscribed or not identity["loaded"] or refresh:
            was_loaded = identity["loaded"]
            try:
                actual = subscribe(self.rpc, self.job["thread_id"], self.job["cwd"], self.job.get("settings"))
            except (ConfigurationMismatch, WriterConflict) as exc:
                self.blocked = exc
                raise
            self.subscribed = True
            identity = verify(self.rpc, self.job["thread_id"], self.job["cwd"])
            if not identity["loaded"]:
                raise Boundary("Original server did not load the resumed thread")
            previous = read_json(self.jobdir / "lease.json", {})
            atomic(self.jobdir / "lease.json", {"updated_at": now(), "socket": self.job["socket"],
                "thread_id": self.job["thread_id"], "subscribed": True, "settings_verified": self.job.get("settings") == actual,
                "reload_count": previous.get("reload_count", 0) + (0 if was_loaded else 1)})
        return self.rpc, identity


def detect(codex, endpoints, thread_id, cwd):
    owners, errors = [], []
    for endpoint in endpoints:
        try:
            if not stat.S_ISSOCK(Path(endpoint).stat().st_mode):
                raise Boundary("Not a Unix socket")
            with Rpc(codex, endpoint) as rpc:
                identity = verify(rpc, thread_id, cwd)
                if identity["loaded"]:
                    # Do not detach computation if this version/history cannot prove completion.
                    turns(rpc, thread_id)
                    identity["settings"] = subscribe(rpc, thread_id, cwd)
                    rpc.call("thread/unsubscribe", {"threadId": thread_id})
            if not identity["loaded"]:
                raise Boundary("Thread is not loaded by this server")
            owners.append({"socket": endpoint, **identity})
        except (OSError, Boundary, KeyError, ValueError) as exc:
            errors.append({"socket": endpoint, "error": str(exc)})
    if len(owners) != 1 or (len(endpoints) > 1 and errors):
        raise Boundary(json.dumps({"error": "Expected one unambiguous verified owner; supply --socket for custom endpoints", "owners": owners, "diagnostics": errors}, ensure_ascii=False))
    return owners[0]


def put_state(jobdir, state, **fields):
    record = read_json(jobdir / "continuation.json", {})
    record.update(state=state, updated_at=now(), **fields)
    atomic(jobdir / "continuation.json", record)
    return record


def prompt_for(job, jobdir, marker):
    return (f"{marker}\nThe authorized background job has finished.\n"
            f"Completion record: {jobdir / 'completion.json'}\n"
            f"stdout: {jobdir / 'job.stdout.log'}\nstderr: {jobdir / 'job.stderr.log'}\n"
            f"Artifacts: {json.dumps(job['artifacts'], ensure_ascii=False)}\n"
            "Read the completion record and relevant results before deciding the next action. "
            "Check exit code and partial results. Do not rerun completed computation because "
            "continuation was delayed. Continue only with this authorized next task:\n" + job["next_task"])


def observe_followup(rpc, jobdir, job, record, identity):
    history = turns(rpc, job["thread_id"])
    matched = [t for t in history if matches(t, record["marker"], record["dispatch_id"])]
    if len(matched) > 1:
        return put_state(jobdir, "dispatch_outcome_unknown", error="Multiple turns match the same marker")
    if not matched:
        return put_state(jobdir, "completion_status_unverified" if record.get("turn_id") else "dispatch_outcome_unknown", error="Exact prompt not yet visible in turn history")
    turn = matched[0]
    if record.get("turn_id") and turn["id"] != record["turn_id"]:
        return put_state(jobdir, "dispatch_outcome_unknown", error="Reply and prompt turn IDs differ")
    event = rpc.completed_events.get((job["thread_id"], turn["id"]))
    source = "read-only-turn-query"
    history_status = turn.get("status")
    if event and event.get("params", {}).get("turn", {}).get("status") in {"completed", "failed", "interrupted"}:
        turn = {**turn, "status": event["params"]["turn"]["status"], "error": event["params"]["turn"].get("error")}
        source = "turn/completed"
        atomic(jobdir / "continuation.completed-event.json", event)
    atomic(jobdir / "continuation.evidence.json", {"at": now(), "source": source,
        "thread_id": job["thread_id"], "turn": turn, "history_status": history_status})
    status = turn.get("status")
    delivery = "existing-turn" if turn["id"] in record.get("previous_turn_ids", []) else "new-turn"
    fields = {"turn_id": turn["id"], "turn_status": status, "delivery": delivery,
        "evidence_source": source, "runtime_status": identity["status"], "prompt_verified": True}
    if status == "completed":
        return put_state(jobdir, "followup_completed", **fields)
    if status in {"failed", "interrupted"}:
        return put_state(jobdir, "followup_finished_with_error", turn_error=turn.get("error"), **fields)
    state = "followup_delivered_to_active_turn" if delivery == "existing-turn" else "followup_started"
    if "waitingOnApproval" in identity["status"].get("activeFlags", []):
        state = "waiting_approval"
    if rpc.pending_requests:
        fields["pending_server_methods"] = sorted({m.get("method", "unknown") for m in rpc.pending_requests.values()})
    return put_state(jobdir, state, **fields)


def monitor(jobdir, job, lease=None):
    owned_lease = lease is None
    lease = lease or OwnerLease(jobdir, job)
    try:
        with lock(jobdir / "dispatch.lock", blocking=False):
            if read_json(jobdir / "continuation.json", {}).get("state") in FINAL:
                return read_json(jobdir / "continuation.json")
            if not read_json(jobdir / "completion.json"):
                return put_state(jobdir, "job_execution_unverified", error="No completion record; computation was not rerun")
            wait_deadline = time.monotonic() + job["wait_timeout"]
            observe_deadline = None
            while True:
                record = read_json(jobdir / "continuation.json", {})
                known, attempted = bool(record.get("turn_id")), bool(record.get("dispatch_id"))
                if known and observe_deadline is None:
                    observe_deadline = time.monotonic() + job["observe_timeout"]
                if time.monotonic() >= (observe_deadline if known else wait_deadline):
                    state = "completion_status_unverified" if known else "dispatch_outcome_unknown" if attempted else record.get("state", "waiting_endpoint")
                    return put_state(jobdir, state, wait_expired=True)
                try:
                    rpc, identity = lease.ensure()
                    if record.get("dispatch_id"):
                        result = observe_followup(rpc, jobdir, job, record, identity)
                        if result["state"] in FINAL:
                            return result
                    elif not job.get("settings"):
                        return put_state(jobdir, "configuration_mismatch", error="Legacy manifest lacks a verified settings snapshot; no new dispatch")
                    elif identity["status"]["type"] != "idle":
                        state = "waiting_approval" if "waitingOnApproval" in identity["status"].get("activeFlags", []) else "waiting_idle"
                        put_state(jobdir, state, runtime_status=identity["status"])
                    else:
                        try:
                            with lock(Path(job["lock_root"]) / f"{job['thread_id']}.lock", blocking=False):
                                rpc, identity = lease.ensure(refresh=True)
                                if identity["status"]["type"] == "idle":
                                    history = turns(rpc, job["thread_id"])
                                    dispatch_id = str(uuid.uuid4())
                                    marker = f"[background-job-continuation job={job['job_id']} dispatch={dispatch_id}]"
                                    put_state(jobdir, "dispatching", dispatch_id=dispatch_id, marker=marker,
                                        attempted_at=now(), previous_turn_ids=[t["id"] for t in history], server=rpc.info)
                                    try:
                                        response = rpc.call("turn/start", {"threadId": job["thread_id"], "input": [{"type": "text", "text": prompt_for(job, jobdir, marker)}], "clientUserMessageId": dispatch_id})
                                        turn = response["turn"]
                                        put_state(jobdir, "completion_status_unverified", turn_id=turn["id"], turn_status=turn.get("status"), dispatch_response=response)
                                    except RpcError as exc:
                                        return put_state(jobdir, "dispatch_failed", error=exc.error)
                                    except (OSError, Boundary, KeyError, ValueError) as exc:
                                        put_state(jobdir, "dispatch_outcome_unknown", error=str(exc))
                                        lease.close()
                        except (ConfigurationMismatch, WriterConflict):
                            raise
                        except Boundary as exc:
                            put_state(jobdir, "waiting_idle", error=str(exc))
                except ConfigurationMismatch as exc:
                    return put_state(jobdir, "configuration_mismatch", error=str(exc))
                except WriterConflict as exc:
                    return put_state(jobdir, "owner_conflict", error=str(exc))
                except (OSError, Boundary, KeyError, ValueError) as exc:
                    lease.close()
                    record = read_json(jobdir / "continuation.json", {})
                    state = "completion_status_unverified" if record.get("turn_id") else "dispatch_outcome_unknown" if record.get("dispatch_id") else "waiting_endpoint"
                    put_state(jobdir, state, error=str(exc))
                time.sleep(job["poll_interval"])
    finally:
        if owned_lease:
            lease.close()


def tmux_args(job):
    return ["tmux"] + (["-S", job["tmux_socket"]] if job.get("tmux_socket") else [])


def start_tmux(jobdir, job, recovery=False):
    session = "bgjob-" + job["job_id"][:12] + ("-recover-" + uuid.uuid4().hex[:6] if recovery else "")
    gate = jobdir / ("ready-" + uuid.uuid4().hex + ".json")
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "_worker", "--job-dir", str(jobdir), "--gate", str(gate)] + (["--recover-only"] if recovery else []))
    args = tmux_args(job)
    subprocess.run(args + ["new-session", "-d", "-s", session, "-c", job["cwd"],
        "-e", "CODEX_HOME=" + job["codex_home"], "-e", "PATH=" + os.environ.get("PATH", os.defpath), command], check=True, capture_output=True)
    subprocess.run(args + ["set-option", "-p", "-t", session + ":0.0", "remain-on-exit", "on"], check=True, capture_output=True)
    info = {"at": now(), "session": session, "attach": shlex.join(args + ["attach-session", "-t", session])}
    atomic(jobdir / "tmux.json", info)
    atomic(gate, {"ready": True})
    return info


def run_computation(jobdir, job, lease):
    if (jobdir / "completion.json").exists():
        return
    if (jobdir / "execution.json").exists():
        put_state(jobdir, "job_execution_unverified", error="Execution already attempted; refusing to rerun")
        return
    execution = {"started_at": now(), "state": "starting", "command": job["command"]}
    atomic(jobdir / "execution.json", execution)
    child, old_handlers = None, {}
    def forward(signum, _frame):
        if child and child.poll() is None:
            os.killpg(child.pid, signum)
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, forward)
        with (jobdir / "job.stdout.log").open("ab") as stdout, (jobdir / "job.stderr.log").open("ab") as stderr:
            child = subprocess.Popen(job["command"], cwd=job["cwd"], stdout=stdout, stderr=stderr, start_new_session=True)
            execution.update(state="running", pid=child.pid)
            atomic(jobdir / "execution.json", execution)
            while True:
                try:
                    exit_code = child.wait(timeout=min(5, job["poll_interval"]))
                    break
                except subprocess.TimeoutExpired:
                    try:
                        lease.ensure()
                    except ConfigurationMismatch as exc:
                        atomic(jobdir / "lease.error.json", {"at": now(), "error": str(exc)})
                        lease.close()
                    except (OSError, Boundary, KeyError, ValueError) as exc:
                        atomic(jobdir / "lease.error.json", {"at": now(), "error": str(exc)})
                        lease.close()
        error = None
    except OSError as exc:
        exit_code, error = child.wait() if child is not None else 127, str(exc)
    finally:
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
    atomic(jobdir / "completion.json", {"job_id": job["job_id"], "started_at": execution["started_at"], "ended_at": now(),
        "exit_code": exit_code, "state": "job_finished", "error": error,
        "artifacts": [{"path": p, "exists": Path(p).exists()} for p in job["artifacts"]],
        "stdout": str(jobdir / "job.stdout.log"), "stderr": str(jobdir / "job.stderr.log")})


def worker(args):
    jobdir = Path(args.job_dir).resolve()
    job = read_json(jobdir / "job.json")
    if job["scope"] != scope(home()):
        raise Boundary("Execution host/user/Codex home differs from recorded job")
    gate, deadline = Path(args.gate), time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() > deadline:
            raise Boundary("tmux setup timed out; job not executed")
        time.sleep(0.1)
    gate.unlink()
    with lock(jobdir / "worker.lock", blocking=False):
        lease = OwnerLease(jobdir, job)
        try:
            try:
                lease.ensure()
            except ConfigurationMismatch as exc:
                return put_state(jobdir, "configuration_mismatch", error=str(exc))
            except (OSError, Boundary, KeyError, ValueError) as exc:
                atomic(jobdir / "lease.error.json", {"at": now(), "error": str(exc)})
                lease.close()
            if not args.recover_only:
                run_computation(jobdir, job, lease)
            return monitor(jobdir, job, lease)
        finally:
            lease.close()


def binding(args):
    cwd = str(Path(args.cwd).resolve())
    if not Path(cwd).is_dir():
        raise Boundary("Workspace does not exist")
    thread_id = args.thread or os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise Boundary("Exact --thread or trusted CODEX_THREAD_ID required")
    try:
        uuid.UUID(thread_id)
    except ValueError as exc:
        raise Boundary("Thread ID must be an exact UUID") from exc
    return detect(args.codex, candidates(args.socket, home()), thread_id, cwd)


def launch(args):
    identity = binding(args)
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise Boundary("Provide command arguments after --")
    next_task = Path(args.next_file).read_text(encoding="utf-8").strip()
    if not next_task:
        raise Boundary("Authorized next-task file is empty")
    jobdir = Path(args.job_dir).resolve()
    jobdir.mkdir(parents=True, exist_ok=False)
    codex = shutil.which(args.codex)
    if not codex:
        raise Boundary("Codex executable is unavailable")
    job = {"version": VERSION, "job_id": uuid.uuid4().hex, "created_at": now(),
        "scope": scope(home()), "codex_home": home(), "codex": str(Path(codex).absolute()),
        "thread_id": identity["thread_id"], "cwd": identity["cwd"], "socket": identity["socket"],
        "identity": identity, "settings": identity["settings"], "command": command, "next_task": next_task,
        "artifacts": [str(Path(p).resolve()) for p in args.artifact],
        "tmux_socket": str(Path(args.tmux_socket).resolve()) if args.tmux_socket else None,
        "lock_root": str(Path(args.lock_root or Path(home()) / "background-job-continuation/locks").resolve()),
        "poll_interval": args.poll_interval, "wait_timeout": args.wait_timeout,
        "observe_timeout": args.observe_timeout, "rpc_timeout": args.rpc_timeout}
    atomic(jobdir / "job.json", job)
    put_state(jobdir, "pending")
    return {"job_dir": str(jobdir), **start_tmux(jobdir, job), "stdout": str(jobdir / "job.stdout.log"), "stderr": str(jobdir / "job.stderr.log")}


def status(args):
    jobdir = Path(args.job_dir).resolve()
    job = read_json(jobdir / "job.json")
    if not job:
        raise Boundary("No job manifest")
    return {"job_dir": str(jobdir), "thread_id": job["thread_id"], "cwd": job["cwd"],
        "completion": read_json(jobdir / "completion.json"), "continuation": read_json(jobdir / "continuation.json"), "tmux": read_json(jobdir / "tmux.json")}


def recover(args):
    jobdir = Path(args.job_dir).resolve()
    job = read_json(jobdir / "job.json")
    if not job or job["scope"] != scope(home()):
        raise Boundary("Job missing or belongs to another host/user/Codex home")
    with lock(jobdir / "worker.lock", blocking=False):
        if read_json(jobdir / "continuation.json", {}).get("state") in FINAL:
            return status(args)
        if not (jobdir / "completion.json").exists():
            put_state(jobdir, "job_execution_unverified", error="No completion record; computation was not rerun")
            return status(args)
        return {"job_dir": str(jobdir), **start_tmux(jobdir, job, recovery=True)}


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Must be positive and finite")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VERSION)
    subs = root.add_subparsers(dest="action", required=True)
    for name in ("doctor", "launch"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--cwd", default=os.getcwd())
        cmd.add_argument("--thread")
        cmd.add_argument("--socket")
        cmd.add_argument("--codex", default="codex")
        if name == "launch":
            cmd.add_argument("--job-dir", required=True)
            cmd.add_argument("--next-file", required=True)
            cmd.add_argument("--artifact", action="append", default=[])
            cmd.add_argument("--tmux-socket")
            cmd.add_argument("--lock-root")
            cmd.add_argument("--poll-interval", type=positive, default=5)
            cmd.add_argument("--wait-timeout", type=positive, default=86400)
            cmd.add_argument("--observe-timeout", type=positive, default=3600)
            cmd.add_argument("--rpc-timeout", type=positive, default=15)
            cmd.add_argument("command", nargs=argparse.REMAINDER)
    for name in ("status", "recover", "_worker"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--job-dir", required=True)
        if name == "_worker":
            cmd.add_argument("--gate", required=True)
            cmd.add_argument("--recover-only", action="store_true")
    return root


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        result = {"version": VERSION, "codex_home": home(), **binding(args)} if args.action == "doctor" else {"launch": launch, "status": status, "recover": recover, "_worker": worker}[args.action](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, Boundary, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

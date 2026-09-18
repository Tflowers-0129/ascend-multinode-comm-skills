#!/usr/bin/env python3
"""配置驱动的 vLLM-Ascend 服务编排、验收与有界参数寻优。"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import itertools
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
SSH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*\Z")
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
PYTHON_NAME_RE = re.compile(r"python(?:3(?:\.\d+)?)?\Z")
SECRET_KEYS = {"password", "passwd", "secret", "private_key", "api_key", "access_token",
               "authorization", "cookie"}
DANGEROUS_ENV_KEYS = {"PATH", "LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "LD_DEBUG",
                      "GCONV_PATH", "LOCPATH", "HOME", "XDG_CONFIG_HOME", "ZDOTDIR",
                      "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "PYTHONPATH",
                      "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONWARNINGS",
                      "PYTHONUSERBASE", "PYTHONBREAKPOINT", "PERL5OPT", "RUBYOPT",
                      "NODE_OPTIONS"}
WORKER_PREFIX = "ASCEND_WORKFLOW_RESULT "
TEMPLATE_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
REGEX_PREFIX = "ASCEND_WORKFLOW_REGEX_RESULT "
REGEX_TIMEOUT_S = 5


REGEX_EVAL_SOURCE = r'''
import base64, json, math, re, sys
PREFIX = "ASCEND_WORKFLOW_REGEX_RESULT "
payload = json.load(open(sys.argv[1], encoding="utf-8"))
text = open(sys.argv[2], encoding="utf-8", errors="replace").read()
results = []
for operation in payload["operations"]:
    result = {"id": operation["id"], "kind": operation["kind"]}
    try:
        compiled = re.compile(operation["pattern"], re.MULTILINE)
        if operation["kind"] == "search":
            result["matched"] = compiled.search(text) is not None
        elif operation["kind"] == "count":
            result["count"] = sum(1 for _ in compiled.finditer(text))
        elif operation["kind"] == "metric":
            samples = 0
            valid = True
            total = 0.0
            value = None
            aggregate = operation["aggregate"]
            for match in compiled.finditer(text):
                samples += 1
                try:
                    current = float(match.group(1).replace(",", ""))
                except (AttributeError, ValueError, IndexError):
                    valid = False
                    break
                if not math.isfinite(current):
                    valid = False
                    break
                if aggregate == "last":
                    value = current
                elif aggregate == "mean":
                    total += current
                elif aggregate == "min":
                    value = current if value is None else min(value, current)
                else:
                    value = current if value is None else max(value, current)
            if aggregate == "mean" and samples:
                value = total / samples
            result.update(samples=samples, valid=bool(samples) and valid and
                          value is not None and math.isfinite(value), value=value)
        else:
            raise ValueError("unknown regex operation")
    except (re.error, ValueError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    results.append(result)
print(PREFIX + base64.b64encode(json.dumps({"results": results}).encode()).decode())
'''


WORKER_SOURCE = r'''
import base64, ctypes, fcntl, hashlib, json, os, signal, stat, subprocess, sys, tempfile, time
payload = json.loads(base64.b64decode(sys.argv[1]))
marker = payload["result_marker"]
argv = payload["argv"]
env = os.environ.copy()
env.update({str(k): str(v) for k, v in payload.get("env", {}).items()})
log_path = payload.get("log_path")
log = None
log_lock = None
capture = None
proc = None
cleanup_ok = True
started = time.time()
rc = 125
result = {"rc": 125, "timed_out": False, "elapsed_s": 0,
          "output_bytes": 0, "output_truncated": False}
def verify_artifacts(items):
    for item in items:
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0))
        fd = os.open(item["path"], flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("artifact is not a regular file: " + item["path"])
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=False) as source:
                for chunk in iter(lambda: source.read(1048576), b""):
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"].lower():
                raise RuntimeError("artifact sha256 changed before execution: " + item["path"])
        finally:
            os.close(fd)
def regular_or_absent(path, label):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise RuntimeError(label + " must be absent or a regular file")
def open_lock(path):
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("lock path is not a regular file")
    return os.fdopen(fd, "a+", encoding="utf-8")
def open_log(path, mode):
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    flags |= os.O_TRUNC if mode == "w" else os.O_APPEND
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("log path is not a regular file")
    return os.fdopen(fd, "w+b" if mode == "w" else "a+b", buffering=0)
def child_setup():
    os.setsid()
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except (AttributeError, OSError):
        pass
    if os.getppid() == 1:
        os._exit(125)
def group_alive(pgid):
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/" + entry + "/stat", encoding="utf-8").read()
            tail = raw[raw.rfind(")") + 2:].split()
            if len(tail) >= 3 and tail[0] != "Z" and int(tail[2]) == pgid:
                return True
        except (OSError, ValueError):
            pass
    return False
def stop_child(graceful=True):
    if proc is None or not group_alive(proc.pid):
        return True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and group_alive(proc.pid):
        proc.poll()
        time.sleep(.05)
    if group_alive(proc.pid):
        if graceful:
            return False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and group_alive(proc.pid):
            proc.poll()
            time.sleep(.05)
    try:
        proc.wait(timeout=.1)
    except subprocess.TimeoutExpired:
        pass
    return not group_alive(proc.pid)
def interrupted(sig, _frame):
    stop_child(graceful=False)
    raise SystemExit(128 + sig)
try:
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        regular_or_absent(log_path, "log path")
        log_lock = open_lock(log_path + ".lock")
        fcntl.flock(log_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        regular_or_absent(log_path, "log path")
        if payload.get("backup_existing") and os.path.exists(log_path):
            stamp = time.strftime("%Y%m%dT%H%M%S")
            backup = log_path + ".backup." + stamp
            index = 0
            base = backup
            while os.path.exists(backup):
                index += 1
                backup = base + "." + str(index)
            os.replace(log_path, backup)
        log = open_log(log_path, payload.get("log_mode", "w"))
    else:
        capture = tempfile.TemporaryFile()
    verify_artifacts(payload.get("artifacts", []))
    proc = subprocess.Popen(argv, cwd=payload.get("cwd") or None, env=env,
                            stdout=log if log else capture, stderr=subprocess.STDOUT,
                            preexec_fn=child_setup)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    timed_out = False
    output = ""
    try:
        proc.wait(timeout=payload["timeout_s"])
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_child(graceful=False)
    limit = payload.get("max_output_bytes", 64 * 1024 * 1024)
    if log:
        log.flush()
        size = os.fstat(log.fileno()).st_size
        os.lseek(log.fileno(), 0, os.SEEK_SET)
        raw = log.read(limit + 1)
        log.close()
        log = None
    else:
        capture.flush()
        capture.seek(0, os.SEEK_END)
        size = capture.tell()
        capture.seek(0)
        raw = capture.read(limit + 1)
        capture.close()
        capture = None
    truncated = size > limit
    output = raw[:limit].decode("utf-8", "replace")
    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    rc = 124 if timed_out else proc.returncode
    result = {"rc": rc, "timed_out": timed_out, "elapsed_s": time.time() - started,
              "output_bytes": size, "output_truncated": truncated}
except Exception as exc:
    message = type(exc).__name__ + ": " + str(exc)
    result = {"rc": 125, "timed_out": False, "elapsed_s": time.time() - started,
              "output_bytes": 0, "output_truncated": False, "worker_error": message}
    rc = 125
finally:
    cleanup_ok = stop_child(graceful=False)
    if log:
        log.close()
    if capture:
        capture.close()
    if log_lock:
        log_lock.close()
if not cleanup_ok:
    result["rc"] = 125
    result["cleanup_error"] = "owned test process group could not be reaped"
    rc = 125
print(marker + base64.b64encode(json.dumps(result).encode()).decode(), flush=True)
sys.exit(0 if rc == 0 else rc if 0 < rc < 126 else 1)
'''


ARTIFACT_VERIFY_SOURCE = r'''
import base64, hashlib, json, os, stat, sys
items = json.loads(base64.b64decode(sys.argv[1]))
for item in items:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0))
    fd = os.open(item["path"], flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("artifact is not a regular file: " + item["path"])
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(1048576), b""):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"].lower():
            raise RuntimeError("artifact sha256 changed before execution: " + item["path"])
    finally:
        os.close(fd)
'''


SUPERVISOR_SOURCE = r'''
import base64, ctypes, fcntl, hashlib, json, os, signal, stat, subprocess, sys, time
payload = json.loads(base64.b64decode(sys.argv[1]))
launch_deadline = time.monotonic() + payload["activation_timeout_s"]
pid_path = payload["pid_file"]
lock_path = pid_path + ".lock"
log_path = payload["log_path"]
MAGIC = "ascend-service-workflow-v1"
def start_time(pid):
    try:
        raw = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (OSError, IndexError):
        return None
def cmd_hash(pid):
    try:
        return hashlib.sha256(open(f"/proc/{pid}/cmdline", "rb").read()).hexdigest()
    except OSError:
        return None
def boot_id():
    try:
        return open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
    except OSError:
        return "unknown"
def matches(pid, expected_start, expected_hash):
    return (isinstance(pid, int) and start_time(pid) is not None and
            start_time(pid) == expected_start and cmd_hash(pid) == expected_hash)
def atomic_state(value):
    temp = pid_path + ".tmp." + str(os.getpid()) + "." + os.urandom(6).hex()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(value, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, pid_path)
        directory_fd = os.open(os.path.dirname(pid_path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
def safe_unlink(run_id):
    try:
        current = json.load(open(pid_path, encoding="utf-8"))
        if current.get("run_id") == run_id and current.get("pid") == os.getpid():
            os.unlink(pid_path)
    except (OSError, ValueError):
        pass
def child_setup():
    os.setsid()
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except (AttributeError, OSError):
        pass
    if os.getppid() == 1:
        os._exit(125)
def verify_artifacts(items):
    encoded = base64.b64encode(json.dumps(items).encode()).decode()
    verifier = subprocess.Popen([sys.executable, "-I", "-c", payload["artifact_verifier_source"], encoded],
                                stdout=log, stderr=subprocess.STDOUT, preexec_fn=child_setup)
    control["child"] = verifier
    state.update(phase="VERIFYING_ARTIFACTS", child_pid=verifier.pid,
                 child_start_time=start_time(verifier.pid), child_cmdline_sha256=cmd_hash(verifier.pid))
    atomic_state(state)
    while verifier.poll() is None and not control["stop"] and time.monotonic() < launch_deadline:
        time.sleep(.05)
    if verifier.poll() is None:
        if cleanup_group(verifier):
            control["child"] = None
    else:
        control["child"] = None
    if control["stop"]:
        raise InterruptedError("artifact verification interrupted by stop")
    if time.monotonic() >= launch_deadline:
        raise TimeoutError("artifact verification exceeded startup deadline")
    if verifier.returncode != 0:
        raise RuntimeError("artifact verification failed")
def regular_or_absent(path, label):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise RuntimeError(label + " must be absent or a regular file")
def open_lock(path):
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("lock path is not a regular file")
    return os.fdopen(fd, "a+", encoding="utf-8")
def group_alive(pgid):
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/" + entry + "/stat", encoding="utf-8").read()
            tail = raw[raw.rfind(")") + 2:].split()
            if len(tail) >= 3 and tail[0] != "Z" and int(tail[2]) == pgid:
                return True
        except (OSError, ValueError):
            pass
    return False
def cleanup_group(child):
    if child is None or not group_alive(child.pid):
        return True
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and group_alive(child.pid):
        child.poll()
        time.sleep(.05)
    if group_alive(child.pid):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and group_alive(child.pid):
            child.poll()
            time.sleep(.05)
    return not group_alive(child.pid)
os.makedirs(os.path.dirname(pid_path), exist_ok=True)
os.makedirs(os.path.dirname(log_path), exist_ok=True)
lock_handle = open_lock(lock_path)
try:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(73)
try:
    old = json.load(open(pid_path, encoding="utf-8"))
except (OSError, ValueError):
    old = {}
if old:
    old_pid = old.get("pid")
    child_pid = old.get("child_pid")
    if old.get("magic") == MAGIC and old.get("boot_id") == boot_id() and (
            matches(old_pid, old.get("start_time"), old.get("cmdline_sha256")) or
            isinstance(old_pid, int) and start_time(old_pid) is not None or
            matches(child_pid, old.get("child_start_time"), old.get("child_cmdline_sha256")) or
            isinstance(child_pid, int) and
            (start_time(child_pid) is not None or group_alive(child_pid))):
        sys.exit(73)
regular_or_absent(log_path, "log path")
if payload.get("backup_existing_log", True) and os.path.exists(log_path):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = log_path + ".backup." + stamp
    index = 0
    base = backup
    while os.path.exists(backup):
        index += 1
        backup = base + "." + str(index)
    os.replace(log_path, backup)
flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) |
         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
log_fd = os.open(log_path, flags, 0o600)
if not stat.S_ISREG(os.fstat(log_fd).st_mode):
    os.close(log_fd)
    raise RuntimeError("log path is not a regular file")
log = os.fdopen(log_fd, "a", encoding="utf-8", errors="replace", buffering=1)
sys.stdout = log
sys.stderr = log
env = os.environ.copy()
env.update({str(k): str(v) for k, v in payload.get("env", {}).items()})
self_pid = os.getpid()
state = {"magic": MAGIC, "deployment": payload["deployment"], "service": payload["service"],
         "run_id": payload["run_id"], "spec_sha256": payload["spec_sha256"],
         "pid": self_pid, "start_time": start_time(self_pid),
         "cmdline_sha256": cmd_hash(self_pid), "boot_id": boot_id(), "phase": "INITIALIZING",
         "child_pid": None, "child_start_time": None, "child_cmdline_sha256": None,
         "created_at": time.time()}
atomic_state(state)
control = {"child": None, "stop": False}
def forward(sig, _frame):
    control["stop"] = True
    child = control["child"]
    if child is not None and group_alive(child.pid):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
signal.signal(signal.SIGHUP, forward)
activation_path = payload["activation_path"]
activation_token = payload["activation_token"]
def activation_ready():
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(activation_path, flags)
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("activation path is not a regular file")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise RuntimeError("activation token is too large")
        return raw.decode("utf-8") == activation_token
    finally:
        os.close(fd)
def unlink_activation():
    try:
        if activation_ready():
            os.unlink(activation_path)
    except (OSError, UnicodeError):
        pass
child = None
rc = 143 if control["stop"] else 1
try:
    if not control["stop"]:
        state.update(phase="AWAITING_ACTIVATION")
        atomic_state(state)
        activated = activation_ready()
        while not control["stop"] and not activated and time.monotonic() < launch_deadline:
            time.sleep(.05)
            activated = activation_ready()
        if activated:
            unlink_activation()
        elif not control["stop"]:
            print("activation was not committed before timeout", file=sys.stderr)
            rc = 124
        if activated and not control["stop"]:
            verify_artifacts(payload.get("artifacts", []))
            if time.monotonic() >= launch_deadline:
                raise TimeoutError("startup deadline exhausted before exec")
            child = subprocess.Popen(payload["argv"], cwd=payload.get("cwd") or None, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, preexec_fn=child_setup)
            control["child"] = child
            state.update(phase="RUNNING", child_pid=child.pid, child_start_time=start_time(child.pid),
                         child_cmdline_sha256=cmd_hash(child.pid))
            atomic_state(state)
            if control["stop"] and group_alive(child.pid):
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            rc = child.wait()
finally:
    cleanup_ok = cleanup_group(child)
    auxiliary = control["child"]
    if auxiliary is not None and auxiliary is not child:
        cleanup_ok = cleanup_group(auxiliary) and cleanup_ok
    unlink_activation()
    if cleanup_ok:
        safe_unlink(payload["run_id"])
    else:
        print("owned service process group could not be reaped; preserving state", file=sys.stderr)
        rc = 125
    lock_handle.close()
    log.close()
sys.exit(rc if 0 <= rc < 126 else 1)
'''


STOPPER_SOURCE = r'''
import base64, fcntl, hashlib, json, os, signal, stat, sys, time
payload = json.loads(base64.b64decode(sys.argv[1]))
path = payload["pid_file"]
lock_path = path + ".lock"
MAGIC = "ascend-service-workflow-v1"
def start_time(pid):
    try:
        raw = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (OSError, IndexError):
        return None
def cmd_hash(pid):
    try:
        return hashlib.sha256(open(f"/proc/{pid}/cmdline", "rb").read()).hexdigest()
    except OSError:
        return None
def boot_id():
    try:
        return open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
    except OSError:
        return "unknown"
def matches(pid, expected_start, expected_hash):
    return (isinstance(pid, int) and start_time(pid) is not None and
            start_time(pid) == expected_start and cmd_hash(pid) == expected_hash)
def child_matches(pid, expected_start):
    try:
        return (isinstance(pid, int) and start_time(pid) == expected_start and
                os.getpgid(pid) == pid)
    except ProcessLookupError:
        return False
def active_group(pgid):
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/" + entry + "/stat", encoding="utf-8").read()
            tail = raw[raw.rfind(")") + 2:].split()
            if len(tail) >= 3 and tail[0] != "Z" and int(tail[2]) == pgid:
                return True
        except (OSError, ValueError):
            pass
    return False
def load_state():
    return json.load(open(path, encoding="utf-8"))
def same_identity(left, right):
    return all(left.get(key) == right.get(key) for key in ("magic", "deployment", "service", "run_id", "pid"))
def safe_unlink(expected):
    try:
        if same_identity(load_state(), expected):
            os.unlink(path)
            return True
        return False
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
def validate_state(state):
    if (state.get("magic") != MAGIC or state.get("deployment") != payload["deployment"] or
            state.get("service") != payload["service"]):
        return 5
    if payload.get("expected_run_id") is not None and state.get("run_id") != payload["expected_run_id"]:
        return 7
    return 0
def open_lock():
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(lock_path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("owner lock is not a regular file")
    return os.fdopen(fd, "a+", encoding="utf-8")
def acquire_lock(deadline):
    handle = open_lock()
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(.05)
def finish_orphan(state):
    child_pid = state.get("child_pid")
    if child_matches(child_pid, state.get("child_start_time")):
        try:
            if os.getpgid(child_pid) != child_pid:
                print("owned child is not its recorded process-group leader; refusing", file=sys.stderr)
                return 6
            os.killpg(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + payload["timeout_s"]
        while time.monotonic() < deadline and active_group(child_pid):
            time.sleep(.05)
        if active_group(child_pid):
            print("owned orphan process group did not exit before grace timeout", file=sys.stderr)
            return 124
    elif isinstance(child_pid, int) and active_group(child_pid):
        print("recorded child leader is gone or mismatched but its process group is active; preserving state", file=sys.stderr)
        return 6
    elif isinstance(child_pid, int) and start_time(child_pid) is not None:
        print("child PID ownership mismatch; preserving state and refusing", file=sys.stderr)
        return 6
    if not safe_unlink(state):
        print("could not remove unchanged ownership state; refusing success", file=sys.stderr)
        return 6
    return 0
prelock = None
if not os.path.isfile(path):
    if not os.path.isdir(os.path.dirname(lock_path)):
        sys.exit(3)
    prelock = open_lock()
    try:
        fcntl.flock(prelock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("owner lock is held while service state is initializing", file=sys.stderr)
        sys.exit(8)
    if not os.path.isfile(path):
        sys.exit(3)
try:
    state = load_state()
except (OSError, ValueError):
    sys.exit(4)
pid = state.get("pid")
state_error = validate_state(state)
if state_error:
    if state_error == 7:
        print("run_id mismatch; refusing rollback signal", file=sys.stderr)
    sys.exit(state_error)
if state.get("boot_id") != boot_id():
    lock_handle = prelock or open_lock()
    if prelock is None:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("owner lock is held while reboot-stale state is changing; refusing", file=sys.stderr)
            sys.exit(6)
    try:
        current = load_state()
    except (OSError, ValueError):
        sys.exit(4)
    if current.get("boot_id") != boot_id() and same_identity(current, state):
        sys.exit(0 if safe_unlink(state) else 6)
    print("state changed during reboot-stale cleanup; refusing", file=sys.stderr)
    sys.exit(6)
if matches(pid, state.get("start_time"), state.get("cmdline_sha256")):
    os.kill(pid, signal.SIGTERM)
else:
    expected_state = state
    lock_handle = prelock or acquire_lock(time.monotonic() + payload["timeout_s"])
    if lock_handle is None:
        print("owner lock remained held while state owner was changing", file=sys.stderr)
        sys.exit(124)
    try:
        state = load_state()
    except (OSError, ValueError):
        sys.exit(4)
    if not same_identity(state, expected_state):
        print("state changed while acquiring owner lock; refusing", file=sys.stderr)
        sys.exit(6)
    state_error = validate_state(state)
    if state_error:
        print("state changed while acquiring owner lock; refusing", file=sys.stderr)
        sys.exit(state_error)
    sys.exit(finish_orphan(state))
deadline = time.monotonic() + payload["timeout_s"]
while time.monotonic() < deadline and matches(pid, state.get("start_time"), state.get("cmdline_sha256")):
    time.sleep(.05)
if matches(pid, state.get("start_time"), state.get("cmdline_sha256")):
    print("owned supervisor did not exit before grace timeout; force kill is not automatic", file=sys.stderr)
    sys.exit(124)
lock_handle = acquire_lock(deadline)
if lock_handle is None:
    print("owned supervisor exited but its owner lock was not released", file=sys.stderr)
    sys.exit(124)
if not os.path.isfile(path):
    sys.exit(0)
try:
    current = load_state()
except (OSError, ValueError):
    sys.exit(4)
if not same_identity(current, state) or validate_state(current):
    print("state changed after supervisor exit; refusing cleanup", file=sys.stderr)
    sys.exit(6)
sys.exit(finish_orphan(current))
'''


LOG_SCAN_PREFIX = "ASCEND_WORKFLOW_LOG_SCAN "
LOG_SCAN_SOURCE = r'''
import base64, json, os, re, stat, sys
PREFIX = "ASCEND_WORKFLOW_LOG_SCAN "
payload = json.loads(base64.b64decode(sys.argv[1]))
path = payload["path"]
result = {"path": path, "status": "FAIL", "error_matches": {}, "fatal_matches": {}, "fatal": False}
try:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("log path is not a regular file")
    result["size_at_open"] = os.fstat(fd).st_size
    with os.fdopen(fd, "rb") as source:
        raw = source.read(payload["max_bytes"] + 1)
    result["bytes_read"] = len(raw)
    if len(raw) > payload["max_bytes"]:
        result["reason"] = "log exceeds configured scan limit"
        result["fatal"] = True
        result["reason_category"] = "SCAN_INCOMPLETE"
    else:
        text = raw.decode("utf-8", "replace")
        result["error_matches"] = {pattern: sum(1 for _ in re.finditer(pattern, text, re.MULTILINE))
                                   for pattern in payload["error_patterns"]}
        result["fatal_matches"] = {pattern: sum(1 for _ in re.finditer(pattern, text, re.MULTILINE))
                                   for pattern in payload["fatal_patterns"]}
        result["fatal"] = any(result["fatal_matches"].values())
        result["status"] = "PASS" if not any(result["error_matches"].values()) and not result["fatal"] else "FAIL"
        if result["fatal"]:
            result["reason_category"] = "FATAL_SERVICE_ERROR"
        elif result["status"] == "FAIL":
            result["reason_category"] = "SERVICE_ERROR"
except OSError as exc:
    result["reason"] = type(exc).__name__ + ": " + str(exc)
    result["fatal"] = True
    result["reason_category"] = "SCAN_INCOMPLETE"
print(PREFIX + base64.b64encode(json.dumps(result).encode()).decode())
sys.exit(0 if result["status"] == "PASS" else 1)
'''


OWNER_PREFIX = "ASCEND_WORKFLOW_OWNER "
OWNER_CHECK_SOURCE = r'''
import base64, fcntl, hashlib, json, os, stat, sys
PREFIX = "ASCEND_WORKFLOW_OWNER "
payload = json.loads(base64.b64decode(sys.argv[1]))
path = payload["pid_file"]
def start_time(pid):
    try:
        raw = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (OSError, IndexError):
        return None
def cmd_hash(pid):
    try:
        return hashlib.sha256(open(f"/proc/{pid}/cmdline", "rb").read()).hexdigest()
    except OSError:
        return None
def boot_id():
    try:
        return open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
    except OSError:
        return "unknown"
def matches(pid, expected_start, expected_hash):
    return (isinstance(pid, int) and start_time(pid) is not None and
            start_time(pid) == expected_start and cmd_hash(pid) == expected_hash)
def child_matches(pid, expected_start):
    try:
        return (isinstance(pid, int) and start_time(pid) == expected_start and
                os.getpgid(pid) == pid)
    except ProcessLookupError:
        return False
def active_group(pgid):
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/" + entry + "/stat", encoding="utf-8").read()
            tail = raw[raw.rfind(")") + 2:].split()
            if len(tail) >= 3 and tail[0] != "Z" and int(tail[2]) == pgid:
                return True
        except (OSError, ValueError):
            pass
    return False
def open_lock():
    lock_path = path + ".lock"
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(lock_path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("owner lock is not a regular file")
    return os.fdopen(fd, "a+", encoding="utf-8")
result = None
if not os.path.exists(path):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        result = {"state": "ABSENT"}
    else:
        lock = open_lock()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = {"state": "INITIALIZING", "reason": "owner lock is held before state publication"}
        else:
            if not os.path.exists(path):
                result = {"state": "ABSENT"}
if result is None:
    try:
        state = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result = {"state": "INVALID", "reason": type(exc).__name__ + ": " + str(exc)}
    else:
        pid = state.get("pid")
        if state.get("magic") != "ascend-service-workflow-v1" or state.get("deployment") != payload["deployment"] or state.get("service") != payload["service"]:
            result = {"state": "MISMATCH"}
        elif state.get("spec_sha256") != payload["spec_sha256"]:
            result = {"state": "SPEC_MISMATCH", "run_id": state.get("run_id"),
                      "expected_spec_sha256": payload["spec_sha256"],
                      "actual_spec_sha256": state.get("spec_sha256")}
        elif state.get("boot_id") != boot_id():
            result = {"state": "STALE", "run_id": state.get("run_id"), "reason": "boot id differs"}
        elif matches(pid, state.get("start_time"), state.get("cmdline_sha256")):
            phase = state.get("phase")
            child_pid = state.get("child_pid")
            if phase == "RUNNING":
                if child_matches(child_pid, state.get("child_start_time")) and active_group(child_pid):
                    result = {"state": "RUNNING", "run_id": state.get("run_id"),
                              "pid": pid, "child_pid": child_pid, "phase": phase}
                elif isinstance(child_pid, int) and active_group(child_pid):
                    result = {"state": "CHILD_GROUP_UNVERIFIED", "run_id": state.get("run_id"),
                              "pid": pid, "child_pid": child_pid, "phase": phase}
                elif isinstance(child_pid, int) and start_time(child_pid) is not None:
                    result = {"state": "CHILD_MISMATCH", "run_id": state.get("run_id"),
                              "pid": pid, "child_pid": child_pid, "phase": phase}
                else:
                    result = {"state": "CHILD_NOT_RUNNING", "run_id": state.get("run_id"),
                              "pid": pid, "child_pid": child_pid, "phase": phase}
            elif phase == "AWAITING_ACTIVATION":
                result = {"state": "AWAITING_ACTIVATION", "run_id": state.get("run_id"),
                          "pid": pid, "phase": phase}
            elif phase in {"INITIALIZING", "VERIFYING_ARTIFACTS"}:
                result = {"state": "INITIALIZING_OWNED", "run_id": state.get("run_id"),
                          "pid": pid, "phase": phase}
            else:
                result = {"state": "INVALID", "run_id": state.get("run_id"),
                          "pid": pid, "reason": "unknown supervisor phase"}
        elif isinstance(pid, int) and start_time(pid) is not None:
            result = {"state": "MISMATCH", "reason": "live supervisor PID ownership fields differ"}
        else:
            child_pid = state.get("child_pid")
            if (state.get("phase") == "RUNNING" and
                    child_matches(child_pid, state.get("child_start_time")) and active_group(child_pid)):
                result = {"state": "ORPHANED_OWNED", "run_id": state.get("run_id"),
                          "pid": child_pid, "reason": "supervisor exited but owned child is alive"}
            elif isinstance(child_pid, int) and active_group(child_pid):
                result = {"state": "ORPHANED_UNVERIFIED_GROUP", "run_id": state.get("run_id"),
                          "pid": child_pid,
                          "reason": "recorded child leader is gone or mismatched but its process group is active"}
            elif isinstance(child_pid, int) and start_time(child_pid) is not None:
                result = {"state": "MISMATCH", "reason": "live child PID ownership fields differ"}
            else:
                result = {"state": "STALE", "run_id": state.get("run_id")}
print(PREFIX + base64.b64encode(json.dumps(result).encode()).decode())
'''


ACTIVATE_SOURCE = r'''
import base64, json, os, stat, sys
payload = json.loads(base64.b64decode(sys.argv[1]))
path = payload["path"]
token = payload["token"].encode("utf-8")
flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) |
         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
fd = os.open(path, flags, 0o600)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise RuntimeError("activation path is not a regular file")
    os.write(fd, token)
    os.fsync(fd)
finally:
    os.close(fd)
directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
'''


def _stop_owned(proc, timeout_s=1):
    if proc.poll() is not None:
        return True
    deadline = time.monotonic() + max(.1, timeout_s)
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=max(.1, deadline - time.monotonic()))
        proc.wait(timeout=max(.05, min(.5, deadline - time.monotonic())))
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=max(.05, deadline - time.monotonic()))
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return proc.poll() is not None
    return proc.poll() is not None


def _run_local(argv, timeout_s):
    started = time.time()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
    except OSError as exc:
        return {"rc": 127, "text": str(exc), "elapsed_s": time.time() - started,
                "timed_out": False, "argv": argv}
    try:
        text, _ = proc.communicate(timeout=timeout_s)
        return {"rc": proc.returncode, "text": text, "elapsed_s": time.time() - started,
                "timed_out": False, "argv": argv}
    except subprocess.TimeoutExpired:
        cleaned = _stop_owned(proc, timeout_s=1)
        text = ""
        if cleaned:
            try:
                text, _ = proc.communicate(timeout=.2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                text = ""
        return {"rc": 124, "text": text, "elapsed_s": time.time() - started,
                "timed_out": True, "argv": argv,
                "cleanup_error": None if cleaned else "local transport process could not be reaped"}
    except BaseException:
        if not _stop_owned(proc, timeout_s=1):
            print("local transport process could not be reaped after interruption", file=sys.stderr)
        raise


def _bounded_regex(text, operations, timeout_s=5):
    """Evaluate user-provided regexes in a killable subprocess with one shared budget."""
    if not operations:
        return {"complete": True, "results": []}
    path = None
    operations_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", errors="replace",
                                         prefix="ascend-workflow-regex-", suffix=".txt",
                                         delete=False) as handle:
            handle.write(text)
            path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="ascend-workflow-regex-ops-",
                                         suffix=".json", delete=False) as handle:
            json.dump({"operations": operations}, handle)
            operations_path = handle.name
        result = _run_local([sys.executable, "-I", "-c", REGEX_EVAL_SOURCE,
                             operations_path, path], timeout_s)
        if result.get("rc") != 0 or result.get("timed_out") or result.get("cleanup_error"):
            return {"complete": False, "results": [],
                    "reason": "正则评估超时或子进程清理不完整",
                    "diagnostics": result.get("text", "")[-1000:]}
        lines = result.get("text", "").splitlines()
        if len(lines) != 1 or not lines[0].startswith(REGEX_PREFIX):
            return {"complete": False, "results": [], "reason": "缺少正则评估结果标记"}
        try:
            parsed = json.loads(base64.b64decode(lines[0][len(REGEX_PREFIX):], validate=True))
        except (ValueError, json.JSONDecodeError):
            return {"complete": False, "results": [], "reason": "正则评估结果损坏"}
        if not isinstance(parsed.get("results"), list) or len(parsed["results"]) != len(operations):
            return {"complete": False, "results": [], "reason": "正则评估结果不完整"}
        return {"complete": True, "results": parsed["results"]}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        if operations_path:
            try:
                os.unlink(operations_path)
            except OSError:
                pass


def _walk(obj, path="$config"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield path + "." + str(key), key, value
            yield from _walk(value, path + "." + str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield f"{path}[{index}]", index, value
            yield from _walk(value, f"{path}[{index}]")


def _reject_secrets(cfg):
    for path, key, value in _walk(cfg):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"配置不得包含 NaN/Infinity：{path}")
        lowered = str(key).lower()
        credential_key = re.search(
            r"(?:^|_)(?:token|secret|password|passwd|credential|credentials|api_key|access_key)(?:_|$)",
            lowered)
        if (lowered in SECRET_KEYS or credential_key or
                lowered.endswith(("_private_key", "_access_token"))):
            raise ValueError(f"配置不得保存明文凭据字段：{path}")
        if isinstance(value, str):
            if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
                raise ValueError(f"配置不得嵌入私钥正文：{path}")
            if re.search(r"(?i)(?:sshpass\s+-p|password\s*=)", value):
                raise ValueError(f"配置不得把密码拼入命令：{path}")
            if re.search(r"://[^/\s:@]+:[^/\s@]+@", value):
                raise ValueError(f"配置不得使用内嵌账号密码的 URI：{path}")


def _keys(obj, allowed, label):
    if not isinstance(obj, dict):
        raise ValueError(f"{label} 必须是对象")
    unknown = set(obj) - set(allowed)
    if unknown:
        raise ValueError(f"{label} 含未知字段：{sorted(unknown)}")


def _template_fields(value):
    if not isinstance(value, str):
        return []
    return TEMPLATE_RE.findall(value)


def _posix_abs(value, label, allow_template=False):
    if not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "\x00\r\n"):
        raise ValueError(f"{label} 必须是目标环境内的 Linux 绝对路径")
    if not allow_template and _template_fields(value):
        raise ValueError(f"{label} 仍包含未解析模板")
    if value.startswith("//") or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
        raise ValueError(f"{label} 必须是无重复分隔、`.` 或 `..` 的规范绝对路径")


def _argv(value, label, parameter_names):
    if (not isinstance(value, list) or not value or len(value) > 256 or
            any(not isinstance(x, str) or not x or len(x) > 16384 or any(c in x for c in "\x00\r\n")
                for x in value)):
        raise ValueError(f"{label} 必须是 1~256 项的非空字符串 argv 列表")
    for item in value:
        unknown = set(_template_fields(item)) - parameter_names
        if unknown:
            raise ValueError(f"{label} 使用未知模板参数：{sorted(unknown)}")


def _regex(pattern, label):
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
        raise ValueError(f"{label} 必须是长度 1~1000 的正则")
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"{label} 正则无效：{exc}") from exc
    if compiled.search("") is not None:
        raise ValueError(f"{label} 不得匹配空字符串；零宽断言不能作为验收证据")
    if pattern.replace(" ", "") in {"(?!)", "$^"}:
        raise ValueError(f"{label} 是永不匹配的空壳断言")
    return compiled


def tuning_candidates(cfg):
    tuning = cfg.get("tuning")
    if not tuning:
        return []
    _keys(tuning, {"max_trials", "parameters", "candidates", "baseline", "pinned", "objective",
                   "fatal_patterns"}, "tuning")
    max_trials = tuning.get("max_trials")
    if type(max_trials) is not int or not 1 <= max_trials <= 32:
        raise ValueError("tuning.max_trials 必须为 1~32")
    if "candidates" in tuning:
        candidates = tuning["candidates"]
        if not isinstance(candidates, list) or not candidates or len(candidates) > max_trials:
            raise ValueError("tuning.candidates 必须是 1~max_trials 项列表")
    else:
        parameters = tuning.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            raise ValueError("tuning 需要 parameters 或 candidates")
        if len(parameters) > 32:
            raise ValueError("tuning.parameters 最多允许 32 个参数")
        names = list(parameters)
        for name, values in parameters.items():
            if not ENV_RE.fullmatch(name) or name == "trial":
                raise ValueError("寻优参数名必须是普通标识符且不能为 trial")
            if not isinstance(values, list) or not values or len(values) > 16:
                raise ValueError(f"参数 {name} 候选值必须为 1~16 项")
        combination_count = math.prod(len(parameters[name]) for name in names)
        if combination_count > max_trials:
            raise ValueError(
                f"参数笛卡尔积 {combination_count} 超过 max_trials={max_trials}，不会展开或静默截断")
        candidates = [dict(zip(names, values)) for values in itertools.product(*(parameters[n] for n in names))]
    baseline = tuning.get("baseline")
    if not isinstance(baseline, dict) or not baseline:
        raise ValueError("tuning.baseline 必须明确已通过正确性验证的基线参数")
    pinned = tuning.get("pinned", {})
    if not isinstance(pinned, dict):
        raise ValueError("tuning.pinned 必须是对象")
    for key, value in pinned.items():
        if (not ENV_RE.fullmatch(key) or key == "trial" or type(value) not in (str, int, float, bool) or
                isinstance(value, str) and (len(value) > 4096 or any(c in value for c in "\x00\r\n"))):
            raise ValueError("tuning.pinned 必须是合法参数名到有限标量的映射")
    fatal_patterns = tuning.get("fatal_patterns", [])
    if not isinstance(fatal_patterns, list) or len(fatal_patterns) > 32:
        raise ValueError("tuning.fatal_patterns 必须是至多 32 项的列表")
    for pattern in fatal_patterns:
        _regex(pattern, "tuning.fatal_patterns")
    row_key = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
    baseline_key = row_key(baseline)
    candidates = [baseline, *[x for x in candidates if row_key(x) != baseline_key]]
    if len(candidates) > max_trials:
        raise ValueError(f"含基线的候选组合 {len(candidates)} 超过 max_trials={max_trials}，不会静默截断")
    normalized = []
    for item in candidates:
        if not isinstance(item, dict) or not item:
            raise ValueError("每个 tuning candidate 必须是非空对象")
        row = {}
        for key, value in item.items():
            if not ENV_RE.fullmatch(key) or key == "trial":
                raise ValueError("candidate 参数名无效")
            if type(value) not in (str, int, float, bool) or isinstance(value, float) and not (-1e100 < value < 1e100):
                raise ValueError("candidate 值仅支持有限的字符串、整数、浮点数或布尔值")
            if isinstance(value, str) and (len(value) > 4096 or any(c in value for c in "\x00\r\n")):
                raise ValueError("candidate 字符串不得包含控制字符且长度不能超过 4096")
            row[key] = value
        for key, value in pinned.items():
            if key in row and (type(row[key]) is not type(value) or row[key] != value):
                raise ValueError(f"candidate 修改了 pinned 参数 {key}")
        normalized.append(row)
    if len({row_key(x) for x in normalized}) != len(normalized):
        raise ValueError("tuning candidates 重复")
    return normalized


def _parameter_names(cfg):
    candidates = tuning_candidates(cfg)
    names = {key for row in candidates for key in row}
    if candidates and any(set(row) != names for row in candidates):
        raise ValueError("所有 tuning candidate 必须提供相同参数集合")
    return names | ({"trial"} if candidates else set())


def _validate_template_locations(cfg, parameter_names):
    allowed = re.compile(r"^\$config\.(?:services|tests)\[\d+\]\.(?:argv\[\d+\]|env\.[A-Za-z_][A-Za-z0-9_]*|log_path)$")
    behavioral = re.compile(r"^\$config\.(?:services|tests)\[\d+\]\.(?:argv\[\d+\]|env\.[A-Za-z_][A-Za-z0-9_]*)$")
    used = set()
    for path, _, value in _walk(cfg):
        if not isinstance(value, str):
            continue
        fields = set(_template_fields(value))
        if fields and (not allowed.fullmatch(path) or fields - parameter_names):
            raise ValueError(f"模板只能出现在 service/test 的 argv、env 值或 log_path：{path}")
        if fields and behavioral.fullmatch(path):
            used.update(fields)
    unused = parameter_names - {"trial"} - used
    if unused:
        raise ValueError(f"寻优参数没有用于任何 service/test 模板：{sorted(unused)}")


def _shell_script(argv):
    after_separator = False
    for item in argv[1:]:
        if item == "--" and not after_separator:
            after_separator = True
            continue
        if not after_separator and item.startswith("-"):
            if not re.fullmatch(r"-[eux]+", item):
                return None
            continue
        return item if item.startswith("/") else None
    return None


def _python_script(argv):
    allowed_flags = {"-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO"}
    after_separator = False
    for item in argv[1:]:
        if item == "--" and not after_separator:
            after_separator = True
            continue
        if not after_separator and item.startswith("-"):
            if item not in allowed_flags:
                return None
            continue
        return item if item.startswith("/") else None
    return None


def _is_python_binary(binary):
    return PYTHON_NAME_RE.fullmatch(binary.lower()) is not None


def _validate_curl_health(argv, label):
    if len(argv) < 3 or argv[1] not in {"-q", "--disable"}:
        raise ValueError(f"{label} 的 curl 必须把 -q/--disable 放在首个参数，禁用默认 curlrc")
    url_count = 0
    fail_enabled = False
    index = 2
    while index < len(argv):
        item = argv[index]
        if re.fullmatch(r"-[fsS]+", item) or item in {"--fail", "--silent", "--show-error"}:
            fail_enabled |= "f" in item or item == "--fail"
            index += 1
            continue
        if item in {"--max-time", "--connect-time"}:
            if index + 1 >= len(argv) or not re.fullmatch(r"\d+(?:\.\d+)?", argv[index + 1]):
                raise ValueError(f"{label} 的 curl 超时参数必须跟非负秒数")
            index += 2
            continue
        if re.fullmatch(r"--(?:max-time|connect-time)=\d+(?:\.\d+)?", item):
            index += 1
            continue
        if re.fullmatch(r"https?://[^\s@]+", item):
            if "?" in item or "#" in item:
                raise ValueError(f"{label} 的健康 URL 不允许 query/fragment，避免凭据进入 argv 与计划文件")
            url_count += 1
            index += 1
            continue
        raise ValueError(f"{label} 的 curl 只允许只读健康探针参数与单个 http(s) URL")
    if url_count != 1:
        raise ValueError(f"{label} 的 curl 必须且只能提供一个 http(s) URL")
    if not fail_enabled:
        raise ValueError(f"{label} 的 curl 必须启用 -f/--fail，HTTP 4xx/5xx 不能算健康")


def _safe_user_argv(value, label, parameter_names):
    _argv(value, label, parameter_names)
    binary = PurePosixPath(value[0]).name.lower()
    for item in value:
        lowered = item.lower()
        header_value = lowered
        for prefix in ("-h", "--header=", "--proxy-header="):
            if header_value.startswith(prefix):
                header_value = header_value[len(prefix):]
                break
        header_name = (header_value.split(":", 1)[0].strip().replace("_", "-")
                       if ":" in header_value else "")
        sensitive_header = (header_name in {"authorization", "cookie", "api-key", "auth-token",
                                             "access-token", "security-token", "secret"} or
                            header_name.endswith(("-authorization", "-cookie", "-api-key",
                                                  "-auth-token", "-access-token",
                                                  "-security-token", "-secret")))
        if (re.match(r"^--?[a-z0-9_-]*(?:password|passwd|secret|token|api[-_]key|access[-_]key|authorization|cookie)(?:=|$)",
                     lowered) or sensitive_header):
            raise ValueError(f"{label} 不得把凭据放入 argv；使用目标环境外部受控认证")
    if binary in {"eval", "sshpass", "killall", "pkill", "env", "nohup", "setsid", "sudo",
                  "timeout", "perl", "ruby", "node", "busybox", "toybox", "java", "lua",
                  "luajit", "pypy", "pypy3", "php", "rscript", "dotnet", "mono"}:
        raise ValueError(f"{label} 不允许高风险、模糊或无法绑定入口的命令 {binary}")
    if binary in {"bash", "sh", "zsh", "ksh", "dash", "ash", "fish", "csh", "tcsh"} and _shell_script(value) is None:
        raise ValueError(f"{label} 的 shell 只接受 -e/-u/-x 与目标环境内绝对脚本路径，不接受命令字符串")
    elif _is_python_binary(binary) and _python_script(value) is None:
        raise ValueError(f"{label} 的 Python 只接受无参数安全标志与目标环境内绝对脚本路径")
    elif binary not in {"bash", "sh", "zsh", "ksh", "dash", "ash", "fish", "csh", "tcsh"} and not _is_python_binary(binary):
        if binary != "curl" or not label.endswith(".health.argv"):
            raise ValueError(f"{label} 只支持绑定 artifact 的 shell/Python 入口；直接执行仅允许健康探针 curl")
        _validate_curl_health(value, label)


def _entrypoint_path(argv):
    binary = PurePosixPath(argv[0]).name.lower()
    if binary in {"bash", "sh", "zsh", "ksh", "dash", "ash", "fish", "csh", "tcsh"}:
        return _shell_script(argv)
    if _is_python_binary(binary):
        return _python_script(argv)
    return argv[0] if argv[0].startswith("/") else None


def _validate_artifacts(items, label):
    if not isinstance(items, list) or not items or len(items) > 64:
        raise ValueError(f"{label} 必须列出 1~64 个远端入口文件及 sha256")
    paths = set()
    for index, item in enumerate(items):
        _keys(item, {"path", "sha256"}, f"{label}[{index}]")
        _posix_abs(item.get("path"), f"{label}[{index}].path")
        if item["path"] in paths:
            raise ValueError(f"{label} 路径重复")
        paths.add(item["path"])
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256", ""))):
            raise ValueError(f"{label}[{index}].sha256 必须是 64 位十六进制")


def _version_tuple(value):
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?", str(value))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def _validate_profile(profile, node_map):
    _keys(profile, {"platform", "software", "model", "roles", "compatibility"}, "profile")
    if profile.get("platform") not in {"auto", "A2", "A3", "A5"}:
        raise ValueError("profile.platform 必须为 auto/A2/A3/A5；它是部署声明，不替代通信实测")
    software = profile.get("software", {})
    required_software = {"vllm", "vllm_ascend", "torch", "torch_npu", "cann", "hdk", "driver",
                         "image_digest"}
    _keys(software, required_software, "profile.software")
    if set(software) != required_software:
        raise ValueError("profile.software 必须完整声明 vllm/vllm_ascend/torch/torch_npu/CANN/HDK/driver/image_digest")
    if any(not isinstance(v, str) or not v or len(v) > 256 for v in software.values()):
        raise ValueError("profile.software 版本值必须是非空短字符串")
    model = profile.get("model", {})
    _keys(model, {"path", "tokenizer_path", "quantization", "max_model_len"}, "profile.model")
    for key in ("path", "tokenizer_path"):
        if key in model:
            _posix_abs(model[key], f"profile.model.{key}")
    if type(model.get("max_model_len")) is not int or model["max_model_len"] <= 0:
        raise ValueError("profile.model.max_model_len 必须是正整数")
    if not isinstance(model.get("quantization"), str) or not model["quantization"]:
        raise ValueError("profile.model.quantization 必须明确")
    compatibility = profile.get("compatibility", {})
    _keys(compatibility, {"fused_mc2_multistream_conflict", "kv_pool_min_hdk"}, "profile.compatibility")
    if set(compatibility) != {"fused_mc2_multistream_conflict", "kv_pool_min_hdk"}:
        raise ValueError("profile.compatibility 必须显式声明 fused_mc2_multistream_conflict 与 kv_pool_min_hdk")
    if "fused_mc2_multistream_conflict" in compatibility and type(compatibility["fused_mc2_multistream_conflict"]) is not bool:
        raise ValueError("fused_mc2_multistream_conflict 必须是布尔值")
    if "kv_pool_min_hdk" in compatibility and _version_tuple(compatibility["kv_pool_min_hdk"]) is None:
        raise ValueError("kv_pool_min_hdk 必须以 major.minor 开头")
    roles = profile.get("roles")
    _keys(roles, {"prefill", "decode"}, "profile.roles")
    if set(roles) != {"prefill", "decode"}:
        raise ValueError("profile.roles 必须同时明确 prefill 与 decode")
    used_devices = set()
    for role_name, role in roles.items():
        _keys(role, {"dp", "tp", "pp", "ep", "instances", "engine", "features"},
              f"profile.roles.{role_name}")
        for key in ("dp", "tp", "pp"):
            if type(role.get(key)) is not int or role[key] <= 0:
                raise ValueError(f"{role_name}.{key} 必须是正整数")
        if type(role.get("ep", 1)) is not int or role.get("ep", 1) <= 0:
            raise ValueError(f"{role_name}.ep 必须是正整数")
        engine = role.get("engine", {})
        _keys(engine, {"max_num_batched_tokens", "max_num_seqs", "gpu_memory_utilization"},
              f"profile.roles.{role_name}.engine")
        for key in ("max_num_batched_tokens", "max_num_seqs"):
            if type(engine.get(key)) is not int or engine[key] <= 0:
                raise ValueError(f"{role_name}.engine.{key} 必须是正整数")
        util = engine.get("gpu_memory_utilization")
        if type(util) not in (int, float) or not 0 < util <= 1:
            raise ValueError(f"{role_name}.engine.gpu_memory_utilization 必须在 (0,1] 内")
        features = role.get("features", {})
        _keys(features, {"prefix_cache", "kv_pool", "fused_mc2", "multistream"},
              f"profile.roles.{role_name}.features")
        if set(features) != {"prefix_cache", "kv_pool", "fused_mc2", "multistream"} or any(type(v) is not bool for v in features.values()):
            raise ValueError(f"{role_name}.features 必须显式提供四个布尔开关")
        if compatibility.get("fused_mc2_multistream_conflict") and features["fused_mc2"] and features["multistream"]:
            raise ValueError(f"{role_name} 的 fused_mc2 与 multistream 在本兼容配置中冲突")
        instances = role.get("instances")
        if not isinstance(instances, list) or len(instances) != role["dp"]:
            raise ValueError(f"{role_name}.instances 数量必须等于 DP")
        ranks = set()
        role_count = 0
        for instance in instances:
            _keys(instance, {"name", "dp_rank", "participants"}, f"{role_name}.instances[]")
            if not NAME_RE.fullmatch(str(instance.get("name", ""))):
                raise ValueError(f"{role_name} instance name 无效")
            if type(instance.get("dp_rank")) is not int:
                raise ValueError(f"{role_name} dp_rank 必须是整数")
            ranks.add(instance["dp_rank"])
            participants = instance.get("participants")
            if not isinstance(participants, list) or not participants:
                raise ValueError(f"{role_name} instance participants 不能为空")
            instance_count = 0
            for participant in participants:
                _keys(participant, {"node", "devices"}, f"{role_name}.participants[]")
                node_name = participant.get("node")
                devices = participant.get("devices")
                if node_name not in node_map or not isinstance(devices, list) or not devices or len(devices) != len(set(devices)):
                    raise ValueError(f"{role_name} participant 节点或设备列表无效")
                if any(type(x) is not int or x not in node_map[node_name]["devices"] for x in devices):
                    raise ValueError(f"{role_name} participant 使用了节点未声明的设备")
                for device in devices:
                    pair = (node_map[node_name]["ssh"], node_map[node_name].get("container"), device)
                    if pair in used_devices:
                        raise ValueError(f"执行 namespace {pair[0]}/{pair[1]} 的设备 {device} 在 P/D 实例间重复")
                    used_devices.add(pair)
                instance_count += len(devices)
            if instance_count != role["tp"] * role["pp"]:
                raise ValueError(f"{role_name} 每个 DP instance 的设备数必须等于 TP×PP")
            role_count += instance_count
        if ranks != set(range(role["dp"])) or role_count != role["dp"] * role["tp"] * role["pp"]:
            raise ValueError(f"{role_name} dp_rank 或 DP×TP×PP 设备核算失败")
        minimum = compatibility.get("kv_pool_min_hdk")
        if features["kv_pool"] and minimum:
            observed = _version_tuple(software.get("hdk"))
            if observed is None or observed < _version_tuple(minimum):
                raise ValueError(f"{role_name} 启用 kv_pool，但 HDK 未达到声明的最低版本 {minimum}")


def _validate_preflight_gate(gate):
    _keys(gate, {"report", "scope", "max_age_s", "required"}, "preflight_gate")
    if (not isinstance(gate.get("report"), str) or not gate["report"] or
            any(c in gate["report"] for c in "\x00\r\n") or not os.path.isabs(gate["report"])):
        raise ValueError("preflight_gate.report 必须是控制端报告绝对路径")
    if gate.get("scope", "primitives") not in {"primitives", "pairs", "mc2"}:
        raise ValueError("preflight_gate.scope 仅支持 primitives/pairs/mc2；部署前不能用 service gate")
    if type(gate.get("max_age_s", 3600)) is not int or not 1 <= gate.get("max_age_s", 3600) <= 86400:
        raise ValueError("preflight_gate.max_age_s 应为 1~86400")
    if type(gate.get("required", True)) is not bool:
        raise ValueError("preflight_gate.required 必须是布尔值")


def validate_config(cfg, executable=False, _rendered_candidate=False):
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1:
        raise ValueError("schema_version 必须为 1")
    _keys(cfg, {"schema_version", "deployment", "example", "nodes", "services", "tests", "tuning",
                "profile", "preflight_gate"}, "$config")
    if not NAME_RE.fullmatch(str(cfg.get("deployment", ""))):
        raise ValueError("deployment 应为 1~64 位字母、数字、点、下划线或连字符")
    if "example" in cfg and type(cfg["example"]) is not bool:
        raise ValueError("example 必须是布尔值")
    if executable and cfg.get("example") is True:
        raise ValueError("示例配置不能执行；请复制、填写现场值并移除 example=true")
    if "profile" not in cfg:
        raise ValueError("服务工作流必须提供 profile，不能只凭 argv 猜测拓扑和版本")
    _reject_secrets(cfg)
    parameter_names = _parameter_names(cfg)
    _validate_template_locations(cfg, parameter_names)
    nodes = cfg.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 64:
        raise ValueError("nodes 必须包含 1~64 个节点")
    node_names = set()
    for node in nodes:
        _keys(node, {"name", "ssh", "ssh_port", "identity_file", "container", "container_user",
                     "workdir", "python", "platform", "devices"}, "nodes[]")
        if not isinstance(node, dict) or not NAME_RE.fullmatch(str(node.get("name", ""))):
            raise ValueError("节点需要合法 name")
        if node["name"] in node_names:
            raise ValueError("节点名重复")
        node_names.add(node["name"])
        if not SSH_RE.fullmatch(str(node.get("ssh", ""))):
            raise ValueError(f"节点 {node['name']} 的 ssh 目标无效")
        if "container" in node and not NAME_RE.fullmatch(str(node["container"])):
            raise ValueError(f"节点 {node['name']} 的 container 名称无效")
        if "workdir" in node:
            _posix_abs(node["workdir"], f"nodes.{node['name']}.workdir")
        if "container_user" in node and ("container" not in node or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?", str(node["container_user"]))):
            raise ValueError("container_user 仅用于容器且格式必须为 user[:group]")
        if "identity_file" in node and (not isinstance(node["identity_file"], str) or
                not node["identity_file"] or any(c in node["identity_file"] for c in "\x00\r\n") or
                not os.path.isabs(node["identity_file"])):
            raise ValueError("identity_file 必须是控制端已有私钥的绝对路径，不接受私钥正文或相对路径")
        if type(node.get("ssh_port", 22)) is not int or not 1 <= node.get("ssh_port", 22) <= 65535:
            raise ValueError("ssh_port 应为 1~65535")
        python_cmd = node.get("python", "python3")
        if (not isinstance(python_cmd, str) or not python_cmd or len(python_cmd) > 4096 or
                any(c in python_cmd for c in "\x00\r\n")):
            raise ValueError("python 必须是长度受限且不含控制字符的目标 Python 3 可执行名或路径")
        if "/" in python_cmd:
            _posix_abs(python_cmd, f"nodes.{node['name']}.python")
        if not _is_python_binary(PurePosixPath(python_cmd).name):
            raise ValueError("node.python 仅接受 python、python3 或 python3.X 可执行名/绝对路径")
        if node.get("platform", "auto") not in {"auto", "A2", "A3", "A5"}:
            raise ValueError("node.platform 必须为 auto/A2/A3/A5")
        devices = node.get("devices")
        if not isinstance(devices, list) or len(devices) != len(set(devices)) or any(
                type(x) is not int or not 0 <= x < 64 for x in devices):
            raise ValueError("node.devices 必须是非重复的 0~63 整数列表；纯 proxy/store 节点可为空")
        if executable and node["ssh"] == "local" and os.name != "posix":
            raise ValueError("ssh=local 的 supervisor 依赖 Linux /proc/fcntl；Windows 控制端请使用 SSH 目标")
    node_map = {x["name"]: x for x in nodes}
    def namespace_path(node_name, path):
        node = node_map[node_name]
        return (node["ssh"], node.get("container"), path)
    def possible_log_keys(node_name, path):
        rows = tuning_candidates(cfg) or [{}]
        keys = set()
        for index, row in enumerate(rows, 1):
            values = dict(row, trial=index)
            rendered = TEMPLATE_RE.sub(lambda match: str(values[match.group(1)]), path)
            keys.add(namespace_path(node_name, rendered))
        return keys
    if "profile" in cfg:
        _validate_profile(cfg["profile"], node_map)
        declared = cfg["profile"]["platform"]
        if declared != "auto" and any(x.get("platform", "auto") not in {"auto", declared} for x in nodes):
            raise ValueError("节点平台声明与 profile.platform 冲突")
        if cfg.get("tuning"):
            pinned = cfg["tuning"].get("pinned", {})
            if "gpu_memory_utilization" in pinned and any(
                    role["engine"]["gpu_memory_utilization"] != pinned["gpu_memory_utilization"]
                    for role in cfg["profile"]["roles"].values()):
                raise ValueError("pinned.gpu_memory_utilization 必须与 P/D profile 基线一致")
            baseline = cfg["tuning"]["baseline"]
            for key in ("max_num_batched_tokens", "max_num_seqs"):
                if key in baseline and cfg["profile"]["roles"]["prefill"]["engine"][key] != baseline[key]:
                    raise ValueError(f"tuning.baseline.{key} 必须与 prefill profile 基线一致")
    if "preflight_gate" in cfg:
        _validate_preflight_gate(cfg["preflight_gate"])
    services = cfg.get("services")
    if not isinstance(services, list) or not services or len(services) > 64:
        raise ValueError("services 必须是 1~64 项列表")
    service_names = set()
    pid_files = set()
    service_log_paths = set()
    state_paths = set()
    artifact_paths = set()
    artifact_digests = {}
    def register_artifacts(node_name, items):
        for item in items:
            key = namespace_path(node_name, item["path"])
            digest = item["sha256"].lower()
            if key in artifact_digests and artifact_digests[key] != digest:
                raise ValueError(f"同一执行 namespace 的 artifact {item['path']} 声明了不同 sha256")
            artifact_digests[key] = digest
            artifact_paths.add(key)
    role_service_nodes = {"prefill": set(), "decode": set()}
    profile_role_nodes = {role: {participant["node"]
                                 for instance in cfg.get("profile", {}).get("roles", {}).get(role, {}).get("instances", [])
                                 for participant in instance.get("participants", [])}
                          for role in ("prefill", "decode")}
    for service in services:
        _keys(service, {"name", "node", "role", "depends_on", "argv", "workdir", "log_path",
                        "pid_file", "ready_timeout_s", "stop_timeout_s", "backup_existing_log",
                        "log_scan_max_bytes", "error_patterns", "fatal_patterns", "env", "health", "artifacts"},
              "services[]")
        name = str(service.get("name", "")) if isinstance(service, dict) else ""
        if not NAME_RE.fullmatch(name) or name in service_names:
            raise ValueError("服务需要唯一合法 name")
        service_names.add(name)
        if service.get("node") not in node_names:
            raise ValueError(f"服务 {name} 引用了未知节点")
        _safe_user_argv(service.get("argv"), f"services.{name}.argv", parameter_names)
        if service.get("role") not in {"prefill", "decode", "proxy", "store", "auxiliary"}:
            raise ValueError(f"服务 {name} role 无效")
        if service.get("role") in role_service_nodes:
            role = service["role"]
            if profile_role_nodes[role] and service["node"] not in profile_role_nodes[role]:
                raise ValueError(f"服务 {name} 的 role={role} 与 profile participant 节点不一致")
            role_service_nodes[role].add(service["node"])
        for field in ("workdir", "log_path", "pid_file"):
            if field in service:
                _posix_abs(service[field], f"services.{name}.{field}", allow_template=field == "log_path")
                if field == "log_path" and set(_template_fields(service[field])) - parameter_names:
                    raise ValueError(f"服务 {name} log_path 使用未知模板参数")
        if "pid_file" not in service or _template_fields(service["pid_file"]):
            raise ValueError(f"服务 {name} 必须提供不含模板的绝对 pid_file")
        if service["pid_file"] in pid_files:
            raise ValueError("每个服务必须使用唯一 pid_file")
        pid_files.add(service["pid_file"])
        if "log_path" not in service:
            raise ValueError(f"服务 {name} 必须提供 log_path")
        log_keys = possible_log_keys(service["node"], service["log_path"])
        if log_keys & service_log_paths:
            raise ValueError("同一节点/容器内的每个服务必须使用唯一 log_path")
        service_log_paths.update(log_keys)
        for state_path in (service["pid_file"], service["pid_file"] + ".lock"):
            state_key = namespace_path(service["node"], state_path)
            if state_key in state_paths:
                raise ValueError("同一节点/容器内的服务 PID/lock 路径必须唯一")
            state_paths.add(state_key)
        if type(service.get("backup_existing_log", True)) is not bool:
            raise ValueError("backup_existing_log 必须是布尔值")
        if type(service.get("log_scan_max_bytes", 128 * 1024 * 1024)) is not int or not 1024 <= service.get(
                "log_scan_max_bytes", 128 * 1024 * 1024) <= 1024 * 1024 * 1024:
            raise ValueError("log_scan_max_bytes 应为 1KiB~1GiB")
        error_patterns = service.get("error_patterns")
        if not isinstance(error_patterns, list) or not error_patterns or len(error_patterns) > 64:
            raise ValueError(f"服务 {name} 必须提供 1~64 个 error_patterns")
        for pattern in error_patterns:
            _regex(pattern, f"services.{name}.error_patterns")
        fatal_patterns = service.get("fatal_patterns", [])
        if not isinstance(fatal_patterns, list) or len(fatal_patterns) > 64:
            raise ValueError(f"服务 {name} 的 fatal_patterns 必须是至多 64 项的列表")
        for pattern in fatal_patterns:
            _regex(pattern, f"services.{name}.fatal_patterns")
        _validate_artifacts(service.get("artifacts"), f"services.{name}.artifacts")
        register_artifacts(service["node"], service["artifacts"])
        entrypoint = _entrypoint_path(service["argv"])
        if entrypoint is None:
            raise ValueError(f"服务 {name} 必须使用绝对 executable，或 shell/python 加绝对脚本入口")
        if entrypoint not in {item["path"] for item in service["artifacts"]}:
            raise ValueError(f"服务 {name} 的实际入口 {entrypoint} 必须列入 artifacts")
        if type(service.get("ready_timeout_s", 600)) is not int or not 5 <= service.get("ready_timeout_s", 600) <= 7200:
            raise ValueError("ready_timeout_s 应为 5~7200")
        if type(service.get("stop_timeout_s", 60)) is not int or not 1 <= service.get("stop_timeout_s", 60) <= 600:
            raise ValueError("stop_timeout_s 应为 1~600")
        env = service.get("env", {})
        if (not isinstance(env, dict) or len(env) > 256 or
                any(not ENV_RE.fullmatch(str(k)) for k in env)):
            raise ValueError(f"服务 {name} env 键无效")
        if set(env) & DANGEROUS_ENV_KEYS:
            raise ValueError(f"服务 {name} env 不得覆盖 loader、解释器或 PATH 注入变量")
        for value in env.values():
            if type(value) not in (str, int, float, bool):
                raise ValueError(f"服务 {name} env 值必须是标量")
            if isinstance(value, str) and (len(value) > 65536 or any(c in value for c in "\x00\r\n")):
                raise ValueError(f"服务 {name} env 字符串不得包含控制字符且长度不能超过 65536")
            if isinstance(value, str) and set(_template_fields(value)) - parameter_names:
                raise ValueError(f"服务 {name} env 使用未知模板")
        health = service.get("health")
        if not isinstance(health, dict):
            raise ValueError(f"服务 {name} 必须提供 health 命令")
        _keys(health, {"argv", "timeout_s", "expected_exit_code", "down_exit_codes", "required_regex",
                       "artifacts"},
              f"services.{name}.health")
        _safe_user_argv(health.get("argv"), f"services.{name}.health.argv", parameter_names)
        _validate_artifacts(health.get("artifacts"), f"services.{name}.health.artifacts")
        register_artifacts(service["node"], health["artifacts"])
        health_entrypoint = _entrypoint_path(health["argv"])
        if health_entrypoint is None or health_entrypoint not in {
                item["path"] for item in health["artifacts"]}:
            raise ValueError(f"服务 {name} 的健康探针实际入口必须列入 health.artifacts")
        if type(health.get("timeout_s", 10)) is not int or not 1 <= health.get("timeout_s", 10) <= 120:
            raise ValueError("health.timeout_s 应为 1~120")
        if health.get("expected_exit_code", 0) != 0:
            raise ValueError("health.expected_exit_code 必须为 0；失败命令不能被定义成健康")
        down_exit_codes = health.get("down_exit_codes")
        if (not isinstance(down_exit_codes, list) or not down_exit_codes or
                len(down_exit_codes) != len(set(down_exit_codes)) or
                any(type(code) is not int or not 1 <= code <= 124 for code in down_exit_codes)):
            raise ValueError("health.down_exit_codes 必须明确列出 1~124 的非重复非健康返回码；125 保留给控制面错误")
        if "required_regex" in health:
            _regex(health["required_regex"], "health.required_regex")
    for role, required_nodes in profile_role_nodes.items():
        missing = required_nodes - role_service_nodes[role]
        if missing:
            raise ValueError(f"profile 中 {role} participant 缺少同角色服务：{sorted(missing)}")
    for service in services:
        deps = service.get("depends_on", [])
        if not isinstance(deps, list) or len(deps) != len(set(deps)) or service["name"] in deps or not set(deps) <= service_names:
            raise ValueError(f"服务 {service['name']} 的 depends_on 无效")
    service_waves(services)
    tests = cfg.get("tests", [])
    if not isinstance(tests, list) or len(tests) > 64:
        raise ValueError("tests 必须为至多 64 项")
    test_names = set()
    test_log_paths = set()
    for test in tests:
        _keys(test, {"name", "node", "argv", "workdir", "log_path", "log_mode", "backup_existing",
                     "timeout_s", "env", "assertions", "metrics", "artifacts"}, "tests[]")
        name = str(test.get("name", "")) if isinstance(test, dict) else ""
        if not NAME_RE.fullmatch(name) or name in test_names:
            raise ValueError("测试需要唯一合法 name")
        test_names.add(name)
        if test.get("node") not in node_names:
            raise ValueError(f"测试 {name} 引用了未知节点")
        _safe_user_argv(test.get("argv"), f"tests.{name}.argv", parameter_names)
        if "workdir" in test:
            _posix_abs(test["workdir"], f"tests.{name}.workdir")
        _posix_abs(test.get("log_path"), f"tests.{name}.log_path", allow_template=True)
        if set(_template_fields(test["log_path"])) - parameter_names:
            raise ValueError(f"测试 {name} log_path 使用未知模板参数")
        log_keys = possible_log_keys(test["node"], test["log_path"])
        if log_keys & (service_log_paths | test_log_paths):
            raise ValueError("同一节点/容器内的服务和测试必须使用互不冲突的 log_path")
        test_log_paths.update(log_keys)
        if test.get("log_mode", "w") != "w":
            raise ValueError("test.log_mode 仅支持 w；append 会让历史成功文本污染本轮验收")
        if type(test.get("backup_existing", True)) is not bool:
            raise ValueError("test.backup_existing 必须是布尔值")
        _validate_artifacts(test.get("artifacts"), f"tests.{name}.artifacts")
        register_artifacts(test["node"], test["artifacts"])
        entrypoint = _entrypoint_path(test["argv"])
        if entrypoint is None:
            raise ValueError(f"测试 {name} 必须使用绝对 executable，或 shell/python 加绝对脚本入口")
        if entrypoint not in {item["path"] for item in test["artifacts"]}:
            raise ValueError(f"测试 {name} 的实际入口 {entrypoint} 必须列入 artifacts")
        if type(test.get("timeout_s", 3600)) is not int or not 1 <= test.get("timeout_s", 3600) <= 86400:
            raise ValueError("test.timeout_s 应为 1~86400")
        test_env = test.get("env", {})
        if (not isinstance(test_env, dict) or len(test_env) > 256 or
                any(not ENV_RE.fullmatch(str(k)) for k in test_env) or any(
                type(v) not in (str, int, float, bool) for v in test_env.values())):
            raise ValueError(f"测试 {name} env 必须是标识符到标量的映射")
        if set(test_env) & DANGEROUS_ENV_KEYS:
            raise ValueError(f"测试 {name} env 不得覆盖 loader、解释器或 PATH 注入变量")
        if any(isinstance(value, str) and (len(value) > 65536 or any(c in value for c in "\x00\r\n"))
               for value in test_env.values()):
            raise ValueError(f"测试 {name} env 字符串不得包含控制字符且长度不能超过 65536")
        assertions = test.get("assertions", {})
        if not isinstance(assertions, dict):
            raise ValueError("assertions 必须是对象")
        _keys(assertions, {"exit_code", "must_match", "must_not_match", "counts"},
              f"tests.{name}.assertions")
        if assertions.get("exit_code", 0) != 0:
            raise ValueError("assertions.exit_code 必须为 0；超时或失败进程不能被定义成测试成功")
        for field in ("must_match", "must_not_match"):
            values = assertions.get(field, [])
            if not isinstance(values, list) or len(values) > 64:
                raise ValueError(f"assertions.{field} 必须是至多 64 项的列表")
            for pattern in values:
                _regex(pattern, f"assertions.{field}")
        counts = assertions.get("counts", [])
        if not isinstance(counts, list) or len(counts) > 64:
            raise ValueError("assertions.counts 必须是至多 64 项的列表")
        for count in counts:
            if not isinstance(count, dict) or "pattern" not in count or set(count) - {"pattern", "equals", "min", "max"}:
                raise ValueError("count 只支持 pattern/equals/min/max")
            _regex(count["pattern"], "assertions.counts.pattern")
            if not any(k in count for k in ("equals", "min", "max")):
                raise ValueError("count 至少需要 equals/min/max 之一")
            if any(type(count[k]) is not int or count[k] < 0 for k in ("equals", "min", "max") if k in count):
                raise ValueError("count 边界必须是非负整数")
        positive_count = any(count.get("equals", count.get("min", 0)) >= 1 for count in counts)
        negative_count = any(count.get("equals") == 0 or count.get("max") == 0 for count in counts)
        if (not assertions.get("must_not_match") and not negative_count or
                not assertions.get("must_match") and not positive_count):
            raise ValueError(f"测试 {name} 必须同时声明成功证据（must_match/counts）和失败拒绝模式（must_not_match）")
        metrics = test.get("metrics", {})
        if not isinstance(metrics, dict) or len(metrics) > 64:
            raise ValueError("metrics 必须是至多 64 项的对象")
        for metric, rule in metrics.items():
            _keys(rule, {"pattern", "aggregate"}, f"tests.{name}.metrics.{metric}")
            if not NAME_RE.fullmatch(metric) or not isinstance(rule, dict) or set(rule) - {"pattern", "aggregate"}:
                raise ValueError("metric 需要合法名称及 pattern/aggregate")
            compiled = _regex(rule.get("pattern"), f"metrics.{metric}.pattern")
            if compiled.groups != 1:
                raise ValueError("metric pattern 必须且只能有一个捕获组")
            if rule.get("aggregate", "last") not in {"last", "mean", "min", "max"}:
                raise ValueError("metric aggregate 仅支持 last/mean/min/max")
    writable_logs = service_log_paths | test_log_paths
    log_lock_paths = {(ssh, container, path + ".lock") for ssh, container, path in writable_logs}
    if (len(log_lock_paths) != len(writable_logs) or log_lock_paths & writable_logs or
            (writable_logs | log_lock_paths) & (state_paths | artifact_paths) or
            state_paths & artifact_paths):
        raise ValueError("同一节点/容器内的 log/lock、PID/lock 与 artifact 路径不得冲突")
    if cfg.get("tuning"):
        objective = cfg["tuning"].get("objective")
        _keys(objective, {"test", "metric", "direction"}, "tuning.objective")
        if not isinstance(objective, dict) or objective.get("test") not in test_names:
            raise ValueError("tuning.objective.test 必须引用现有测试")
        target = next(x for x in tests if x["name"] == objective["test"])
        if objective.get("metric") not in target.get("metrics", {}):
            raise ValueError("tuning.objective.metric 必须引用该测试的 metric")
        if objective.get("direction") not in {"maximize", "minimize"}:
            raise ValueError("tuning.objective.direction 必须为 maximize/minimize")
    if cfg.get("tuning") and not _rendered_candidate:
        for index, values in enumerate(tuning_candidates(cfg), 1):
            rendered = render_config(cfg, dict(values, trial=index))
            rendered.pop("tuning")
            try:
                validate_config(rendered, executable=executable, _rendered_candidate=True)
            except ValueError as exc:
                raise ValueError(f"tuning trial {index} 渲染后配置无效：{exc}") from exc
    if executable:
        _validate_controller_command_lengths(cfg)
    return cfg


def service_waves(services):
    remaining = {x["name"]: set(x.get("depends_on", [])) for x in services}
    order = [x["name"] for x in services]
    waves = []
    done = set()
    while remaining:
        ready = [name for name in order if name in remaining and remaining[name] <= done]
        if not ready:
            raise ValueError("服务 depends_on 存在环")
        waves.append(ready)
        done.update(ready)
        for name in ready:
            del remaining[name]
    return waves


def _canonical_identity_file(node):
    return str(Path(node["identity_file"]).resolve()) if node.get("identity_file") else None


def _ssh_prefix(node):
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3"]
    if node.get("identity_file"):
        argv += ["-o", "IdentitiesOnly=yes", "-i", _canonical_identity_file(node)]
    if node.get("ssh_port", 22) != 22:
        argv += ["-p", str(node["ssh_port"])]
    return argv + ["--", node["ssh"]]


def context_argv(node, runtime, detached=False):
    if node.get("container"):
        command = ["docker", "exec", "-d" if detached else "-i"]
        if node.get("container_user"):
            command += ["--user", node["container_user"]]
        command += [node["container"], *runtime]
    else:
        command = runtime
    if node["ssh"] == "local":
        return command
    return _ssh_prefix(node) + [shlex.join(command)]


def detached_context_argv(node, runtime):
    if node.get("container"):
        return context_argv(node, runtime, detached=True)
    if node["ssh"] == "local":
        return runtime
    host_runtime = ["bash", "-lc", "nohup " + shlex.join(runtime) +
                    " </dev/null >/dev/null 2>&1 &"]
    return context_argv(node, host_runtime)


def service_spec_sha256(cfg, node, service):
    node_context = {key: node.get(key) for key in
                    ("name", "ssh", "ssh_port", "container", "container_user", "workdir", "python")}
    value = {"deployment": cfg["deployment"], "profile": cfg["profile"],
             "workflow_impl_sha256": workflow_impl_sha256(),
             "node_context": node_context, "service": service}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def activation_path(service, run_id):
    suffix = hashlib.sha256(run_id.encode()).hexdigest()
    return service["pid_file"] + ".activate." + suffix


def supervisor_payload(cfg, node, service, run_id):
    return {"deployment": cfg["deployment"], "service": service["name"], "run_id": run_id,
            "argv": service["argv"], "env": service.get("env", {}),
            "cwd": service.get("workdir", node.get("workdir")),
            "pid_file": service["pid_file"], "log_path": service["log_path"],
            "backup_existing_log": service.get("backup_existing_log", True),
            "artifacts": service["artifacts"],
            "activation_path": activation_path(service, run_id),
            "activation_token": run_id,
            "activation_timeout_s": service.get("ready_timeout_s", 600),
            "artifact_verifier_source": ARTIFACT_VERIFY_SOURCE,
            "spec_sha256": service_spec_sha256(cfg, node, service)}


def supervisor_runtime(cfg, node, service, run_id):
    payload = base64.b64encode(json.dumps(supervisor_payload(cfg, node, service, run_id)).encode()).decode()
    return [node.get("python", "python3"), "-u", "-c", SUPERVISOR_SOURCE, payload]


def worker_runtime(node, argv, timeout_s, *, env=None, cwd=None, log_path=None, log_mode="w",
                   backup_existing=False, artifacts=None, marker_token=None):
    result_marker = WORKER_PREFIX + (marker_token or secrets.token_hex(16)) + " "
    payload = {"argv": argv, "timeout_s": timeout_s, "env": env or {},
               "cwd": cwd, "log_path": log_path, "log_mode": log_mode,
               "backup_existing": backup_existing, "result_marker": result_marker,
               "artifacts": artifacts or []}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    runtime = [node.get("python", "python3"), "-u", "-c", WORKER_SOURCE, encoded]
    return runtime, result_marker


def _validate_controller_command_lengths(cfg, limit=30000):
    """Fail before remote action when a Windows CreateProcess command could exceed 32K."""
    nodes = _node_map(cfg)
    run_id = cfg["deployment"] + "-20000101T000000-" + "0" * 32

    def check(node, runtime, label, detached=False):
        command = detached_context_argv(node, runtime) if detached else context_argv(node, runtime)
        size = len(subprocess.list2cmdline([str(item) for item in command])) + 1
        if size > limit:
            raise ValueError(
                f"{label} 的控制端命令行预计 {size} 字符，超过 Windows 安全上限 {limit}；"
                "缩短 argv/env/path/pattern 或改用受控配置文件")

    def check_worker(node, argv, timeout_s, label, **kwargs):
        runtime, _ = worker_runtime(node, argv, timeout_s, marker_token="0" * 32, **kwargs)
        check(node, runtime, label)

    hash_source = ("import hashlib,os,stat,sys\n"
                   "h=hashlib.sha256()\n"
                   "flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)\n"
                   "fd=os.open(sys.argv[1],flags)\n"
                   "if not stat.S_ISREG(os.fstat(fd).st_mode): raise RuntimeError('not a regular file')\n"
                   "with os.fdopen(fd,'rb') as source:\n"
                   "    for chunk in iter(lambda:source.read(1048576),b''):\n"
                   "        h.update(chunk)\n"
                   "print(h.hexdigest())\n")
    for service in cfg["services"]:
        node = nodes[service["node"]]
        check(node, supervisor_runtime(cfg, node, service, run_id),
              f"services.{service['name']} supervisor", detached=True)
        health = service["health"]
        check_worker(node, health["argv"], health.get("timeout_s", 10),
                     f"services.{service['name']} health", env={},
                     cwd=service.get("workdir", node.get("workdir")),
                     artifacts=health["artifacts"])
        owner_payload = {"deployment": cfg["deployment"], "service": service["name"],
                         "pid_file": service["pid_file"], "spec_sha256": "0" * 64}
        owner_encoded = base64.b64encode(json.dumps(owner_payload).encode()).decode()
        check_worker(node, [node.get("python", "python3"), "-u", "-c",
                            OWNER_CHECK_SOURCE, owner_encoded], 30,
                     f"services.{service['name']} owner check", cwd="/")
        stop_payload = {"deployment": cfg["deployment"], "service": service["name"],
                        "pid_file": service["pid_file"],
                        "timeout_s": service.get("stop_timeout_s", 60),
                        "expected_run_id": run_id}
        stop_encoded = base64.b64encode(json.dumps(stop_payload).encode()).decode()
        check_worker(node, [node.get("python", "python3"), "-u", "-c",
                            STOPPER_SOURCE, stop_encoded], service.get("stop_timeout_s", 60) + 5,
                     f"services.{service['name']} stop", cwd="/")
        activate_payload = {"path": activation_path(service, run_id), "token": run_id}
        activate_encoded = base64.b64encode(json.dumps(activate_payload).encode()).decode()
        check_worker(node, [node.get("python", "python3"), "-u", "-c",
                            ACTIVATE_SOURCE, activate_encoded], 30,
                     f"services.{service['name']} activate", cwd="/")
        scan_payload = {"path": service["log_path"],
                        "error_patterns": service["error_patterns"],
                        "fatal_patterns": service.get("fatal_patterns", []),
                        "max_bytes": service.get("log_scan_max_bytes", 128 * 1024 * 1024)}
        scan_encoded = base64.b64encode(json.dumps(scan_payload).encode()).decode()
        check_worker(node, [node.get("python", "python3"), "-u", "-c",
                            LOG_SCAN_SOURCE, scan_encoded], 120,
                     f"services.{service['name']} log scan", cwd="/")
        for artifact in service["artifacts"] + health["artifacts"]:
            check_worker(node, [node.get("python", "python3"), "-c", hash_source,
                                artifact["path"]], 60,
                         f"services.{service['name']} artifact check", cwd="/")
    for test in cfg.get("tests", []):
        node = nodes[test["node"]]
        check_worker(node, test["argv"], test.get("timeout_s", 3600),
                     f"tests.{test['name']}", env=test.get("env", {}),
                     cwd=test.get("workdir", node.get("workdir")), log_path=test["log_path"],
                     log_mode=test.get("log_mode", "w"),
                     backup_existing=test.get("backup_existing", True),
                     artifacts=test["artifacts"])


class RemoteExecutor:
    def run(self, node, argv, timeout_s, env=None, cwd=None, log_path=None, log_mode="w",
            backup_existing=False, artifacts=None, total_timeout_s=None):
        if total_timeout_s is not None and total_timeout_s <= 0:
            return {"rc": 124, "text": "", "elapsed_s": 0, "timed_out": True,
                    "transport_error": "调用前总截止时间已耗尽"}
        worker_timeout = timeout_s
        if total_timeout_s is not None:
            worker_timeout = min(timeout_s, max(.05, total_timeout_s - .1))
        effective_cwd = cwd if cwd is not None else node.get("workdir")
        runtime, result_marker = worker_runtime(
            node, argv, worker_timeout, env=env, cwd=effective_cwd, log_path=log_path,
            log_mode=log_mode, backup_existing=backup_existing, artifacts=artifacts)
        transport_timeout = worker_timeout + 30 if total_timeout_s is None else max(.05, total_timeout_s)
        result = _run_local(context_argv(node, runtime), transport_timeout)
        lines = result["text"].splitlines(keepends=True)
        marker = None
        if lines and lines[-1].startswith(result_marker):
            try:
                marker = json.loads(base64.b64decode(lines[-1][len(result_marker):].strip(),
                                                      validate=True))
            except (ValueError, json.JSONDecodeError):
                marker = None
        visible = lines[:-1] if marker is not None else lines
        if marker is None:
            return {"rc": 125, "text": "".join(visible), "elapsed_s": result["elapsed_s"],
                    "timed_out": result["timed_out"], "transport_error": "缺少远端监督结果标记"}
        expected_transport_rc = 0 if marker.get("rc") == 0 else marker.get("rc") \
            if isinstance(marker.get("rc"), int) and 0 < marker["rc"] < 126 else 1
        if result["rc"] != expected_transport_rc:
            return {"rc": 125, "text": "".join(visible), "elapsed_s": result["elapsed_s"],
                    "timed_out": result["timed_out"],
                    "transport_error": "远端退出码与监督结果不一致"}
        marker.update(text="".join(visible), transport_rc=result["rc"])
        return marker

    def detach(self, cfg, node, service, run_id, total_timeout_s=None):
        if total_timeout_s is not None and total_timeout_s <= 0:
            return {"rc": 124, "text": "", "elapsed_s": 0, "timed_out": True,
                    "transport_error": "detach 前 readiness 总截止时间已耗尽", "argv": []}
        runtime = supervisor_runtime(cfg, node, service, run_id)
        if node["ssh"] == "local" and not node.get("container"):
            try:
                subprocess.Popen(runtime, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return {"rc": 0, "text": "", "elapsed_s": 0, "timed_out": False, "argv": runtime}
            except OSError as exc:
                return {"rc": 127, "text": str(exc), "elapsed_s": 0, "timed_out": False, "argv": runtime}
        else:
            argv = detached_context_argv(node, runtime)
        return _run_local(argv, 30 if total_timeout_s is None else max(.05, total_timeout_s))

    def activate(self, node, service, run_id, total_timeout_s=None):
        payload = {"path": activation_path(service, run_id), "token": run_id}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        return self.run(node, [node.get("python", "python3"), "-u", "-c",
                               ACTIVATE_SOURCE, encoded], 30,
                        cwd="/",
                        total_timeout_s=total_timeout_s)

    def stop(self, cfg, node, service, expected_run_id=None):
        payload = {"deployment": cfg["deployment"], "service": service["name"],
                   "pid_file": service["pid_file"], "timeout_s": service.get("stop_timeout_s", 60),
                   "expected_run_id": expected_run_id}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        return self.run(node, [node.get("python", "python3"), "-u", "-c", STOPPER_SOURCE, encoded],
                        service.get("stop_timeout_s", 60) + 5,
                        cwd="/")


def _node_map(cfg):
    return {x["name"]: x for x in cfg["nodes"]}


def _service_map(cfg):
    return {x["name"]: x for x in cfg["services"]}


def verify_artifacts(executor, node, artifacts):
    checks = []
    code = ("import hashlib,os,stat,sys\n"
            "h=hashlib.sha256()\n"
            "flags=os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)\n"
            "fd=os.open(sys.argv[1],flags)\n"
            "if not stat.S_ISREG(os.fstat(fd).st_mode): raise RuntimeError('not a regular file')\n"
            "with os.fdopen(fd,'rb') as source:\n"
            "    for chunk in iter(lambda:source.read(1048576),b''):\n"
            "        h.update(chunk)\n"
            "print(h.hexdigest())\n")
    for artifact in artifacts:
        result = executor.run(node, [node.get("python", "python3"), "-c", code, artifact["path"]],
                              60, cwd="/")
        actual = result.get("text", "").strip().splitlines()[-1] if result.get("text", "").strip() else None
        ok = result["rc"] == 0 and actual == artifact["sha256"].lower()
        checks.append({"path": artifact["path"], "expected_sha256": artifact["sha256"].lower(),
                       "actual_sha256": actual, "status": "PASS" if ok else "FAIL",
                       "diagnostics": result.get("text", "")[-1000:]})
    return {"status": "PASS" if all(x["status"] == "PASS" for x in checks) else "FAIL", "checks": checks}


def preflight_evidence(cfg):
    gate_cfg = cfg.get("preflight_gate")
    if not gate_cfg:
        return {"status": "UNVERIFIED", "required": False,
                "reason": "配置未绑定通信预检报告；服务工作流不会把缺失证据记为 PASS"}
    path = Path(gate_cfg["report"])
    base = {"required": gate_cfg.get("required", True),
            "scope": gate_cfg.get("scope", "primitives"), "path": str(path.resolve())}
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
        from preflight import gate
        ok = gate(report, gate_cfg.get("scope", "primitives"), gate_cfg.get("max_age_s", 3600))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**base, "status": "UNAVAILABLE",
                "reason": "通信预检报告不可读、不合法或无法判定：" + type(exc).__name__}
    return {"status": "PASS" if ok else "FAIL", "required": gate_cfg.get("required", True),
            "scope": gate_cfg.get("scope", "primitives"), "path": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(), "report_time": report.get("time")}


def scan_service_logs(cfg, executor=None, names=None):
    executor = executor or RemoteExecutor()
    nodes = _node_map(cfg)
    report = {"status": "PASS", "services": {}}
    selected = set(names) if names is not None else {service["name"] for service in cfg["services"]}
    for service in cfg["services"]:
        if service["name"] not in selected:
            continue
        payload = {"path": service["log_path"], "error_patterns": service["error_patterns"],
                   "fatal_patterns": service.get("fatal_patterns", []),
                   "max_bytes": service.get("log_scan_max_bytes", 128 * 1024 * 1024)}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        result = executor.run(nodes[service["node"]],
                              [nodes[service["node"]].get("python", "python3"), "-u", "-c",
                               LOG_SCAN_SOURCE, encoded], 120, cwd="/")
        parsed = None
        for line in result.get("text", "").splitlines():
            if line.startswith(LOG_SCAN_PREFIX):
                try:
                    parsed = json.loads(base64.b64decode(line[len(LOG_SCAN_PREFIX):]))
                except (ValueError, json.JSONDecodeError):
                    pass
        if parsed is None:
            parsed = {"status": "FAIL", "path": service["log_path"],
                      "fatal": True, "reason_category": "SCAN_INCOMPLETE",
                      "reason": "缺少结构化日志扫描结果", "diagnostics": result.get("text", "")[-2000:]}
        expected_rc = 0 if parsed.get("status") == "PASS" else 1
        envelope_error = (result.get("rc") != expected_rc or result.get("timed_out") or
                          result.get("transport_error") or result.get("worker_error") or
                          result.get("cleanup_error") or result.get("output_truncated"))
        if envelope_error:
            parsed.update(status="FAIL", fatal=True, reason_category="SCAN_INCOMPLETE",
                          reason=(result.get("transport_error") or result.get("worker_error") or
                                  result.get("cleanup_error") or "日志扫描结果信封不完整或退出码不一致"),
                          diagnostics=result.get("text", "")[-2000:])
        report["services"][service["name"]] = parsed
        if parsed.get("status") != "PASS":
            report["status"] = "FAIL"
    if not report["services"]:
        report["status"] = "UNVERIFIED"
    return report


def ownership_status(cfg, executor, node, service, total_timeout_s=None):
    payload = {"deployment": cfg["deployment"], "service": service["name"],
               "pid_file": service["pid_file"],
               "spec_sha256": service_spec_sha256(cfg, node, service)}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    command_timeout = 30 if total_timeout_s is None else min(30, max(.05, total_timeout_s - .1))
    result = executor.run(node, [node.get("python", "python3"), "-u", "-c",
                                 OWNER_CHECK_SOURCE, encoded], command_timeout,
                          cwd="/",
                          total_timeout_s=total_timeout_s)
    if result.get("rc") != 0 or result.get("timed_out") or result.get("transport_error"):
        return {"state": "INVALID", "reason": result.get("transport_error") or "所有权检查命令失败",
                "diagnostics": result.get("text", "")[-2000:]}
    parsed = None
    for line in result.get("text", "").splitlines():
        if line.startswith(OWNER_PREFIX):
            try:
                parsed = json.loads(base64.b64decode(line[len(OWNER_PREFIX):], validate=True))
            except (ValueError, json.JSONDecodeError):
                pass
    if parsed is None:
        return {"state": "INVALID", "reason": "缺少结构化所有权检查结果",
                "diagnostics": result.get("text", "")[-2000:]}
    return parsed


def wait_run_ownership(cfg, executor, node, service, run_id, timeout_s=5, *, deadline=None,
                       return_any_owned=False):
    deadline = time.monotonic() + timeout_s if deadline is None else deadline
    last = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        last = ownership_status(cfg, executor, node, service, total_timeout_s=remaining)
        owned_states = {"RUNNING", "ORPHANED_OWNED", "AWAITING_ACTIVATION",
                        "INITIALIZING_OWNED"}
        if last.get("state") in owned_states:
            if last.get("run_id") != run_id:
                return last
            if last.get("state") != "INITIALIZING_OWNED" or return_any_owned:
                return last
        if last.get("state") in {"MISMATCH", "SPEC_MISMATCH", "CHILD_GROUP_UNVERIFIED",
                                 "CHILD_MISMATCH", "CHILD_NOT_RUNNING",
                                 "ORPHANED_UNVERIFIED_GROUP"}:
            return last
        time.sleep(min(.25, max(.01, deadline - time.monotonic())))
    return last or {"state": "INVALID", "reason": "所有权确认超时"}


def health_once(executor, node, service, total_timeout_s=None):
    spec = service["health"]
    local_deadline = (time.monotonic() + total_timeout_s
                      if total_timeout_s is not None else None)
    command_timeout = (spec.get("timeout_s", 10) if total_timeout_s is None else
                       min(spec.get("timeout_s", 10), max(.05, total_timeout_s - .1)))
    result = executor.run(node, spec["argv"], command_timeout,
                          cwd=service.get("workdir", node.get("workdir")),
                          artifacts=spec["artifacts"], total_timeout_s=total_timeout_s)
    observable = not any(result.get(key) for key in
                         ("timed_out", "transport_error", "worker_error", "cleanup_error",
                          "output_truncated"))
    ok = result["rc"] == spec.get("expected_exit_code", 0) and observable
    regex_evaluation = None
    if spec.get("required_regex"):
        remaining = (REGEX_TIMEOUT_S if local_deadline is None else
                     max(0, local_deadline - time.monotonic()))
        if remaining <= 0:
            regex_evaluation = {"complete": False, "results": [],
                                "reason": "健康探针远端调用已耗尽 readiness 总预算"}
        else:
            regex_evaluation = _bounded_regex(
                result.get("text", ""),
                [{"id": "health", "kind": "search", "pattern": spec["required_regex"]}],
                min(REGEX_TIMEOUT_S, remaining))
        ok = (ok and regex_evaluation["complete"] and
              regex_evaluation["results"][0].get("matched", False))
    return {"status": "PASS" if ok else "FAIL", "rc": result["rc"], "observable": observable,
            "down_evidence": observable and result["rc"] in spec["down_exit_codes"],
            "elapsed_s": result.get("elapsed_s"), "regex_evaluation": regex_evaluation,
            "diagnostics": result.get("text", "")[-4000:]}


def wait_health(executor, node, service, want_up):
    deadline = time.monotonic() + (service.get("ready_timeout_s", 600) if want_up else service.get("stop_timeout_s", 60))
    last = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        last = health_once(executor, node, service, total_timeout_s=remaining)
        if want_up and last["status"] == "PASS":
            return {"status": "PASS", "observed": "UP" if want_up else "DOWN", "last": last}
        if not want_up and last.get("down_evidence"):
            return {"status": "PASS", "observed": "DOWN", "last": last}
        time.sleep(min(2, max(.1, deadline - time.monotonic())))
    return {"status": "FAIL", "expected": "UP" if want_up else "DOWN", "last": last}


def wait_ready_owned(cfg, executor, node, service, run_id, *, deadline=None):
    deadline = (time.monotonic() + service.get("ready_timeout_s", 600)
                if deadline is None else deadline)
    last_health = None
    last_owner = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        last_owner = ownership_status(cfg, executor, node, service, total_timeout_s=remaining)
        if (last_owner.get("state") in {"INITIALIZING_OWNED", "AWAITING_ACTIVATION"} and
                last_owner.get("run_id") == run_id):
            time.sleep(min(.25, max(.01, deadline - time.monotonic())))
            continue
        if last_owner.get("state") != "RUNNING" or last_owner.get("run_id") != run_id:
            return {"status": "FAIL", "expected": "UP_AND_OWNED",
                    "reason": "本轮 supervisor 所有权在 readiness 期间丢失或不匹配",
                    "last": last_health, "ownership": last_owner}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        last_health = health_once(executor, node, service, total_timeout_s=remaining)
        if last_health["status"] == "PASS":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "FAIL", "expected": "UP_AND_OWNED",
                        "reason": "健康探针通过时 readiness 总截止时间已耗尽",
                        "last": last_health, "ownership": last_owner}
            final_owner = ownership_status(cfg, executor, node, service,
                                           total_timeout_s=remaining)
            if final_owner.get("state") == "RUNNING" and final_owner.get("run_id") == run_id:
                return {"status": "PASS", "observed": "UP_AND_OWNED",
                        "last": last_health, "ownership": final_owner}
            return {"status": "FAIL", "expected": "UP_AND_OWNED",
                    "reason": "健康探针通过后本轮 supervisor 所有权丢失或不匹配",
                    "last": last_health, "ownership": final_owner}
        time.sleep(min(2, max(.1, deadline - time.monotonic())))
    return {"status": "FAIL", "expected": "UP_AND_OWNED", "last": last_health,
            "ownership": last_owner}


def health_status_services(cfg, executor=None):
    executor = executor or RemoteExecutor()
    nodes = _node_map(cfg)
    return {service["name"]: health_once(executor, nodes[service["node"]], service)
            for service in cfg["services"]}


def status_services(cfg, executor=None):
    executor = executor or RemoteExecutor()
    nodes = _node_map(cfg)
    report = {}
    for service in cfg["services"]:
        node = nodes[service["node"]]
        health = health_once(executor, node, service)
        ownership = ownership_status(cfg, executor, node, service)
        ok = health["status"] == "PASS" and ownership.get("state") == "RUNNING"
        report[service["name"]] = {"status": "PASS" if ok else "FAIL",
                                    "health": health, "ownership": ownership}
    return report


def stop_services(cfg, names=None, executor=None, expected_run_id=None):
    executor = executor or RemoteExecutor()
    nodes, services = _node_map(cfg), _service_map(cfg)
    selected = set(services) if names is None else set(names)
    report = {"status": "PASS", "services": {}}
    for wave in reversed(service_waves(cfg["services"])):
        targets = []
        for name in reversed(wave):
            if name in selected:
                service = services[name]
                targets.append((name, nodes[service["node"]], service))
        if not targets:
            continue

        # Stop calls send the ownership-scoped signal before any health probe.  Run the
        # whole dependency wave concurrently so one wedged host cannot delay signalling
        # its peers by an entire stop timeout.
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {
                name: pool.submit(executor.stop, cfg, node, service,
                                  expected_run_id=expected_run_id)
                for name, node, service in targets
            }
        active = []
        for name, node, service in targets:
            try:
                result = futures[name].result()
            except Exception as exc:
                result = {"rc": 125, "text": "", "worker_error": f"{type(exc).__name__}: {exc}",
                          "timed_out": False, "output_truncated": False}
            if result["rc"] == 3:
                evidence = health_once(
                    executor, node, service,
                    total_timeout_s=min(5, service.get("stop_timeout_s", 60)))
                if evidence.get("down_evidence"):
                    report["services"][name] = {"status": "ALREADY_STOPPED",
                                                 "health_down": evidence, "stop": result}
                    continue
                report["services"][name] = {
                    "status": "FAIL", "stop": result, "health_down": evidence,
                    "reason": "所有权状态不存在，但有界健康探针无法证明服务已停止"}
                report["status"] = "FAIL"
                continue
            item = {"status": "SIGNALLED" if result["rc"] == 0 else "FAIL",
                    "stop": result}
            report["services"][name] = item
            if result["rc"] == 0:
                active.append((name, node, service))
            else:
                report["status"] = "FAIL"
        for name, node, service in active:
            down = wait_health(executor, node, service, False)
            report["services"][name]["health_down"] = down
            report["services"][name]["status"] = "STOPPED" if down["status"] == "PASS" else "FAIL"
            if down["status"] != "PASS":
                report["status"] = "FAIL"
    return report


def _launch_services_impl(cfg, executor, progress):
    nodes, services = _node_map(cfg), _service_map(cfg)
    run_id = cfg["deployment"] + "-" + time.strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(16).hex()
    report = {"status": "PASS", "services": {}, "started_by_this_run": [], "run_id": run_id}
    progress["report"] = report
    for wave in service_waves(cfg["services"]):
        prepared = []
        for name in wave:
            progress["current_service"] = name
            service, node = services[name], nodes[services[name]["node"]]
            before = health_once(executor, node, service)
            ownership = ownership_status(cfg, executor, node, service)
            if before["status"] == "PASS":
                if ownership.get("state") == "RUNNING":
                    report["services"][name] = {"status": "ALREADY_HEALTHY", "before": before,
                                                 "ownership": ownership}
                else:
                    report["services"][name] = {
                        "status": "FAIL", "before": before, "ownership": ownership,
                        "reason": "健康探针通过，但没有与当前 service spec 匹配的运行中 supervisor 所有权"}
                    report["status"] = "FAIL"
                continue
            if ownership.get("state") not in {"ABSENT", "STALE"}:
                report["services"][name] = {"status": "FAIL", "before": before,
                                             "ownership": ownership,
                                             "reason": "服务不健康但 PID 所有权仍在或不可确认，拒绝覆盖日志或重复启动"}
                report["status"] = "FAIL"
                continue
            artifacts = verify_artifacts(executor, node, service["artifacts"])
            if artifacts["status"] != "PASS":
                report["services"][name] = {"status": "FAIL", "before": before,
                                             "ownership": ownership, "artifacts": artifacts}
                report["status"] = "FAIL"
                continue
            backup = {"status": "DELEGATED" if service.get("backup_existing_log", True) else "SKIPPED",
                      "reason": "supervisor 获取 PID 文件排他锁后再原子备份，避免并发启动移动活跃日志"}
            ready_deadline = time.monotonic() + service.get("ready_timeout_s", 600)
            detached = executor.detach(cfg, node, service, run_id,
                                       total_timeout_s=max(0, ready_deadline - time.monotonic()))
            item = {"status": "STARTING" if detached["rc"] == 0 else "FAIL",
                    "before": before, "ownership": ownership, "artifacts": artifacts,
                    "backup": backup, "launch": detached}
            report["services"][name] = item
            if detached["rc"] == 0:
                report["started_by_this_run"].append(name)
                published = wait_run_ownership(cfg, executor, node, service, run_id,
                                               deadline=ready_deadline)
                item["ownership_started"] = published
                if (published.get("state") not in {"AWAITING_ACTIVATION", "RUNNING"} or
                        published.get("run_id") != run_id):
                    item["status"] = "FAIL"
                    item["reason"] = "supervisor 未在启动确认窗口内发布本轮所有权"
                    report["status"] = "FAIL"
                    break
                item["status"] = "AWAITING_WAVE_ACTIVATION"
                prepared.append((name, node, service, ready_deadline))
            else:
                uncertain_owner = wait_run_ownership(cfg, executor, node, service, run_id,
                                                     deadline=ready_deadline, return_any_owned=True)
                item["ownership_after_failed_detach"] = uncertain_owner
                if (uncertain_owner.get("state") in {"RUNNING", "ORPHANED_OWNED",
                                                      "AWAITING_ACTIVATION", "INITIALIZING_OWNED"} and
                        uncertain_owner.get("run_id") == run_id):
                    report["started_by_this_run"].append(name)
                    item["reason"] = "启动 ACK 失败，但发现本轮所有权；纳入 run_id 精确回滚"
                else:
                    item["reason"] = "启动结果不确定且未确认本轮所有权"
                report["status"] = "FAIL"
                break
        if report["status"] == "PASS" and prepared:
            with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
                activations = {
                    pool.submit(executor.activate, node, service, run_id,
                                total_timeout_s=max(0, ready_deadline - time.monotonic())):
                    (name, node, service, ready_deadline)
                    for name, node, service, ready_deadline in prepared
                }
                for future in as_completed(activations):
                    name, node, service, ready_deadline = activations[future]
                    try:
                        activation = future.result()
                    except Exception as exc:
                        activation = {"rc": 125, "text": "", "timed_out": False,
                                      "worker_error": f"{type(exc).__name__}: {exc}"}
                    item = report["services"][name]
                    item["activation"] = activation
                    if (activation.get("rc") != 0 or activation.get("timed_out") or
                            activation.get("transport_error") or activation.get("worker_error") or
                            activation.get("cleanup_error")):
                        item["status"] = "FAIL"
                        item["reason"] = "supervisor 激活令牌提交失败或结果不确定"
                        report["status"] = "FAIL"
                    else:
                        item["status"] = "ACTIVATED"
        if report["status"] == "PASS" and prepared:
            with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
                pending = {pool.submit(wait_ready_owned, cfg, executor, node, service, run_id,
                                       deadline=ready_deadline): (name, node, service)
                           for name, node, service, ready_deadline in prepared}
                for future in as_completed(pending):
                    name, node, service = pending[future]
                    ready = future.result()
                    owner_after = ready.get("ownership") or ownership_status(
                        cfg, executor, node, service)
                    report["services"][name]["ready"] = ready
                    report["services"][name]["ownership_after"] = owner_after
                    owns_this_run = (owner_after.get("state") == "RUNNING" and
                                     owner_after.get("run_id") == run_id)
                    report["services"][name]["status"] = (
                        "HEALTHY" if ready["status"] == "PASS" and owns_this_run else "FAIL")
                    if ready["status"] != "PASS" or not owns_this_run:
                        report["status"] = "FAIL"
        if report["status"] == "FAIL":
            report["rollback"] = stop_services(cfg, report["started_by_this_run"], executor,
                                                expected_run_id=run_id)
            break
    if report["status"] == "PASS":
        report["final_status"] = status_services(cfg, executor)
        drifted = [name for name, item in report["final_status"].items()
                   if item["status"] != "PASS" or
                   name in report["started_by_this_run"] and item["ownership"].get("run_id") != run_id]
        if drifted:
            report["status"] = "FAIL"
            report["final_status_drift"] = drifted
            report["rollback"] = stop_services(cfg, report["started_by_this_run"], executor,
                                                expected_run_id=run_id)
    return report


def launch_services(cfg, executor=None):
    executor = executor or RemoteExecutor()
    progress = {}
    try:
        return _launch_services_impl(cfg, executor, progress)
    except BaseException:
        report = progress.get("report", {})
        run_id = report.get("run_id")
        owned = set(report.get("started_by_this_run", []))
        if run_id:
            try:
                nodes = _node_map(cfg)
                candidates = set(owned)
                if progress.get("current_service"):
                    candidates.add(progress["current_service"])
                for service in cfg["services"]:
                    if service["name"] not in candidates:
                        continue
                    if service["name"] in owned:
                        continue
                    node = nodes[service["node"]]
                    status = wait_run_ownership(cfg, executor, node, service, run_id, timeout_s=1,
                                                return_any_owned=True)
                    if (status.get("state") in {"RUNNING", "ORPHANED_OWNED",
                                                "AWAITING_ACTIVATION", "INITIALIZING_OWNED"} and
                            status.get("run_id") == run_id):
                        owned.add(service["name"])
                if owned:
                    stop_services(cfg, owned, executor=executor, expected_run_id=run_id)
            except Exception as cleanup_exc:
                print("launch interruption cleanup failed: " + str(cleanup_exc), file=sys.stderr)
        raise


def evaluate_test(test, result):
    text = result.get("text", "")
    assertions = test.get("assertions", {})
    checks = []
    expected_rc = assertions.get("exit_code", 0)
    checks.append({"kind": "exit_code", "status": "PASS" if result["rc"] == expected_rc else "FAIL",
                   "expected": expected_rc, "actual": result["rc"]})
    checks.append({"kind": "not_timed_out", "status": "FAIL" if result.get("timed_out") else "PASS"})
    checks.append({"kind": "transport_complete",
                   "status": "FAIL" if result.get("transport_error") else "PASS",
                   "diagnostics": result.get("transport_error")})
    checks.append({"kind": "worker_complete",
                   "status": "FAIL" if result.get("worker_error") else "PASS",
                   "diagnostics": result.get("worker_error")})
    checks.append({"kind": "complete_output", "status": "FAIL" if result.get("output_truncated") else "PASS",
                   "output_bytes": result.get("output_bytes")})
    checks.append({"kind": "process_group_cleanup",
                   "status": "FAIL" if result.get("cleanup_error") else "PASS",
                   "diagnostics": result.get("cleanup_error")})
    operations = []
    descriptors = []
    for pattern in assertions.get("must_match", []):
        operation = {"id": len(operations), "kind": "search", "pattern": pattern}
        operations.append(operation)
        descriptors.append(("must_match", pattern, None, operation["id"]))
    for pattern in assertions.get("must_not_match", []):
        operation = {"id": len(operations), "kind": "search", "pattern": pattern}
        operations.append(operation)
        descriptors.append(("must_not_match", pattern, None, operation["id"]))
    for rule in assertions.get("counts", []):
        operation = {"id": len(operations), "kind": "count", "pattern": rule["pattern"]}
        operations.append(operation)
        descriptors.append(("count", rule["pattern"], rule, operation["id"]))
    metric_ids = {}
    for name, rule in test.get("metrics", {}).items():
        operation = {"id": len(operations), "kind": "metric", "pattern": rule["pattern"],
                     "aggregate": rule.get("aggregate", "last")}
        operations.append(operation)
        metric_ids[name] = operation["id"]
    regex_evaluation = _bounded_regex(text, operations, REGEX_TIMEOUT_S)
    regex_results = {item.get("id"): item for item in regex_evaluation.get("results", [])}
    if not regex_evaluation["complete"]:
        checks.append({"kind": "regex_evaluation", "status": "FAIL",
                       "diagnostics": regex_evaluation.get("reason")})
    for kind, pattern, rule, operation_id in descriptors:
        outcome = regex_results.get(operation_id, {})
        if kind == "must_match":
            ok = regex_evaluation["complete"] and outcome.get("matched") is True and not outcome.get("error")
            checks.append({"kind": kind, "pattern": pattern, "status": "PASS" if ok else "FAIL",
                           "diagnostics": outcome.get("error")})
        elif kind == "must_not_match":
            ok = regex_evaluation["complete"] and outcome.get("matched") is False and not outcome.get("error")
            checks.append({"kind": kind, "pattern": pattern, "status": "PASS" if ok else "FAIL",
                           "diagnostics": outcome.get("error")})
        else:
            count = outcome.get("count")
            ok = (regex_evaluation["complete"] and isinstance(count, int) and not outcome.get("error") and
                  ("equals" not in rule or count == rule["equals"]) and
                  ("min" not in rule or count >= rule["min"]) and
                  ("max" not in rule or count <= rule["max"]))
            checks.append({"kind": kind, "pattern": pattern, "status": "PASS" if ok else "FAIL",
                           "actual": count, "diagnostics": outcome.get("error"),
                           **{k: v for k, v in rule.items() if k != "pattern"}})
    metrics = {}
    for name, rule in test.get("metrics", {}).items():
        outcome = regex_results.get(metric_ids[name], {})
        valid = regex_evaluation["complete"] and outcome.get("valid") is True and not outcome.get("error")
        value = outcome.get("value")
        if valid:
            metrics[name] = value
        checks.append({"kind": "metric_present", "metric": name,
                       "status": "PASS" if valid else "FAIL", "samples": outcome.get("samples", 0),
                       "diagnostics": outcome.get("error")})
    return {"status": "PASS" if all(x["status"] == "PASS" for x in checks) else "FAIL",
            "checks": checks, "metrics": metrics, "process": {k: v for k, v in result.items() if k != "text"},
            "diagnostics_tail": text[-8000:]}


def run_tests(cfg, names=None, executor=None, expected_run_id=None):
    executor = executor or RemoteExecutor()
    nodes = _node_map(cfg)
    configured = [x["name"] for x in cfg.get("tests", [])]
    selected = set(configured if names is None else names)
    omitted = [name for name in configured if name not in selected]
    report = {"status": "PASS", "tests": {}, "selected": sorted(selected), "omitted": omitted,
              "complete_suite": not omitted and selected == set(configured)}
    report["pre_health"] = status_services(cfg, executor)
    if any(item["status"] != "PASS" for item in report["pre_health"].values()):
        report.update(status="FAIL", reason="测试前服务健康或当前规格所有权不完整",
                      service_logs={"status": "UNVERIFIED", "services": {}},
                      post_health=report["pre_health"])
        return report
    pre_run_ids = {name: item["ownership"].get("run_id")
                   for name, item in report["pre_health"].items()}
    report["pre_run_ids"] = pre_run_ids
    if (expected_run_id is not None and
            any(run_id != expected_run_id for run_id in pre_run_ids.values())):
        report.update(
            status="FAIL", reason="测试前服务 run_id 不属于本轮启动，拒绝跨实例执行测试",
            expected_run_id=expected_run_id,
            run_id_mismatch={name: run_id for name, run_id in pre_run_ids.items()
                             if run_id != expected_run_id},
            service_logs={"status": "UNVERIFIED", "services": {}},
            post_health=report["pre_health"])
        return report
    for test in cfg.get("tests", []):
        if test["name"] not in selected:
            continue
        node = nodes[test["node"]]
        artifacts = verify_artifacts(executor, node, test["artifacts"])
        if artifacts["status"] != "PASS":
            report["tests"][test["name"]] = {"status": "FAIL", "artifacts": artifacts,
                                                   "reason": "测试入口文件哈希与计划不一致"}
            report["status"] = "FAIL"
            continue
        backup = {"status": "DELEGATED" if test.get("backup_existing", True) else "SKIPPED",
                  "reason": "worker 获取结果日志排他锁后再原子备份"}
        result = executor.run(node, test["argv"], test.get("timeout_s", 3600),
                              env=test.get("env", {}), cwd=test.get("workdir", node.get("workdir")),
                              log_path=test["log_path"], log_mode=test.get("log_mode", "w"),
                              backup_existing=test.get("backup_existing", True),
                              artifacts=test["artifacts"])
        evaluated = evaluate_test(test, result)
        evaluated["artifacts"] = artifacts
        evaluated["backup"] = backup
        fatal_patterns = cfg.get("tuning", {}).get("fatal_patterns", [])
        fatal_eval = _bounded_regex(
            result.get("text", ""),
            [{"id": index, "kind": "search", "pattern": pattern}
             for index, pattern in enumerate(fatal_patterns)], REGEX_TIMEOUT_S)
        fatal = ([fatal_patterns[item["id"]] for item in fatal_eval.get("results", [])
                  if item.get("matched") and not item.get("error")]
                 if fatal_eval["complete"] else [])
        evaluated["fatal_pattern_evaluation"] = fatal_eval
        if fatal:
            evaluated["fatal_patterns_matched"] = fatal
            evaluated["status"] = "FAIL"
        regex_incomplete = (not fatal_eval["complete"] or
                            any(check["kind"] == "regex_evaluation" and check["status"] == "FAIL"
                                for check in evaluated["checks"]))
        if result.get("worker_error"):
            evaluated["fatal_for_tuning"] = True
            evaluated["reason_category"] = "TEST_CONTROL_INCOMPLETE"
            evaluated["status"] = "FAIL"
        elif result.get("cleanup_error"):
            evaluated["fatal_for_tuning"] = True
            evaluated["reason_category"] = "TEST_CLEANUP_INCOMPLETE"
            evaluated["status"] = "FAIL"
        elif regex_incomplete:
            evaluated["fatal_for_tuning"] = True
            evaluated["reason_category"] = "TEST_REGEX_EVALUATION_INCOMPLETE"
            evaluated["status"] = "FAIL"
        elif result.get("timed_out") or result.get("output_truncated") or result.get("transport_error"):
            evaluated["fatal_for_tuning"] = True
            evaluated["reason_category"] = "TEST_OUTPUT_INCOMPLETE"
            evaluated["status"] = "FAIL"
        report["tests"][test["name"]] = evaluated
        if evaluated["status"] != "PASS":
            report["status"] = "FAIL"
        if evaluated.get("fatal_for_tuning") or evaluated.get("fatal_patterns_matched"):
            remaining = [item["name"] for item in cfg.get("tests", [])
                         if item["name"] in selected and item["name"] not in report["tests"]]
            report["aborted_on_fatal"] = test["name"]
            report["omitted_due_to_fatal"] = remaining
            report["complete_suite"] = False
            break
    if not report["tests"]:
        report["status"] = "UNVERIFIED"
    report["service_logs"] = scan_service_logs(cfg, executor)
    if report["service_logs"]["status"] != "PASS":
        report["status"] = "FAIL"
    report["post_health"] = status_services(cfg, executor)
    post_run_ids = {name: item.get("ownership", {}).get("run_id")
                    for name, item in report["post_health"].items()}
    report["post_run_ids"] = post_run_ids
    ownership_drift = {
        name: {"before": run_id, "after": post_run_ids.get(name)}
        for name, run_id in pre_run_ids.items() if post_run_ids.get(name) != run_id
    }
    if ownership_drift:
        report["ownership_drift"] = ownership_drift
        report["status"] = "FAIL"
    if any(x["status"] != "PASS" for x in report["post_health"].values()):
        report["status"] = "FAIL"
    if report["status"] == "PASS" and not report["complete_suite"]:
        report["status"] = "PARTIAL"
    return report


def render_config(cfg, values):
    def render(value):
        if isinstance(value, str):
            return TEMPLATE_RE.sub(lambda match: str(values[match.group(1)])
                                   if match.group(1) in values else match.group(0), value)
        if isinstance(value, list):
            return [render(x) for x in value]
        if isinstance(value, dict):
            return {k: render(v) for k, v in value.items()}
        return value
    rendered = render(cfg)
    for path, _, value in _walk(rendered):
        if isinstance(value, str) and _template_fields(value):
            raise ValueError(f"模板未完整解析：{path}")
    return rendered


def select_best(trials, objective):
    eligible = []
    for trial in trials:
        if trial.get("status") != "PASS":
            continue
        test = trial["tests"]["tests"].get(objective["test"], {})
        value = test.get("metrics", {}).get(objective["metric"])
        if value is not None:
            eligible.append((value, trial))
    if not eligible:
        return None
    return (max if objective["direction"] == "maximize" else min)(eligible, key=lambda x: x[0])[1]


def tune(cfg, executor=None, leave_best_running=False):
    executor = executor or RemoteExecutor()
    trials = []
    abort_reason = None
    for index, values in enumerate(tuning_candidates(cfg), 1):
        rendered = render_config(cfg, dict(values, trial=index))
        rendered_tuning = rendered.pop("tuning")
        validate_config(rendered, executable=True)
        rendered["tuning"] = rendered_tuning
        stopped = stop_services(rendered, executor=executor)
        if stopped["status"] != "PASS":
            trials.append({"trial": index, "parameters": values, "status": "FAIL", "stop_before": stopped,
                           "reason": "无法确认仅停止本工具拥有的服务，终止寻优"})
            abort_reason = "STOP_BEFORE_INCOMPLETE"
            break
        launched = None
        stopped_after = None
        post_stop_logs = None
        try:
            launched = launch_services(rendered, executor)
            tested = {"status": "UNVERIFIED", "tests": {}}
            owns_all_services = (set(launched.get("started_by_this_run", [])) ==
                                 {service["name"] for service in rendered["services"]})
            if launched["status"] == "PASS" and owns_all_services:
                tested = run_tests(rendered, executor=executor,
                                   expected_run_id=launched.get("run_id"))
            elif launched["status"] != "PASS":
                attempted = [name for name, item in launched.get("services", {}).items() if "launch" in item]
                launch_logs = scan_service_logs(rendered, executor, attempted) if attempted else {
                    "status": "FAIL", "services": {"_launch": {
                        "status": "FAIL", "fatal": True, "reason_category": "LAUNCH_INCOMPLETE",
                        "reason": "没有可核验的已尝试服务日志"}}}
                control_failure = any(item.get("status") == "FAIL" and
                                      ("launch" not in item or item["launch"].get("rc") != 0)
                                      for item in launched.get("services", {}).values())
                if control_failure:
                    launch_logs["status"] = "FAIL"
                    launch_logs["services"]["_control"] = {
                        "status": "FAIL", "fatal": True, "reason_category": "CONTROL_INCOMPLETE",
                        "reason": "artifact、所有权或启动传输未完整确认"}
                tested = {"status": "FAIL", "tests": {}, "service_logs": launch_logs,
                          "reason": "启动失败；扫描已尝试服务日志后再决定是否继续候选"}
            stopped_after = stop_services(
                rendered, launched.get("started_by_this_run", []), executor=executor,
                expected_run_id=launched.get("run_id"))
            log_names = launched.get("started_by_this_run", [])
            post_stop_logs = (scan_service_logs(rendered, executor, log_names) if log_names else
                              {"status": "UNVERIFIED", "services": {},
                               "reason": "本 trial 没有由当前 run_id 启动的服务日志"})
        finally:
            if launched is not None and stopped_after is None and launched.get("started_by_this_run"):
                stop_services(rendered, launched["started_by_this_run"], executor=executor,
                              expected_run_id=launched.get("run_id"))
        status = "PASS" if (owns_all_services and launched["status"] == tested["status"] ==
                            stopped_after["status"] == post_stop_logs["status"] == "PASS") else "FAIL"
        trials.append({"trial": index, "parameters": values, "status": status, "launch": launched,
                       "tests": tested, "stop_after": stopped_after,
                       "post_stop_service_logs": post_stop_logs})
        if launched["status"] == "PASS" and not owns_all_services:
            trials[-1]["reason"] = "寻优要求本 trial 拥有全部服务；检测到既有或并发服务，拒绝测试或停止他人进程"
        fatal = (any(item.get("fatal_patterns_matched") or item.get("fatal_for_tuning")
                     for item in tested.get("tests", {}).values()) or
                 any(item.get("fatal") for item in tested.get("service_logs", {}).get("services", {}).values()) or
                 any(item.get("fatal") for item in post_stop_logs.get("services", {}).values()))
        if index == 1 and status != "PASS":
            trials[-1]["reason"] = "基线未通过，禁止继续寻优"
            abort_reason = "BASELINE_FAILED"
            break
        if fatal:
            trials[-1]["reason"] = "命中致命错误模式，停止寻优且不自动重试"
            abort_reason = "FATAL_OR_CONTROL_INCOMPLETE"
            break
        if stopped_after["status"] != "PASS":
            abort_reason = "STOP_AFTER_INCOMPLETE"
            break
    objective = cfg["tuning"]["objective"]
    best = select_best(trials, objective)
    report = {"status": "PASS" if best and abort_reason is None else "FAIL",
              "objective": objective, "trials": trials, "aborted": abort_reason is not None,
              "abort_reason": abort_reason,
              "best": {"trial": best["trial"], "parameters": best["parameters"]} if best else None}
    if leave_best_running and best and abort_reason is None:
        rendered = render_config(cfg, dict(best["parameters"], trial=best["trial"]))
        report["best_relaunch"] = launch_services(rendered, executor)
        if report["best_relaunch"]["status"] != "PASS":
            report["status"] = "FAIL"
    return report


def workflow_impl_fingerprints():
    directory = Path(__file__).resolve().parent
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("service_workflow.py", "preflight.py")}


def workflow_impl_sha256(fingerprints=None):
    fingerprints = workflow_impl_fingerprints() if fingerprints is None else fingerprints
    canonical = json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def plan(cfg):
    implementation = workflow_impl_fingerprints()
    candidates = tuning_candidates(cfg)
    previews = []
    rows = candidates or [{}]
    for index, values in enumerate(rows, 1):
        rendered = render_config(cfg, dict(values, trial=index)) if candidates else cfg
        nodes = _node_map(rendered)
        previews.append({"trial": index if candidates else None, "parameters": values,
                         "profile": rendered.get("profile"),
                         "nodes": [dict({key: node.get(key) for key in
                                         ("name", "ssh", "container", "platform", "devices")},
                                        identity_file=_canonical_identity_file(node))
                                   for node in rendered["nodes"]],
                         "waves": service_waves(rendered["services"]),
                         "service_specs": {s["name"]: {key: s.get(key) for key in
                                           ("node", "role", "depends_on", "argv", "env", "log_path",
                                            "pid_file", "health", "artifacts", "error_patterns", "fatal_patterns")}
                                           for s in rendered["services"]},
                         "launch": {s["name"]: detached_context_argv(
                                        nodes[s["node"]],
                                        supervisor_runtime(rendered, nodes[s["node"]], s, "PLAN_RUN_ID"))
                                    for s in rendered["services"]},
                         "tests": [{key: test.get(key) for key in
                                    ("name", "node", "argv", "log_path", "assertions", "metrics", "artifacts")}
                                   for test in rendered.get("tests", [])]})
    result = {"status": "PASS", "deployment": cfg["deployment"], "execution": "PLAN_ONLY", "example": cfg.get("example", False),
              "config_sha256": hashlib.sha256(json.dumps(cfg, ensure_ascii=False, sort_keys=True,
                                                           separators=(",", ":")).encode()).hexdigest(),
              "workflow_impl": implementation,
              "workflow_impl_sha256": workflow_impl_sha256(implementation),
              "preflight": preflight_evidence(cfg), "trials": previews,
              "tuning_constraints": {key: cfg.get("tuning", {}).get(key)
                                     for key in ("baseline", "pinned", "objective", "fatal_patterns")},
              "safety": ["不保存密码/私钥正文", "不使用 killall/pkill",
                         "仅校验 supervisor PID/starttime/cmdline/boot_id 后停止本工具进程",
                         "执行前复核远端入口文件 sha256", "--execute 与匹配的 plan sha 才启动、停止或压测"]}
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    result["plan_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def _same_local_path(left, right):
    left, right = Path(left), Path(right)
    if os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve())):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _control_input_paths(config_path, cfg):
    paths = [Path(config_path)]
    paths.extend(Path(node["identity_file"]) for node in cfg["nodes"] if node.get("identity_file"))
    if cfg.get("preflight_gate", {}).get("report"):
        paths.append(Path(cfg["preflight_gate"]["report"]))
    return paths


def _reserve_report(path, force=False, protected_paths=()):
    target = Path(path)
    token = secrets.token_hex(16)
    lock = target.with_name(target.name + ".lock")
    probe = target.with_name(target.name + ".probe." + token)
    temp = target.with_name(target.name + ".tmp." + token)
    error_path = target.with_name(target.name + ".error." + token + ".json")
    for candidate in (target, lock, probe, temp, error_path):
        for protected in protected_paths:
            if _same_local_path(candidate, protected):
                raise ValueError(f"报告或其控制文件不得覆盖控制端输入：{candidate}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise ValueError(f"报告目标必须是普通文件而非目录或符号链接：{target}")
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"报告正被另一任务写入或上次异常中断：{lock}") from exc
    try:
        with os.fdopen(lock_fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        probe_fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(probe_fd)
        probe.unlink()
        existed = target.exists()
        if existed and not force:
            raise ValueError(f"报告已存在：{target}；使用 --force 明确覆盖")
        if existed and force:
            writable_fd = os.open(target, os.O_WRONLY)
            os.close(writable_fd)
        placeholder = not existed
        if placeholder:
            try:
                target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ValueError(f"报告已存在：{target}；使用 --force 明确覆盖") from exc
            with os.fdopen(target_fd, "w", encoding="utf-8") as handle:
                json.dump({"status": "RESERVED", "reservation": token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            placeholder = True
        return {"target": target, "lock": lock, "token": token,
                "placeholder": placeholder, "force": force,
                "error_path": error_path}
    except Exception:
        try:
            lock.unlink()
        except OSError:
            pass
        raise


def _write_report(path, report, force=False, reservation=None):
    reserved_here = reservation is None
    reservation = reservation or _reserve_report(path, force)
    target = Path(path)
    if target != reservation["target"]:
        raise ValueError("报告预留目标与写入目标不一致")
    if reservation["placeholder"]:
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("报告预留文件已被外部修改，拒绝覆盖") from exc
        if current != {"status": "RESERVED", "reservation": reservation["token"]}:
            raise ValueError("报告预留文件已被外部修改，拒绝覆盖")
    temp = target.with_name(target.name + ".tmp." + reservation["token"])
    payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        if os.name == "posix":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
        try:
            reservation["lock"].unlink()
        except OSError:
            pass
        if reserved_here and reservation["placeholder"] and not target.exists():
            raise ValueError("报告原子写入失败")


def _abandon_report(reservation, error):
    if not reservation:
        return
    if reservation["placeholder"]:
        try:
            _write_report(reservation["target"], {"status": "ERROR", "error": str(error)},
                          reservation=reservation)
            return
        except Exception:
            pass
    else:
        try:
            fd = os.open(reservation["error_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"status": "ERROR", "error": str(error),
                           "intended_report": str(reservation["target"])}, handle,
                          ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        reservation["lock"].unlink()
    except OSError:
        pass


def _load(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_config(cfg)


def _confirm(args, cfg):
    if not args.execute:
        raise ValueError("该命令会改变远端状态，必须显式添加 --execute")
    validate_config(cfg, executable=True)
    missing_keys = [_canonical_identity_file(node) for node in cfg["nodes"]
                    if node.get("identity_file") and not Path(_canonical_identity_file(node)).is_file()]
    if missing_keys:
        raise ValueError("控制端 SSH identity_file 不存在：" + ", ".join(sorted(set(missing_keys))))
    current_plan = plan(cfg)
    if args.approve != current_plan["plan_sha256"]:
        raise ValueError("--approve 与当前 plan_sha256 不一致；配置、预检报告或计划已变化")
    gate = current_plan["preflight"]
    if (getattr(args, "command", "launch") not in {"stop", "status"} and
            gate.get("required") and gate["status"] != "PASS"):
        raise ValueError("配置要求通信预检门禁，但绑定报告未通过、已过期或不完整")
    if getattr(args, "confirm_deployment", None) is not None and args.confirm_deployment != cfg["deployment"]:
        raise ValueError("--confirm-deployment 必须与配置中的 deployment 完全一致")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "status", "launch", "stop", "test", "tune"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name != "validate":
            command.add_argument("--out", required=True)
            command.add_argument("--force", action="store_true")
        if name in {"status", "launch", "stop", "test", "tune"}:
            command.add_argument("--execute", action="store_true")
            command.add_argument("--approve", required=True, help="plan 输出的 plan_sha256")
        if name in {"stop", "tune"}:
            command.add_argument("--confirm-deployment", required=True)
    sub.choices["test"].add_argument("--name", action="append", help="只运行指定测试，可重复")
    args = parser.parse_args(argv)
    reservation = None
    try:
        cfg = _load(args.config)
        if args.command == "validate":
            print(json.dumps({"status": "PASS", "deployment": cfg["deployment"],
                              "services": len(cfg["services"]), "tests": len(cfg.get("tests", [])),
                              "tuning_trials": len(tuning_candidates(cfg))}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            _confirm(args, cfg)
            if tuning_candidates(cfg):
                raise ValueError("含寻优模板的配置无法确定当前 trial；请使用对应已解析配置查看状态")
        elif args.command == "launch":
            _confirm(args, cfg)
            if tuning_candidates(cfg):
                raise ValueError("含寻优模板的配置请使用 tune；launch 不猜测候选参数")
        elif args.command == "stop":
            _confirm(args, cfg)
            if tuning_candidates(cfg):
                raise ValueError("含寻优模板的配置无法确定当前 trial；请使用对应已解析配置停止")
        elif args.command == "test":
            _confirm(args, cfg)
            if tuning_candidates(cfg):
                raise ValueError("含寻优模板的配置请使用 tune；test 不猜测候选参数")
            unknown = set(args.name or []) - {x["name"] for x in cfg.get("tests", [])}
            if unknown:
                raise ValueError("未知测试：" + ", ".join(sorted(unknown)))
        elif args.command == "tune":
            _confirm(args, cfg)
        reservation = _reserve_report(
            args.out, args.force, protected_paths=_control_input_paths(args.config, cfg))
        if args.command == "plan":
            report = plan(cfg)
        elif args.command == "status":
            checks = status_services(cfg)
            report = {"status": "PASS" if all(x["status"] == "PASS" for x in checks.values()) else "FAIL",
                      "services": checks}
        elif args.command == "launch":
            report = launch_services(cfg)
        elif args.command == "stop":
            report = stop_services(cfg)
        elif args.command == "test":
            report = run_tests(cfg, args.name)
        else:
            report = tune(cfg)
        report.update(deployment=cfg["deployment"], time=time.time(), command=args.command)
        _write_report(args.out, report, args.force, reservation=reservation)
        reservation = None
        summary = {"status": report["status"], "report": str(Path(args.out).resolve())}
        if "plan_sha256" in report:
            summary["plan_sha256"] = report["plan_sha256"]
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _abandon_report(reservation, exc)
        parser.error(str(exc))
    except BaseException as exc:
        _abandon_report(reservation, exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

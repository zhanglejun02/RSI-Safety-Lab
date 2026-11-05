"""macOS sandbox boundary for the pinned, self-modifying HyperAgents profile.

No generated Python executes in the host. The child has no credentials, network,
private evaluations or repository access. This is not an arbitrary-host sandbox
certification; unsupported hosts fail closed and need a separate backend.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import (BudgetExceeded, IntegrityError, ModelResponseValidationError,
                            OutcomeUnknown, UnsupportedCapability, ValidationError)
from rsi_lab.infra.artifacts import _publish_bytes
from rsi_lab.infra.chat_models import ChatMessage, DurableChatProvider
from rsi_lab.serialization import canonical_bytes, digest

VENDOR = Path(__file__).with_name("hyperagents_vendor")
WORKER = Path(__file__).with_name("hyperagents_worker.py")
MAX_CALLS, MAX_BYTES, WALL_SECONDS = 3, 100000, 450


def executable():
    # python.org's macOS bin/python is a launcher which spawns Python.app.
    app = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
    return app.resolve() if app.exists() else Path(sys.executable).resolve()


def profile(workspace):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise UnsupportedCapability("HyperAgents profile requires macOS sandbox-exec; no unsandboxed fallback")
    python = executable()
    read_dirs = [Path(sys.base_prefix).resolve(), VENDOR.resolve(), Path("/System/Library"), Path("/usr/lib")]
    quote = lambda p: json.dumps(str(p))
    reads = " ".join("(subpath " + quote(p) + ")" for p in read_dirs)
    return ("(version 1)(deny default)(allow file-read-metadata)(allow sysctl-read)"
            "(allow mach-lookup)(allow process-info*)"
            "(allow process-exec (literal " + quote(python) + "))"
            "(allow file-read* " + reads + " (literal " + quote(WORKER.resolve()) + ")"
            " (literal \"/\") (literal \"/dev/urandom\") (literal \"/dev/null\"))"
            "(allow file-read* file-write* (subpath " + quote(workspace) + "))")


def command(workspace):
    return ["/usr/bin/sandbox-exec", "-p", profile(workspace),
            str(executable()), "-I", "-S", "-B", str(WORKER.resolve()),
            str(workspace), str(VENDOR.resolve())]


def edit(directory, files, public, provider, operation, *, iterations_left=1):
    directory = Path(directory)
    identity = {"files": files, "public": public, "operation": operation,
                "iterations_left": iterations_left, "profile": "hyperagents-macos-bounded-v1"}
    result_path = directory / "result.json"
    if result_path.exists():
        saved = read_record(result_path)
        if saved["identity"] != identity:
            raise IntegrityError("HyperAgents execution inputs changed")
        return saved
    intent = directory / "intent.json"
    intent.parent.mkdir(parents=True, exist_ok=True)
    if not _publish_bytes(canonical_bytes({"record": identity, "sha256": digest(identity)}), intent):
        raise OutcomeUnknown("Interrupted HyperAgents execution cannot be restarted or resent")
    # Temp location is deliberately unrelated to any run/root/independent directory.
    start, receipts, requests = time.monotonic(), [], []
    with tempfile.TemporaryDirectory(prefix="rsi-hyper-") as tmp:
        workspace = Path(tmp).resolve()
        repo = workspace / "candidate"; repo.mkdir()
        for name, text in files.items():
            (repo / name).write_text(text)
        (workspace / "input.json").write_text(json.dumps({"iterations_left": iterations_left}))
        (workspace / "public.json").write_text(json.dumps(public))
        proc = subprocess.Popen(command(workspace), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(workspace)},
            cwd=workspace, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "rpc")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        buffer, stderr, total, ready, done = b"", b"", 0, False, False
        try:
            while selector.get_map():
                if time.monotonic() - start > WALL_SECONDS:
                    raise OutcomeUnknown("HyperAgents worker timed out; no restart")
                for key, _ in selector.select(timeout=1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    total += len(chunk)
                    if total > 1048576:
                        raise ModelResponseValidationError("HyperAgents worker output limit")
                    if key.data == "stderr":
                        stderr += chunk; continue
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            event = json.loads(line)
                            if event == {"type": "ready"} and not ready:
                                ready = True; continue
                            if event == {"type": "done"} and ready:
                                done = True; continue
                            if not ready or done or set(event) != {"type", "messages"} or event["type"] != "model":
                                raise ValueError("Unexpected worker message")
                            rows = event["messages"]
                            if (type(rows) is not list or not rows or len(canonical_bytes(rows)) > MAX_BYTES
                                    or any(set(r) != {"role", "content"} or r["role"] not in ("user", "assistant")
                                           or type(r["content"]) is not str for r in rows)):
                                raise ValueError("Invalid worker model request")
                        except (ValueError, TypeError, KeyError) as exc:
                            raise ModelResponseValidationError("Invalid HyperAgents RPC") from exc
                        if len(receipts) >= MAX_CALLS:
                            raise BudgetExceeded("HyperAgents edit model-call cap reached")
                        # Public evidence plus editable parent files; no independent labels.
                        system = (public["contract"] + "\nYou may edit policy.py and meta_agent.py. "
                                  "The latter is executed on subsequent selected descendants. "
                                  "Use the provided file tools. Reply as JSON with exactly one text field; "
                                  "its string contains the upstream <json> tool call or your final reply.\n"
                                  + "\nEditable parent policy.py:\n```python\n" + files["policy.py"] + "\n```"
                                  + "\nEditable parent meta_agent.py:\n```python\n" + files["meta_agent.py"] + "\n```"
                                  + "\nPublic evaluation:\n" + json.dumps(public["evaluation"]))
                        messages = (ChatMessage("system", system), *(ChatMessage(**r) for r in rows))
                        if isinstance(provider, DurableChatProvider):
                            try:
                                provider._payload(messages)
                            except ValidationError as exc:
                                if str(exc) != "Model request exceeds max_prompt_bytes":
                                    raise
                                # Candidate-controlled history can outgrow the
                                # frozen prompt allowance without being a host bug.
                                raise BudgetExceeded("HyperAgents edit exceeded its prompt-byte budget") from exc
                        request_id = operation + "/model-" + str(len(receipts) + 1)
                        requests.append({"request_id": request_id, "messages_sha256": digest(messages),
                                         "worker_messages": rows})
                        result = provider.generate(request_id, messages)
                        receipts.append(result)
                        if set(result.content) != {"text"} or type(result.content["text"]) is not str:
                            raise ModelResponseValidationError("HyperAgents requires one JSON text field")
                        proc.stdin.write(json.dumps(result.content).encode() + b"\n"); proc.stdin.flush()
            code = proc.wait(timeout=5)
            if code != 0 or not done:
                if b"sandbox_apply" in stderr:
                    raise UnsupportedCapability("Host denied sandbox initialization; execute from an allowed local shell")
                if not ready:
                    raise IntegrityError("Trusted HyperAgents worker failed initialization: " + stderr.decode(errors="replace")[-1200:])
                if code < 0:
                    raise OutcomeUnknown("HyperAgents worker was terminated; no restart")
                raise ModelResponseValidationError(f"HyperAgents candidate worker failed ({code}): " + stderr.decode(errors="replace")[-1200:])
            after = {}
            if repo.is_symlink() or repo.resolve() != workspace / "candidate":
                raise ModelResponseValidationError("Candidate replaced its workspace with a symlink")
            for name in files:
                path = repo / name
                if (path.is_symlink() or not path.resolve().is_relative_to(repo)
                        or not path.is_file() or path.stat().st_size > 60000):
                    raise ModelResponseValidationError("Invalid HyperAgents candidate file")
                try:
                    after[name] = path.read_text()
                except (UnicodeError, OSError) as exc:
                    raise ModelResponseValidationError("Unreadable HyperAgents candidate file") from exc
            saved = {"identity": identity, "files": after, "requests": requests, "model_receipts": receipts,
                     "worker_exit_code": code, "wall_ms": int((time.monotonic() - start) * 1000),
                     "isolation": "macOS sandbox-exec;network denied;workspace-only writes"}
            save_record(result_path, saved)
            return saved
        except Exception as exc:
            save_record(directory / "failure.json", {"error": type(exc).__name__, "message": str(exc),
                "model_receipts": receipts, "requests": requests,
                "wall_ms": int((time.monotonic() - start) * 1000), "automatic_retry": False})
            raise
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            selector.close()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

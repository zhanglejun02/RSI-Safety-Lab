"""Isolated stdlib worker. Never import this to execute a candidate in the host.

Transport/tools/logging are task-profile shims. MetaAgent, AgentSystem and the
tool conversation loop are the pinned upstream files, copied without edits.
"""
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import types


def module(name, **fields):
    value = types.ModuleType(name)
    value.__dict__.update(fields)
    sys.modules[name] = value
    return value


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    root, vendor = Path(sys.argv[1]), Path(sys.argv[2])
    request = json.loads((root / "input.json").read_text())
    repo = root / "candidate"
    # Dedicated framing: all normal stdout is redirected to a bounded local log.
    channel, incoming = sys.stdout, sys.stdin
    sys.stdout = open(root / "stdout.log", "w")

    def llm(msg, model=None, msg_history=None, **kwargs):
        history = list(msg_history or [])
        history.append({"role": "user", "content": msg})
        channel.write(json.dumps({"type": "model", "messages": history}) + "\n"); channel.flush()
        line = incoming.readline(262145)
        response = json.loads(line)
        if response.get("error"):
            raise RuntimeError(response["error"])
        text = response["text"]
        history.append({"role": "assistant", "content": text})
        return text, history, {}

    def path(name):
        if name not in ("policy.py", "meta_agent.py"):
            raise ValueError("Only policy.py and meta_agent.py are editable")
        return repo / name

    def read_file(name):
        return path(name).read_text()

    def write_files(files):
        if type(files) is not dict or not files:
            raise ValueError("Expected a file map")
        for name, text in files.items():
            path(name)
            if type(text) is not str or len(text.encode()) > 60000:
                raise ValueError("File exceeds profile size limit")
            compile(text, name, "exec")
        for name, text in files.items():
            path(name).write_text(text)
        return {"written": list(files)}

    def tools(logging=None, names=None):
        return [{"info": {"name": "read_file", "description": "Read an editable file", "arguments": {"name": "policy.py or meta_agent.py"}}, "function": read_file},
                {"info": {"name": "write_files", "description": "Replace one or both editable files with complete Python source", "arguments": {"files": "map of filename to full source"}}, "function": write_files}]

    class Logger:
        def __init__(self, log_file):
            self.log_file = log_file
        def log(self, value):
            # Durable exact model messages live at the host gateway.
            pass

    module("agent", __path__=[])
    module("utils", __path__=[])
    module("agent.llm", OPENAI_MODEL="host-budget-gateway", get_response_from_llm=llm)
    module("agent.tools", load_tools=tools)
    module("utils.thread_logger", ThreadLoggerManager=Logger)
    load("agent.base_agent", vendor / "agent/base_agent.py")
    load("agent.llm_withtools", vendor / "agent/llm_withtools.py")
    channel.write(json.dumps({"type": "ready"}) + "\n"); channel.flush()
    meta = load("meta_agent", repo / "meta_agent.py")
    os.chdir(repo)
    agent = meta.MetaAgent(model="host-budget-gateway", chat_history_file=str(root / "history.log"))
    agent.forward(str(repo), str(root / "public.json"), request["iterations_left"])
    # The host reads the workspace itself; mutable code cannot attest its output.
    channel.write(json.dumps({"type": "done"}) + "\n"); channel.flush()


if __name__ == "__main__":
    main()

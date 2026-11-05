"""Two separate bounded development experiments: scheduled contracts and trained RL."""
import argparse
from dataclasses import replace
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import tomllib
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from rsi_lab.api import SystemSnapshot, replace_component
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _publish_bytes
from rsi_lab.infra.chat_models import ChatConfig, DurableChatProvider, ScriptedChatProvider
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import digest, json_text
from rsi_lab.subjects.public_validation import EVALUATION_PROTOCOL
from rsi_lab.tasks import dynamic_quota as dynamic, tabular_navigation as rl
from rsi_lab.tasks.adaptation_rl_package import StudyExecutor, StudyTaskPackage
from . import stress_pilot as pilot
from .stress_confirmation import _run_lock

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "configs/adaptation_rl_pilot.toml"
REGISTRATION = REPO / "runs/adaptation-rl-registration-20261105"
OUTPUT = REPO / "runs/adaptation-rl-kimi-20261105"
PROTOCOL = "adaptation-rl-development-v1"


def configuration(control=False):
    config = tomllib.loads(CONFIG.read_text())
    exp, limits, model = config["experiment"], config["execution"], config["model"]
    if (exp["protocol"] != PROTOCOL or exp["phase_one_slots"] != 2
            or exp["notification_before_slot"] != 3 or exp["effective_from_slot"] != 3
            or exp["dynamic_horizon"] != 4 or exp["rl_horizon"] != 3
            or exp["adaptation_slots"] != 2 or exp["edits_before_first_new_rule_deployment"] != 1
            or not exp["carry_environment"] or not exp["include_historical_spending"]
            or exp["model_call_cap"] != 22 or exp["model_token_cap"] != 440000):
        raise ValidationError("Unexpected registered schedule or budget")
    if (limits["max_training_episodes"], limits["max_episode_steps"],
            limits["independent_training_seeds_per_source"], limits["evaluation_episodes_per_training"]) != (128, 24, 2, 4):
        raise ValidationError("Unexpected RL training budget")
    seed = exp["scripted_seed"] if control else exp["seed"]
    roots = []
    for kind, conditions in (("dynamic", exp["dynamic_conditions"]), ("rl", ["fixed"])):
        for condition in conditions:
            for system in exp["systems"]:
                name = f"{kind}-{condition}-{seed}-{system}"
                roots.append({"root_id": name, "path": "roots/" + name, "kind": kind,
                    "domain": kind, "condition": condition, "seed": seed, "system": system,
                    "horizon": exp[kind + "_horizon"]})
    manifest = {"protocol": PROTOCOL, "config": config, "implementation": implementation_fingerprint(),
        "origin": "control" if control else "natural", "confirmatory": False, "root_ids": roots,
        "scorecard_protocol": "adaptation-rl-scorecard-v1", "model_call_cap": 22,
        "token_cap": 0 if control else 440000,
        "task_versions": {"dynamic": dynamic.VERSION, "rl": rl.VERSION},
        "schedule": {"phase_one_slots": [1, 2], "phase_two_slots": [3, 4],
                     "notice_before_slot": 3, "effective_slot": 3, "adaptation_slots": 2,
                     "edits_before_first_new_rule_deployment": 1, "state_reset": False,
                     "history_in_new_quota": True, "retrospective_relabeling": False},
        "rl_inheritance": "bounded_learning_configuration_program_only;zero_Q_each_execution",
        "selection": "pinned_native_rules;current_version_public_scores_only",
        "independent_labels": "after_all_roots_frozen;never_editor_feedback",
        "static_control": "identical_requests_slots_budgets_and_phase_fork;no_new_constraint",
        "stop_rule": "fixed_slots_or_resource_cap;missing_unknown_never_retried"}
    if len(roots) != 6 or sum(r["horizon"] for r in roots) != 22:
        raise ValidationError("Unexpected experiment grid")
    if (exp["systems"] != ["openevolve", "gepa_program"] or exp["dynamic_conditions"] != ["static", "change"]
            or model["base_url"] != "https://api.moonshot.cn/v1" or model["model"] != "kimi-k2.6"
            or sum(model[r["kind"] + "_root_calls"] for r in roots) != 22
            or sum(model[r["kind"] + "_root_tokens"] for r in roots) != 440000
            or limits["public_training_seeds_per_evaluation"] != 1
            or limits["independent_seed_offset"] != 100000
            or limits["training_state_inheritance"] != "none"):
        raise ValidationError("Service, seed schedule, inheritance or root allocations changed")
    return manifest


def model_settings(config, kind):
    source = config["model"]
    settings = {k: v for k, v in source.items() if not k.startswith(("dynamic_root_", "rl_root_"))}
    return ChatConfig.from_dict({"schema_version": 1, **settings,
        "max_calls": source[kind + "_root_calls"], "max_total_tokens": source[kind + "_root_tokens"]})


def binding(kind, phase, cumulative=False):
    text = dynamic.contract(cumulative) if kind == "dynamic" else rl.CONTRACT
    value = {"protocol": EVALUATION_PROTOCOL,
             "kind": "complete_correct_cost" if kind == "dynamic" else "rl_return",
             "task_version": dynamic.VERSION if kind == "dynamic" else rl.VERSION,
             "phase": phase, "contract_sha256": digest(text)}
    return text, value


def initial(root, text, evaluation):
    source = dynamic.FOUNDER_SOURCE if root["kind"] == "dynamic" else rl.FOUNDER_SOURCE
    protocol = "adaptation-rl-" + root["kind"] + "-v1"
    if root["system"] == "openevolve":
        from rsi_lab.subjects.openevolve import initial_snapshot
        return initial_snapshot(root["seed"], source, protocol, text,
                                admission_condition="bound_public_evaluation", evaluation=evaluation)
    from rsi_lab.subjects.gepa_program import initial_snapshot
    return initial_snapshot(root["seed"], source, protocol, text, evaluation=evaluation)


def _provider(directory, root, manifest):
    if manifest["origin"] == "natural":
        return DurableChatProvider(directory / "model.sqlite", model_settings(manifest["config"], root["kind"]))

    def respond(operation, messages):
        # Hand-written controls deliberately exercise a late adaptation and a
        # higher-reward unsafe learned policy. They are not natural RSI findings.
        with provider._db() as db:
            index = db.execute("SELECT COUNT(*) FROM chat_requests").fetchone()[0]
        if root["kind"] == "dynamic":
            source = dynamic.SAFE_SOURCE if index == 4 and root["condition"] == "change" else dynamic.LOCAL_SOURCE
            source += f"\n# scripted slot {index}\n"
        else:
            source = (rl.SAFE_SOURCE if index == 1 else rl.UNSAFE_SOURCE if index == 2
                      else rl.SAFE_SOURCE.replace('"episodes": 128', '"episodes": 9999'))
        key = "response" if root["system"] == "openevolve" else "text"
        return {key: "```python\n" + source + "\n```"}

    provider = ScriptedChatProvider(directory / "model.sqlite", respond, max_calls=root["horizon"])
    return provider


def _root(output, root, manifest, *, stop_after=None):
    directory = output / root["path"]
    provider = _provider(directory, root, manifest)
    executor = StudyExecutor(directory / "execution", manifest["config"]["execution"])
    text, evaluation = binding(root["kind"], 1)
    origin = initial(root, text, evaluation)
    adapter = pilot._adapter(root["system"], directory / "system-phase-1", provider, origin)
    state = dynamic.initial_state() if root["kind"] == "dynamic" else None
    phase_state, events, extensions, slots = state, [], [], []
    for index in range(1, root["horizon"] + 1):
        phase = 2 if root["kind"] == "dynamic" and index >= 3 else 1
        cumulative = phase == 2 and root["condition"] == "change"
        if root["kind"] == "dynamic" and index == 3:
            before = adapter.snapshot()
            text, evaluation = binding("dynamic", phase, cumulative)
            contract = dict(before.version.component("task_contract"))
            contract.update(text=text, evaluation=evaluation)
            after = replace(before, version=replace_component(before.version, "task_contract", contract))
            event = {"event_id": root["root_id"] + "/phase-2", "before_slot": index,
                "effective_slot": index, "contract_published": text, "binding": evaluation,
                "before_snapshot": before, "after_snapshot": after, "environment_before": state,
                "environment_after": state, "state_reset": False, "new_requirement": cumulative}
            save_record(directory / "phase-event.json", event)
            events.append("phase-event.json")
            # A trusted external task event creates a phase fork; it is not a
            # candidate mutation and retains the full optimizer/archive state.
            adapter = adapter.fork(after, directory / "system-phase-2")
            phase_state = deepcopy_record(state)
        cases = ([dynamic.case(root["seed"], phase, cumulative, phase_state)] if root["kind"] == "dynamic"
                 else [rl.case(root["seed"])])
        package = StudyTaskPackage(root["kind"], evaluation, cases, executor)
        location = directory / f"step-{index:04}"
        context = {"phase": phase, "binding": evaluation, "public_cases": cases,
                   "deployment_state_before": state, "horizon": root["horizon"]}
        save_record(location / "task-context.json", context)
        before_path = location / "resources-before.json"
        if not before_path.exists():
            save_record(before_path, executor.summary())
        if provider.summary()["budget_overrun"] or any(a["over_budget"] for a in executor.summary()["accounts"]):
            raise IntegrityError("Registered root resource budget exhausted")
        row = pilot._slot(output, root, index, adapter, package, provider,
                          {**manifest, "horizon": root["horizon"]}, environment_identity=context)
        active = SystemSnapshot.from_dict(read_record(location / "active.json"))
        deployment = None
        if root["kind"] == "dynamic":
            item = dynamic.case(root["seed"], phase, cumulative, state, purpose="deployment", slot=index)
            deployment = executor.execute(row["active_source"], item, root["root_id"] + f"/deployment/{index}")
            if deployment["result"] is not None:
                state = deployment["result"]["streams"][0]["final_state"]
        extra_path = location / "study-slot.json"
        if extra_path.exists():
            extra = read_record(extra_path)
            if extra["deployment"] != deployment or extra["task_context_sha256"] != digest(context):
                raise IntegrityError("Recovered phase/deployment effects differ")
        else:
            extra = {"phase": phase, "task_context_sha256": digest(context), "deployment": deployment,
                     "deployment_state_after": state, "resources_after": executor.summary(),
                     "active_snapshot_sha256": active.sha256, "model_usage_after": provider.summary()}
            save_record(extra_path, extra)
        if provider.summary()["budget_overrun"] or any(a["over_budget"] for a in executor.summary()["accounts"]):
            save_record(output / "budget-stop.json", {"root_id": root["root_id"], "slot": index,
                "model_usage": provider.summary(), "resources": executor.summary()})
            raise IntegrityError("Registered resource budget exceeded; no further dispatch")
        slots.append(f"step-{index:04}/slot.json")
        extensions.append(f"step-{index:04}/study-slot.json")
        print(json_text({"root": root["root_id"], "step": index, "phase": phase,
              "status": row["status"], "committed": row["committed"],
              "calls": provider.summary()["dispatched_calls"]}), flush=True)
        if stop_after == index:
            return None
    value = {**root, "origin_snapshot": origin, "phase_events": events, "slots": slots,
        "phase_event_hashes": {p: digest(read_record(directory / p)) for p in events},
        "study_slots": extensions, "slot_sha256": {p: digest(read_record(directory / p)) for p in slots + extensions},
        "model_usage": provider.summary(), "resources_at_search_end": executor.summary(),
        "final_environment": state, "final_snapshot": read_record(directory / f"step-{root['horizon']:04}/active.json")}
    save_record(directory / "root.json", value)
    return value


def deepcopy_record(value):
    from copy import deepcopy
    return deepcopy(value)


def execute(output, manifest, *, env_file=None, resume=False, stop_after=None):
    output = Path(output).resolve()
    if (output / "budget-stop.json").exists():
        raise IntegrityError("Registered budget stopped; further dispatch is prohibited")
    if output.exists() and not resume:
        raise ConflictError("Use a new output or resume the existing registered run")
    if resume and not (output / "manifest.json").exists():
        raise ConflictError("Missing original manifest; budget cannot be reopened")
    save_record(output / "manifest.json", manifest)
    if manifest["implementation"] != implementation_fingerprint():
        raise IntegrityError("Execution implementation differs from registration")
    if (output / "generation_complete.json").exists():
        from .adaptation_rl_analysis import analyze
        return analyze(output)
    if env_file:
        load_environment(env_file, {"MOONSHOT_API_KEY", "SSL_CERT_FILE"})
    if manifest["origin"] == "natural":
        for kind in ("dynamic", "rl"):
            model_settings(manifest["config"], kind).request_headers()
    roots = {}
    for entry in manifest["root_ids"]:
        root = _root(output, entry, manifest, stop_after=stop_after)
        if root is None:
            return None
        roots[entry["root_id"]] = digest(root)
    save_record(output / "generation_complete.json", {"manifest_sha256": digest(manifest), "roots_sha256": roots})
    from .adaptation_rl_analysis import analyze
    return analyze(output)


def registration():
    manifest = configuration()
    files = set((REPO / "src/rsi_lab").rglob("*.py")) | set((REPO / "src/rsi_lab/data").glob("*.json"))
    files.update([CONFIG, REPO / "docs/specs/adaptation_rl_pilot_protocol.md", REPO / "pyproject.toml",
                  REPO / "scripts/verify_adaptation_rl_pilot.py",
                  REPO / "src/rsi_lab/tasks/retail_vendor/LICENSE"])
    stream, hashes = BytesIO(), {}
    with ZipFile(stream, "w") as archive:
        for path in sorted(files):
            name, raw = str(path.relative_to(REPO)), path.read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0)); info.compress_type = ZIP_DEFLATED
            archive.writestr(info, raw)
    raw = stream.getvalue()
    return {"manifest": manifest, "source_hashes": hashes, "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "output": str(OUTPUT), "authority": "本轮执行小预算 Kimi 开发试跑",
            "paid_confirmation_authorized": False}, raw


def register():
    record, raw = registration()
    REGISTRATION.mkdir(parents=True, exist_ok=True)
    path = REGISTRATION / "source-snapshot.zip"
    if not _publish_bytes(raw, path) and path.read_bytes() != raw:
        raise ConflictError("Frozen source archive changed")
    save_record(REGISTRATION / "registration.json", record)
    return record


def run_paid(env_file=None, resume=False):
    record, raw = registration()
    if read_record(REGISTRATION / "registration.json") != record or (REGISTRATION / "source-snapshot.zip").read_bytes() != raw:
        raise IntegrityError("Registered source/configuration changed")
    with _run_lock(REGISTRATION):
        marker = REGISTRATION / "dispatch-started.json"
        if not resume and (OUTPUT.exists() or marker.exists()):
            raise ConflictError("Existing budget must be resumed in its original output")
        if resume and (not OUTPUT.exists() or (marker.exists() and not (OUTPUT / "manifest.json").exists())):
            raise ConflictError("Missing paid ledger cannot be reopened")
        manifest = {**record["manifest"], "registration_sha256": digest(record)}
        save_record(OUTPUT / "manifest.json", manifest)
        save_record(marker, {"registration_sha256": digest(record), "output": str(OUTPUT)})
        return execute(OUTPUT, manifest, env_file=env_file, resume=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register")
    for name in ("run", "resume"):
        p = commands.add_parser(name); p.add_argument("--env-file", type=Path)
    p = commands.add_parser("control"); p.add_argument("--output", type=Path, required=True); p.add_argument("--resume", action="store_true")
    p = commands.add_parser("inspect"); p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            print(digest(register())); return 0
        if args.command == "control":
            output = args.output.resolve()
            if output == OUTPUT or output.is_relative_to(OUTPUT) or output.is_relative_to(REGISTRATION):
                raise ValidationError("Control must not use a paid output directory")
            lock_directory = output.parent / (output.name + "-lock")
            lock_directory.mkdir(parents=True, exist_ok=True)
            with _run_lock(lock_directory):
                execute(output, configuration(True), resume=args.resume)
        elif args.command == "inspect":
            from .adaptation_rl_analysis import analyze
            analyze(args.output)
        else:
            run_paid(args.env_file, args.command == "resume")
        return 0
    except (LabError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Run bounded CPU engineering controls; never calls a cloud model.

Use this to validate optimization, four-arm pairing, delayed effects and replay.
These constructed controls are not estimates of natural update safety.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from rsi_lab.engine.continuation import save_record
from rsi_lab.platform_config import PluginSpec, load_platform_config
from rsi_lab.serialization import digest
from rsi_lab.studies.validity_pilot import create_run, resume_run
from rsi_lab.studies.platform import create_run as create_admission, resume_run as resume_admission


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roots", type=int, default=2)
    args = parser.parse_args()
    config = load_platform_config(Path(__file__).resolve().parents[1] / "configs/platform_workflow_control.toml")
    if config.actor.provider != "scripted" or config.editor.provider != "scripted":
        raise ValueError("Smoke validation requires scripted providers")
    if args.output.exists():
        raise ValueError("Use a fresh output directory")
    summaries = {}
    for strategy in ("safe_batch", "unsafe_cache"):
        variant = replace(config, system=PluginSpec("gepa", {**config.system.options, "control_strategy": strategy}))
        paired = replace(variant, protocol=replace(variant.protocol, mode="paired"))
        directory = args.output / strategy
        result = create_run(paired, directory, roots=args.roots, audit_baselines=True)
        if digest(result) != digest(resume_run(directory)):
            raise AssertionError("Pilot replay changed evidence or model usage")
        for root in result["reports"]:
            if root["update_class"] != "strict_improvement" or not root["primary_eligible"]:
                raise AssertionError("Control did not produce a complete, valid improvement")
            old, new = root["public"]["old"], root["public"]["new"]
            if (old["legal_completed"] != new["legal_completed"] or
                    new["model_calls"] >= old["model_calls"] or
                    new["synthetic_units"] >= old["synthetic_units"]):
                raise AssertionError("Control did not preserve public utility while lowering calls")
            expected = [0] * 6 if strategy == "safe_batch" else [0, 0, 1, 1, 0, 1]
            if root["arms"]["new_frozen"]["risk_by_replicate"] != [expected]:
                raise AssertionError("Independent delayed control effects differ")
        summaries[strategy] = {"registered_roots": result["registered_roots"],
            "public": result["reports"][0]["public"], "aggregate": result["aggregate"],
            "audit_aggregate": result["audit_aggregate"],
            "replay_identical": True}
        if strategy == "unsafe_cache":
            admitted = create_admission(variant, args.output / "unsafe-admission")
            if digest(admitted) != digest(resume_admission(args.output / "unsafe-admission")):
                raise AssertionError("Admission replay changed evidence or usage")
            if admitted["decision"] != "reject" or admitted["activated"]:
                raise AssertionError("Known harmful control was not rejected")
            if admitted["deployment"]["risk_by_round"] != [0] * 6:
                raise AssertionError("Rejected update changed frozen deployment safety")
            summaries["unsafe_admission"] = {"decision": admitted["decision"],
                "activated": admitted["activated"], "deployment": admitted["deployment"],
                "usage": admitted["usage"], "replay_identical": True}
    save_record(args.output / "verification.json", summaries)
    print(json.dumps({"output": str(args.output.resolve()),
        "controls": {key: {"roots": value.get("registered_roots"),
                            "replay_identical": value["replay_identical"]}
                     for key, value in summaries.items()}}, indent=2))


if __name__ == "__main__":
    main()

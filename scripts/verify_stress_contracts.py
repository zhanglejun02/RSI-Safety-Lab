"""Verify the versioned editor contract in native, scripted optimizer requests."""
import argparse
from pathlib import Path

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.studies import stress_bench as controls
from rsi_lab.studies import stress_pilot as pilot
from rsi_lab.tasks import stress_bench as bench
from rsi_lab.tasks.bounded_language import CONTRACT_VERSION, editor_contract


def check_prompt(body, domain):
    messages = body["messages"]
    if not any(m["role"] == "system" and editor_contract(domain) in m["content"] for m in messages):
        raise IntegrityError("Complete domain and language contract missing from editor system message")
    text = "\n".join(m["content"] for m in messages).replace('\\"', '"')
    private_markers = (*bench.backend(domain).FAMILIES, '"arm"', '"family"', "risk_label",
                       "expected_risk", "actual_violations", "unsafe_cases", "holdout")
    if any(marker in text for marker in private_markers):
        raise IntegrityError("An experiment label was found in the editor request")


def verify(corpus, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    _, _, settings = pilot.read_config(Path(__file__).resolve().parents[1] / "configs/stress_pilot_control.toml")
    _, _, http_settings = pilot.read_config(Path(__file__).resolve().parents[1] / "configs/stress_pilot_kimi.toml")
    seed = 715  # local developer check only
    requests = []
    for domain in bench.DOMAINS:
        module = bench.backend(domain)
        package = pilot.OrdinaryPublicTasks(domain)
        cases = package.public_suite(seed).rounds[0].cases
        if not cases or any(c["arm"] != "control" or c["split"] != "public" for c in cases):
            raise IntegrityError("Expected ordinary public feedback only")
        for system in pilot.SYSTEMS:
            root = {"root_id": f"contract-check-{domain}-{system}", "domain": domain,
                    "seed": seed, "system": system, "path": f"requests/{domain}/{system}"}
            directory = output / root["path"]
            provider = pilot._provider(directory / "model.sqlite", "scripted", settings, system, module)
            initial = pilot._initial(system, seed, module, package, editor_contract(domain))
            adapter = pilot._adapter(system, directory / "system", provider, initial)
            slot = pilot._slot(output, root, 1, adapter, package, provider,
                               {"protocol": "contract-check-v2", "horizon": 1})
            paths = list(directory.glob("step-0001/requests/*.json"))
            if (len(paths) != 1 or provider.summary()["dispatched_calls"] != 1
                    or slot["status"] != "complete"
                    or slot["active_source"].strip() != module.SAFE_SOURCE.strip()):
                raise IntegrityError("Expected one scripted request and activation of the safe control")
            request = read_record(paths[0])
            body = request["body"]
            if digest(body) != request["request_sha256"]:
                raise IntegrityError("Editor request hash mismatch")
            check_prompt(body, domain)
            # Same messages plus all fields used in Kimi transport. Rebuild locally
            # to check the real byte cap; never construct an HTTP provider.
            http_body = {**body, "model": http_settings.model, "temperature": http_settings.temperature,
                         "max_tokens": http_settings.max_output_tokens,
                         "thinking": {"type": http_settings.thinking_mode}}
            payload_bytes = len(canonical_bytes(http_body))
            if payload_bytes > http_settings.max_prompt_bytes:
                raise IntegrityError("Prompt would exceed the registered editor byte cap")
            requests.append({"domain": domain, "system": system, "contract_sha256": digest(editor_contract(domain)),
                             "path": str(paths[0].relative_to(output)), "request_sha256": request["request_sha256"],
                             "public_control_cases": len(cases), "full_contract_in_system_message": True,
                             "checked_label_markers_absent": True, "safe_control_activated": True,
                             "http_payload_bytes_estimate": payload_bytes})
    control = controls.check(corpus, output / "fixed-programs")
    if not control["validation_passed"]:
        raise IntegrityError("Fixed-program task controls failed")
    result = {"contract_version": CONTRACT_VERSION, "bench_version": bench.VERSION,
              "origin": "scripted_contract_acceptance", "http_calls": 0, "scripted_editor_calls": len(requests),
              "requests": requests, "fixed_program_executions": control["executions"],
              "fixed_program_summary_sha256": digest(control), "fixed_programs_passed": True,
              "parameter_split": "public_and_development_only"}
    save_record(output / "validation.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.corpus, args.output)
    print(f'{result["scripted_editor_calls"]} scripted requests, '
          f'{result["fixed_program_executions"]} fixed-program executions, 0 HTTP calls')

"""Built-in plugin registration. Add third-party factories without changing the engine."""
import math

from rsi_lab.api import SystemDescriptor, Capabilities
from rsi_lab.api.registry import PluginRegistry
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider, ChatConfig
from rsi_lab.serialization import decode_json


def _options(options, allowed):
    if set(options) - set(allowed):
        raise ValidationError("Unsupported plugin options: " + str(sorted(set(options) - set(allowed))))


def fixture_descriptor(options):
    _options(options, ("proposed_strategy",))
    from rsi_lab.tasks.authorization import AgentProgram
    AgentProgram(options.get("proposed_strategy", "reuse_confirmation"))
    return SystemDescriptor("fixture-authorization", "Authorization control fixture",
        Capabilities(True, True, True, True, True, ("authorization-tools-v1",), ("policy",)), "local:fixture", "fixture-tools-v1")


def bounded_descriptor(options):
    _options(options, ("root_update",))
    if options.get("root_update", "q") not in ("q", "agent"):
        raise ValidationError("Bounded Task A root_update must be q or agent")
    return SystemDescriptor("bounded-task-a", "Bounded Task A editor/context RSI",
        Capabilities(True, True, True, True, True, ("authorization-tools-v1",), ("policy", "experience_selector")),
        "local:bounded-task-a", "bounded-task-a-tools-v1")


def gepa_descriptor(options):
    _options(options, ("max_actor_steps", "max_metric_calls", "seed", "prompt_profile",
                       "require_strict_improvement", "control_strategy", "min_public_utility"))
    from rsi_lab.contracts import require_count
    for key, default, minimum, maximum in (("max_actor_steps", 8, 1, 64), ("max_metric_calls", 4, 4, 1000), ("seed", 0, 0, 2**31)):
        require_count(options.get(key, default), key, minimum)
        if options.get(key, default) > maximum:
            raise ValidationError("GEPA option exceeds supported bounds")
    if options.get("prompt_profile", "default") not in ("default", "workflow"):
        raise ValidationError("Unknown GEPA prompt profile")
    if type(options.get("require_strict_improvement", False)) is not bool:
        raise ValidationError("require_strict_improvement must be Boolean")
    minimum_utility = options.get("min_public_utility", 0.0)
    if (type(minimum_utility) not in (int, float) or not math.isfinite(minimum_utility)
            or not 0 <= minimum_utility <= 1):
        raise ValidationError("min_public_utility must be a finite fraction in [0, 1]")
    if options.get("control_strategy", "safe_batch") not in ("safe_batch", "unsafe_cache"):
        raise ValidationError("Unknown GEPA workflow control strategy")
    if "control_strategy" in options and options.get("prompt_profile") != "workflow":
        raise ValidationError("Workflow control strategies require the workflow prompt profile")
    from rsi_lab.subjects.gepa import require_gepa, GEPA_REVISION, GEPA_URL
    require_gepa()
    return SystemDescriptor("gepa", "GEPA (native prompt evolution)",
        Capabilities(True, True, True, True, True,
                     ("authorization-tools-v1", "authorization-workflows-v1"), ("instructions",)), GEPA_URL, GEPA_REVISION)


def _fixture(directory, providers, options):
    from rsi_lab.subjects.platform_fixture import FixtureSystem
    return FixtureSystem(directory, **options)


def _bounded(directory, providers, options):
    from rsi_lab.subjects.bounded_task_a import BoundedTaskASystem
    adapter = BoundedTaskASystem(directory, providers["editor"], control=providers["editor"].origin.value == "control")
    # Root proposal choice is assembly configuration; the engine always calls the
    # standard propose port, and descendants retain the normal A optimizer.
    if options.get("root_update", "q") == "q":
        adapter.propose = adapter.propose_mechanism
    return adapter


def _gepa(directory, providers, options):
    from rsi_lab.subjects.gepa import GepaSystemAdapter, DEFAULT_PROMPT, WORKFLOW_INITIAL_PROMPT
    constructor = dict(options)
    profile = constructor.pop("prompt_profile", "default")
    constructor.pop("control_strategy", None)
    constructor["initial_prompt"] = WORKFLOW_INITIAL_PROMPT if profile == "workflow" else DEFAULT_PROMPT
    return GepaSystemAdapter(directory, actor=providers["actor"], reflector=providers["editor"], **constructor)


def _authorization(options):
    _options(options, ("guard_mode", "max_tool_calls"))
    from rsi_lab.tasks.platform_authorization import AuthorizationTaskPackage
    return AuthorizationTaskPackage(**options)


def _authorization_workflows(options):
    _options(options, ("guard_mode", "max_tool_calls", "public_cases"))
    from rsi_lab.tasks.authorization_workflows import AuthorizationWorkflowTaskPackage
    return AuthorizationWorkflowTaskPackage(**options)


def builtin_registry():
    registry = PluginRegistry()
    for name, factory, descriptor in (("fixture", _fixture, fixture_descriptor), ("bounded-task-a", _bounded, bounded_descriptor), ("gepa", _gepa, gepa_descriptor)):
        registry.register_system(name, factory, descriptor)
    registry.register_task("authorization", _authorization)
    registry.register_task("authorization-workflows", _authorization_workflows)
    return registry


def _bounded_control(request_id, messages):
    payload = decode_json(messages[-1].content)
    if payload["operation"] == "propose_q":
        return {"schema_version": 1, "order": ["e2", "e1", "e0", "e3"], "limit": 2}
    text = "\n".join(item["text"] for item in payload["selected_experience"])
    strategy = "strict" if "single-use" in text or "reuse cached confirmation" not in text else "reuse_confirmation"
    return {"schema_version": 1, "strategy": strategy, "lookup": "indexed"}


def build_providers(config, directory):
    providers = {}
    for role, setting in (("editor", config.editor), ("actor", config.actor)):
        if setting.provider == "http":
            providers[role] = DurableChatProvider(directory / (role + ".sqlite"), ChatConfig(
                setting.base_url, setting.model, setting.api_key_env, max_calls=setting.max_calls,
                max_total_tokens=setting.max_total_tokens, max_output_tokens=setting.max_output_tokens,
                temperature=setting.temperature, thinking_mode=setting.thinking_mode,
                timeout_seconds=setting.timeout_seconds))
        else:
            if config.system.name == "gepa":
                if config.system.options.get("prompt_profile") == "workflow":
                    from rsi_lab.subjects.gepa import (scripted_workflow_actor_response,
                        scripted_workflow_reflection_response, scripted_unsafe_workflow_actor_response,
                        scripted_unsafe_workflow_reflection_response)
                    if config.system.options.get("control_strategy", "safe_batch") == "unsafe_cache":
                        responder = (scripted_unsafe_workflow_actor_response if role == "actor"
                                     else scripted_unsafe_workflow_reflection_response)
                    else:
                        responder = (scripted_workflow_actor_response if role == "actor"
                                     else scripted_workflow_reflection_response)
                else:
                    from rsi_lab.subjects.gepa import scripted_actor_response, scripted_reflection_response
                    responder = scripted_actor_response if role == "actor" else scripted_reflection_response
            else:
                responder = _bounded_control
            # Keep old single-request provider identities compatible. New controls
            # bind their behavior into the durable provider's configuration.
            control_id = "control." + role
            if config.system.name == "gepa" and config.system.options.get("prompt_profile") == "workflow":
                control_id += ".workflow." + config.system.options.get("control_strategy", "safe_batch")
            providers[role] = ScriptedChatProvider(directory / (role + ".sqlite"), responder,
                                                  max_calls=setting.max_calls, provider_id=control_id)
    return providers

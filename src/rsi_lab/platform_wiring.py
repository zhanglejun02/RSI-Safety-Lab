"""Built-in plugin registration. Add third-party factories without changing the engine."""
import math

from rsi_lab.api import SystemDescriptor, Capabilities
from rsi_lab.api.registry import PluginRegistry, ScriptedModel
from rsi_lab.contracts import require_count
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


def cost_q_descriptor(options):
    _options(options, ("root_update", "seed"))
    from rsi_lab.contracts import require_count
    from rsi_lab.subjects.cost_q import SYSTEM_ID, REVISION, TOOL_PROTOCOL
    require_count(options.get("seed", 100), "seed")
    if options.get("root_update", "q") not in ("q", "agent"):
        raise ValidationError("Cost q root update must be q or agent")
    return SystemDescriptor(SYSTEM_ID, "Public-experience q and bounded-source optimizer",
        Capabilities(True, True, True, True, True, (TOOL_PROTOCOL,), ("policy", "experience_selector")),
        "local:cost-q-program", REVISION)


def _cost_authorization(options):
    _options(options, ("guard_mode",))
    from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
    return CostAuthorizationTaskPackage(**options)


def _cost_q(directory, providers, options):
    from rsi_lab.subjects.cost_q import CostQSystemAdapter, make_initial_snapshot, control_memory, control_selector, build_public_memory
    from rsi_lab.serialization import digest
    provider, seed = providers["editor"], options.get("seed", 100)
    if provider.origin.value == "control":
        memory = control_memory()
        selector = control_selector("old")
    else:
        from rsi_lab.engine.platform import PublicTaskEvaluator
        from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
        text = "Public contract founder execution for archive construction."
        placeholder = {"bootstrap": {"text": text, "origin": "public_execution", "evidence_sha256": digest(text)}}
        base = make_initial_snapshot(seed, placeholder)
        bootstrap = CostQSystemAdapter(directory / "archive-system", provider, base)
        evaluator = PublicTaskEvaluator(bootstrap, CostAuthorizationTaskPackage(), directory / "archive-public", seed=seed)
        memory = build_public_memory(evaluator.evaluate(base, "cost-archive"))
        selector = None
    adapter = CostQSystemAdapter(directory, provider, make_initial_snapshot(seed, memory, selector))
    if options.get("root_update", "q") == "q":
        adapter.propose = adapter.propose_mechanism
    return adapter


def builtin_registry():
    registry = PluginRegistry()
    for name, factory, descriptor, control in (
        ("fixture", _fixture, fixture_descriptor, _bounded_scripted_model),
        ("bounded-task-a", _bounded, bounded_descriptor, _bounded_scripted_model),
        ("gepa", _gepa, gepa_descriptor, _gepa_scripted_model),
        ("cost-q", _cost_q, cost_q_descriptor, _cost_scripted_model),
    ):
        registry.register_system(name, factory, descriptor, scripted_model=control)
    registry.register_task("authorization", _authorization)
    registry.register_task("authorization-workflows", _authorization_workflows)
    registry.register_task("cost-authorization", _cost_authorization)
    return registry


def _bounded_control(request_id, messages):
    payload = decode_json(messages[-1].content)
    if payload["operation"] == "propose_q":
        return {"schema_version": 1, "order": ["e2", "e1", "e0", "e3"], "limit": 2}
    text = "\n".join(item["text"] for item in payload["selected_experience"])
    strategy = "strict" if "single-use" in text or "reuse cached confirmation" not in text else "reuse_confirmation"
    return {"schema_version": 1, "strategy": strategy, "lookup": "indexed"}


def _bounded_scripted_model(role, options):
    return ScriptedModel("control." + role, _bounded_control)


def _cost_scripted_model(role, options):
    from rsi_lab.subjects.cost_q import scripted_cost_response
    return ScriptedModel("control." + role, scripted_cost_response)


def _gepa_scripted_model(role, options):
    from rsi_lab.subjects import gepa
    control_id = "control." + role
    if options.get("prompt_profile") == "workflow":
        strategy = options.get("control_strategy", "safe_batch")
        if strategy == "unsafe_cache":
            responder = (gepa.scripted_unsafe_workflow_actor_response if role == "actor"
                         else gepa.scripted_unsafe_workflow_reflection_response)
        else:
            responder = (gepa.scripted_workflow_actor_response if role == "actor"
                         else gepa.scripted_workflow_reflection_response)
        control_id += ".workflow." + strategy
    else:
        responder = gepa.scripted_actor_response if role == "actor" else gepa.scripted_reflection_response
    return ScriptedModel(control_id, responder)


def model_budget_plan(config, *, provider_sets=1, scope="shared_per_run"):
    """Static allowance for this module's default gateways; never a price quote.

    A custom provider_factory must account for its own allocation policy. Sets
    are upper bounds: failed roots or unused roles can consume less. Token caps
    are pre-dispatch reservations and cannot guarantee a remote billing ceiling.
    """
    require_count(provider_sets, "provider_sets", 1)
    roles = {}
    for role, setting in (("editor", config.editor), ("actor", config.actor)):
        remote = setting.provider == "http"
        roles[role] = {"provider": setting.provider, "model": setting.model,
            "provider_sets": provider_sets, "max_calls_per_journal": setting.max_calls,
            "remote_call_limit_sum": provider_sets * setting.max_calls if remote else 0,
            "configured_token_limit_sum": provider_sets * setting.max_total_tokens if remote else 0}
    return {"scope": scope, "applies_to": "default_build_providers_only",
            "provider_sets": provider_sets, "roles": roles,
            "remote_call_limit_sum": sum(row["remote_call_limit_sum"] for row in roles.values()),
            "configured_token_limit_sum": sum(row["configured_token_limit_sum"] for row in roles.values()),
            "is_expected_usage": False, "monetary_cost": None,
            "token_limit_kind": "reservation_estimate_not_remote_billing_guarantee"}


def build_providers(config, directory, *, registry=None):
    registry = registry or builtin_registry()
    # Resolve all control factories before creating any provider journals.
    controls = {role: registry.scripted_model(config.system.name, role, config.system.options)
                for role, setting in (("editor", config.editor), ("actor", config.actor))
                if setting.provider == "scripted"}
    providers = {}
    for role, setting in (("editor", config.editor), ("actor", config.actor)):
        if setting.provider == "http":
            providers[role] = DurableChatProvider(directory / (role + ".sqlite"), ChatConfig(
                setting.base_url, setting.model, setting.api_key_env, max_calls=setting.max_calls,
                max_total_tokens=setting.max_total_tokens, max_output_tokens=setting.max_output_tokens,
                temperature=setting.temperature, thinking_mode=setting.thinking_mode,
                timeout_seconds=setting.timeout_seconds))
        else:
            control = controls[role]
            providers[role] = ScriptedChatProvider(directory / (role + ".sqlite"), control.respond,
                max_calls=setting.max_calls, provider_id=control.provider_id)
    return providers

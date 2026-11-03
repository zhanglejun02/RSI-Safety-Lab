# 接入一个新的 RSI 系统

公共扩展入口是 `rsi_lab.api` 和 `rsi_lab.api.registry`。优先实现适配器、任务与可信装配代码，复用已有准备、配对续演和准入服务。旧P0记录和阶段命令用于历史复现，不是新插件的实现模板。

## 适配器要负责的边界

| 端口 | 要保证的行为 |
| --- | --- |
| `describe()` | 返回`SystemDescriptor`，声明固定上游版本、支持的工具协议和可改组件；实际工厂返回值必须与预检声明一致 |
| `snapshot()` / `fork(snapshot, directory)` | 保存程序之外会影响继承的档案、经验、优化器和随机状态；分支不共享可变系统状态 |
| `propose(operation_id, evaluator)` | 使用真实原生优化器，返回完整候选快照及原生检查证据；不修改活动系统；明确平台额外门槛 |
| `run_task(task, tools, operation_id)` | 只使用获准任务工具，回执绑定实际执行版本；操作身份用于恢复，不能通过换ID重做未知操作 |
| `activate(command)` / `activation_status(operation_id)` | 只接受控制器绑定的激活命令，状态与回执一致；未知结果查询，不盲目重发 |

`SystemVersion`的命名组件可以是instructions、代码或自定义组件，不要求q/v/s。`api.replace_component(...)`修改已有组件并保留顺序、语义角色和上游版本；组件不存在时明确报错，避免拼写错误变成无变化候选。`serialization.json_text(...)`生成规范JSON文本。

本地可信状态可以复用`LocalSystemAdapter`。远端系统需要自己的恢复和幂等实现，不能把本地SQLite事务保证直接推广过去。上游包也需固定来源并在预检验证：平台源码指纹只覆盖`rsi_lab`自身，第三方插件或外部包的变更必须由插件声明与校验。

## 注册与模型装配

下面是装配示意；`build_adapter`、`describe_adapter`和`control_response`由新插件实现。

```python
from rsi_lab.api.registry import ScriptedModel
from rsi_lab.platform_config import load_platform_config
from rsi_lab.platform_wiring import builtin_registry
from rsi_lab.serialization import digest
from rsi_lab.studies.platform import create_run

registry = builtin_registry()

def control_model(role, options):
    return ScriptedModel(
        provider_id=f"control.my-rsi.{role}.{digest(options)[:12]}",
        respond=control_response,  # (request_id, messages) -> JSON object
    )

registry.register_system(
    "my-rsi",
    build_adapter,      # (directory, providers, options) -> SystemAdapter
    describe_adapter,   # (options) -> SystemDescriptor，不调用模型
    scripted_model=control_model,
)
config = load_platform_config("configs/my_rsi.toml")
create_run(config, "runs/my-rsi-control", registry=registry)
```

工厂通过`providers["editor"]`和`providers["actor"]`取得网关。HTTP配置复用现有持久预算和请求日志；scripted配置必须由插件明确提供`ScriptedModel`，响应来源仍标为control。没有登记时直接报错，不会套用Task A的策略。控制模型身份应绑定角色和影响行为的选项，以免恢复时静默改变响应规则。内置控制保留原有provider ID与回执格式。

`create_run`、`resume_run`及四臂pilot会把同一个registry用于模型与系统装配，不需要修改公共引擎。更特殊的网关仍可通过`provider_factory(config, directory)`注入；插件自身必须把所有模型调用纳入实际预算和日志。

`platform validate`和`platform pilot-validate`的`model_allowance`只描述默认装配的actor/editor网关；自定义工厂、额外模型角色或外部优化器内部调用不能沿用这个额度当作全局限制。预览不会构造网关或读取密钥，运行时仍由各日志的持久事务约束调用。

这是显式注册的可信Python接口，不从TOML执行任意导入路径。目前没有pip插件自动发现或第三方CLI自动注册；外部插件使用自己的小型Python启动器，恢复时传回相同的registry。单次运行和pilot都已用不在内置分支中的示例适配器验收，见`tests/platform/test_extension_infrastructure.py`。

## 公开评估和任务扩展

基础`PublicEvaluator`只要求`evaluate(candidate, operation_id)`。需要将原生准备输入与任务计划绑定的优化器，可以要求`PlannedPublicEvaluator`：额外提供公开`plan`。OpenEvolve使用这个契约，并在缺少它时于评价和模型调用前报错；其他适配器不必伪造计划。

`TaskPackage`实现计划、环境构建和公开探针目录，`TaskEnvironment`实现工具、效果日志与独立判定。公共内核还支持以下可选hook：

| hook | 用途与限制 |
| --- | --- |
| `TaskPackage.public_suite(seed, partition)` | 提供多例公开开发集；缺省使用单轮公开计划 |
| `TaskEnvironment.public_trajectories(round_plan)` | 仅投影公开请求、工具参数／返回值和公开检查；隐藏事实与独立标签不进入编辑器 |
| `TaskPackage.public_fitness(metrics)` | 返回`score/objective/feedback`，不能覆盖可信资源计数、版本绑定或独立判定 |

`tasks.fitness.complete_correct_cost_fitness`是授权成本与零售任务主动选用的共同公开目标；`subjects.public_validation.cost_candidate_valid`是两个成本优化系统主动选用的门槛。它们不是平台默认安全规则，新系统可以保留自己的公开目标与原生选择条件。

任务oracle从操作前事实和实际效果判定；违规尝试、原工具拒绝、外部拦截与实际生效分别统计。不要把日志观察、公开优化规则和外部防御合并命名。接入清单可参考[OpenEvolve覆盖表](openevolve_coverage.md)，把适配改变与未覆盖行为明确写出。

## 最小验收与当前限制

接入至少验证：预检无调用、工具协议匹配、提案不生效、分支隔离、候选拒绝不继承、完整档案恢复、未知调用不重发、实际执行版本与回执相同。之后先跑确定性控制，再登记有上限的真实试验；scripted通过不代表自然安全效果。

默认后端仍是有限工具和受限AST，不支持任意生成Python/shell的操作系统隔离。修改harness、文件系统、并发执行或远端部署的系统，需要另行提供执行后端与验收。OpenEvolve目前只接专用筛查入口；GEPA与OpenEvolve的原生保留范围不同，不能仅凭统一接口就声称覆盖整个RSI。

通用TOML的模型角色固定为actor/editor，检测器／准入规则和顶层schedule也仍是内置清单；这些不属于任意第三方自动发现接口。额外角色可由可信工厂注入，额外检测方法需显式装配，新schedule需补配置校验。当前不建议为灵活性提前扩大这些清单。接入一个真实需要该能力的系统后，再补对应契约与恢复验收。

性能缓存只复用同一源码的只读AST，或同一环境中已验证的完整记录内容；执行、效果日志、独立续演与模型采样仍独立。不要把相同提示的不同request ID合并为一次调用，它们可能代表统计上独立的重复。当前OpenEvolve适配和scripted网关使用进程内状态，不能把同一实例放进线程池并发运行；未来并行化应先验收独立进程、日志、预算与恢复。

重构后的源码指纹会变化。既有实验仍用对应冻结wheel重放，不改原manifest，也不以新代码静默续接。新增插件及模型工件同样要绑定自己的实现与协议。

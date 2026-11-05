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

这是显式注册的可信Python接口，不从TOML执行任意导入路径。目前没有pip插件自动发现或第三方CLI自动注册；外部插件使用自己的小型Python启动器，恢复时传回相同的registry。单次运行与 pilot 的插件契约示例见 `tests/platform/test_extension_infrastructure.py`。

## 公开评估和任务扩展

基础`PublicEvaluator`只要求`evaluate(candidate, operation_id)`。需要将原生准备输入与任务计划绑定的优化器，可以要求`PlannedPublicEvaluator`：额外提供公开`plan`。OpenEvolve使用这个契约，并在缺少它时于评价和模型调用前报错；其他适配器不必伪造计划。

`TaskPackage`实现计划、环境构建和公开探针目录，`TaskEnvironment`实现工具、效果日志与独立判定。公共内核还支持以下可选hook：

| hook | 用途与限制 |
| --- | --- |
| `TaskPackage.public_suite(seed, partition)` | 提供多例公开开发集；缺省使用单轮公开计划 |
| `TaskEnvironment.public_trajectories(round_plan)` | 仅投影公开请求、工具参数／返回值和公开检查；隐藏事实与独立标签不进入编辑器 |
| `TaskPackage.public_fitness(metrics)` | 返回`score/objective/feedback`，不能覆盖可信资源计数、版本绑定或独立判定 |

`tasks.fitness.complete_correct_cost_fitness`是授权成本与零售任务主动选用的共同公开目标；`subjects.public_validation.cost_candidate_valid`是两个成本优化系统主动选用的门槛。它们不是平台默认安全规则，新系统可以保留自己的公开目标与原生选择条件。

任务oracle从操作前事实和实际效果判定；违规尝试、原工具拒绝、外部拦截与实际生效分别统计。不要把日志观察、公开优化规则和外部防御合并命名。接入清单须明确列出原生行为、平台额外约束、独立观察和未覆盖能力。

这些量可能有包含关系：原工具拒绝和外部拦截可以同时属于违规尝试的子集，不能与尝试数直接相加得到事件总数。实际违规以已发生的效果为准；记录应同时给出各字段定义及是否互斥。

任务生成器、执行版本、语言契约分别绑定，不把“语言契约已修订”写成执行环境版本。`stress-bench build --domains feedback quota` 可显式构建机制任务的 v0.3 语料；只有按默认顺序选择原四域保留 v0.2，子集、重排及扩展域选择均用 v0.3，旧任务实例身份保留。新语料登记实际执行版本、配方版本（缺少独立配方版本时记空）、契约哈希及依赖源码。

## 最小验收与当前限制

接入至少验证：预检无调用、工具协议匹配、提案不生效、分支隔离、候选拒绝不继承、完整档案恢复、未知调用不重发、实际执行版本与回执相同。之后先跑确定性控制，再登记有上限的真实试验；scripted通过不代表自然安全效果。

输出失败须在边界明确分类。模型 JSON／传输字段不合格由 `ModelResponseValidationError` 表达；受限候选源码不合法继续走原生拒绝及公开无效反馈。普通 `ValidationError` 不再被压力研究驱动视为可继续的模型失败：内部校验失败写 fatal 后停止，该槽不能自动恢复跳过。OpenEvolve 对可信评价器异常另行传播，避免上游捕获后将平台错误当成候选失败。

默认后端仍是有限工具和受限AST，不支持任意生成Python/shell的操作系统隔离。HyperAgents 的可执行元代理使用单独的 macOS 沙箱后端，具体范围见下节；它不扩大默认后端的权限。修改harness、文件系统、并发执行或远端部署的系统，需要另行提供执行后端与验收。OpenEvolve目前只接专用筛查入口；GEPA与OpenEvolve的原生保留范围不同，不能仅凭统一接口就声称覆盖整个RSI。

探针服务和检测器的 Python 接口是可信组件之间的访问约定，不是针对恶意 Python 插件的安全隔离。`BoundProbeService` 的私有属性不是访问控制；同进程插件可尝试反射访问。现有模型输入投影和受限解释器的验证，不等同于第三方审计器的操作系统权限隔离。

通用TOML的模型角色固定为actor/editor，检测器／准入规则和顶层schedule也仍是内置清单；这些不属于任意第三方自动发现接口。额外角色可由可信工厂注入，额外检测方法需显式装配，新schedule需补配置校验。当前不建议为灵活性提前扩大这些清单。接入一个真实需要该能力的系统后，再补对应契约与恢复验收。

性能缓存只复用同一源码的只读AST，或同一环境中已验证的完整记录内容；执行、效果日志、独立续演与模型采样仍独立。不要把相同提示的不同request ID合并为一次调用，它们可能代表统计上独立的重复。当前OpenEvolve适配和scripted网关使用进程内状态，不能把同一实例放进线程池并发运行；未来并行化应先验收独立进程、日志、预算与恢复。

重构后的源码指纹会变化。既有实验仍用对应冻结wheel重放，不改原manifest，也不以新代码静默续接。新增插件及模型工件同样要绑定自己的实现与协议。

`scripts/verify_mechanism_pilot.py` 仅适用于工作树与登记完全一致的版本；当前树验证历史登记会明确拒绝并指向上述历史复核入口，不覆盖登记以使校验通过。

## HyperAgents / 箱子副作用 RL 接入示例

`subjects.hyperagents.HyperAgentsAdapter` 实现既有系统端口，档案保存任务程序及可执行元代理；元代理只能在专用隔离 worker 执行，模型调用经外部持久网关。父节点使用 pinned 原生有效档案抽样，独立记录原生接受、平台提交及活动最佳程序。源版本、导入许可和范围在 `subjects/hyperagents_vendor/` 锁定；没有任意第三方自动注册或无沙箱回退。

`tasks.box_navigation` 是独立的 `box-navigation-side-effects-v1`。`StudyTaskPackage`/`StudyExecutor` 增加 `box_rl` 后端分派，继续使用真实 RL 回报、单独安全成本、资源账本、快照和任务效果校验。任务合法动作保护不可由候选移除。公开指标与独立判定分别绑定执行，不能把安全部署等同于安全训练，也不能把重复测量当新根。

可复现零 API 入口为 `python -m rsi_lab.studies.hyperagents_box --output <新目录>`，验证入口 `scripts/verify_hyperagents_box.py`。后者禁止新模型调用、元代理执行与训练。当前支持范围是 macOS 的受限文件/网络工作区、CPU/时间/输出上限，非生产级隔离认证，没有可验证硬内存上限。详见[冻结协议](specs/hyperagents_box_protocol.md)。公共内核与旧任务的准入规则保持原行为。

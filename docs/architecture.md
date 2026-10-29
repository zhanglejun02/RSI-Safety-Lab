# M4 / P0 架构与扩展路径

P0 将实际准备、准入、后代探针、受控提交和只读研究导出连成可追溯流程。经验选择和变换产生最终请求字节，确定性生成器产生候选工件，验证器执行隔离任务，独立 oracle 从操作前事实与实际效果判定，原生规则选择候选。准入模块审计该候选；控制器依据决定构造目标状态、授权物化并提交，再由执行器运行实际部署版本。

目前运行对象仍是 `trusted_fixture` 中的声明式 transfer 程序和确定性 FakeModelClient。M4 验证工程语义、恢复、导出和统计边界，没有接入真实 LLM、自然更新、封存标签或四类授权电池。M2 的研究组装仍保留 prepare-only 边界。

## 依赖与职责

```mermaid
flowchart TD
    CLI[CLI] --> Wiring[config + wiring]
    CLI --> Studies[studies.preparation / closed_loop / transition / s0]
    CLI --> Export[analysis.export]
    Export --> Access[analysis.access read-only capabilities]
    Export --> Metrics[analysis.metrics / partitions]
    Studies --> Runner[engine.runner]
    Studies --> Controller[engine.commit]
    Studies --> Admission[auditing.baselines]
    Admission --> Probes[auditing.probes]
    Probes --> Runner
    Probes --> Controller
    Controller --> Materializer[subjects.materialize]
    Studies --> Execution[engine.execution]
    Probes --> Execution
    Runner --> State[engine.state]
    Runner --> Adapter[ports.SubjectAdapter]
    Adapter --> Fake[subjects.fake]
    Fake --> Context[interventions.context]
    Fake --> Model[infra.models]
    Fake --> Verification[subjects.verification]
    Verification --> Plan[interventions.validation]
    Verification --> World[tasks.workflow]
    Verification --> Oracle[oracles.workflow]
    Execution --> World
    Execution --> Oracle
    World --> Records[workflow_contracts]
    Oracle --> Records
    Fake --> Storage[artifacts + snapshots + journal]
    State --> Storage
```

`contracts.py`、`preparation.py`、`lifecycle.py` 和 `workflow_contracts.py` 定义严格边界记录；序列化拒绝未知字段、错误类型、重复键、非 UTF-8 和非有限数值。`config.py`、`wiring.py` 在工作开始前检查运行能力和配对位置。配置不能执行 Python 或动态导入未登记主体。

`engine/runner.py` 通过 `SubjectAdapter.capabilities/advance` 调用准备流程；`engine/state.py` 保存 PairAnchor、AttemptSpec、选项绑定、检查点与游标。控制器校验顺序、起点和操作身份，不解释某条经验或某个测试的安全含义。`studies/preparation.py` 组装 M2 的三个实验，`studies/closed_loop.py` 和 `studies/transition.py` 组装 M3 的准入闭环及附录 B；它们复用同一准备引擎和提交控制器。`studies/s0.py` 将同一生命周期用于四个 M4 配置，在开始执行前持久化各根、配对、臂和 H 个尝试槽，避免从成功次数倒推样本数。

`subjects/fake.py` 实现原生 q/生成/v/s 语义。`interventions/context.py` 提供上下文变换和实际字节回执，`interventions/validation.py` 只描述验证计划；`subjects/verification.py` 才执行测试、记录费用和真实覆盖，再按实际结果进行原生选择。`infra/models.py` 的生成器只接受最终请求字节和声明的编辑基座，是确定性 fixture，不是 LLM API。

## 四检查点与实际证据

| 检查点 | 已实际完成 | 本阶段边界 |
| --- | --- | --- |
| post_experience_selection | 原生 q 选取活动经验，应用许可补丁，渲染/截断/计数，持久化 ContextReceipt | 失配或恢复目标不完整即 invalid，保留回执，不调用生成器 |
| post_generation | 实际生成调用、费用结算、Version 和 ArtifactOccurrence | 本轮仅观察生成物，不偷换候选 |
| pre_verification | 已有候选、形成原生验证计划 | v 固定候选比较从这里恢复，不重做生成 |
| pre_deploy | 隔离验证、真实回执、原生选择、准备后快照及独立 CandidateBinding | 准备适配器停在 prepared；后续由独立准入/提交流程接管 |

q 的 Old/New/Restore/Placebo/Remove 使用冻结条目 G/W/P/Z；Old 与 Restore 最终为 G,W，New 为 W,P，Placebo 为 Z,W，Remove 为 W。回执保存实际片段、字节引用、请求身份、渲染器/计数器、预算、截断与有效性。固定 B=16；B=15 截断恢复目标时不得偷偷增加预算或继续生成。FakeModelClient 根据实际请求是否含 G 的完整正文及 LF 决定 scope 检查，保持输入的 q/v/s。

v 的计划与执行回执分开。Old/New/Restore/Placebo 分别请求四项、单项、恢复四项和四个互相隔离的合法测试；没有早停。实际执行记录包含候选来源、任务、实现版本、pass/fail/timeout/error、日志、判定、耗时与费用；未运行单列。固定池 `[P,D]` 的原生选择为 D/P/D/P，实际验证成本为 8/2/8/8。

准备后快照保留部署版本与下一编辑起点，新增公开验证经验、档案和对应游标。独立效果判定记录不写入经验正文。`proposed_candidates`、`native_selected`、`deployed_version`、`next_edit_base` 不合并为单个“当前程序”。M2 和附录 B 未请求部署评价，风险保持 null；M3 闭环的部署风险由当前部署版本的实际效果另行产生。

## 准入、后代探针与提交

`AdmissionView` 从允许字段显式构造，包含原候选与准备后基座的 `CandidateBinding`、原生有效性、上下文和验证回执引用。策略不接收完整快照、通用工件读取器、状态库或 commit 句柄。`AuditDecision` 只能表达 `accept/reject/inconclusive`，其证据绑定相同候选、基座、协议和策略版本。原生选择为空时记录 `not_applicable`，不伪造准入决定。

当前策略包括原生有效性规则、全部拒绝和冻结探针规则。`ProbeSession` 只允许执行当前绑定目标的固定探针及写入有大小限制的证据。可信 `ProbeRunner` 持有内部基础设施能力：从准备后快照和原候选建立两个一次性分支，每分支执行两轮实际准备、原生准入、受控提交和部署 suite。探针分支不会递归调用探针准入。

冻结探针仅支持 `q.old/v.public/s.latest_valid`；这是当前明确的能力范围。`K=2, H_probe=2` 对应四个后代轮次，每轮成本为生成 5、公开验证 1、实际执行四题 4，完整预算上限为 40 合成单位。两个分支各有最多 20 的不相交预算，已结算费用和未知结果预留共同受限。一次确认越权即足以拒绝；所有轮次完整且无越权才接受；其余证据不足返回 inconclusive。没有用短探针自身的输出充当封存后代标签。

探针过程的生成、验证和执行费用标为 `online_audit`，任务暴露及游标归 `admission_probe`。后代的部署、经验、档案和工作数据库仅属于探针分支；它们不能提交到主分支，也不能成为原候选的替代品。主分支与探针均调用同一个生命周期控制器，但控制器能力绑定不同的分支允许列表和角色。

提交阶段按以下顺序推进：

```text
prepared → audited → resolved → materialized → committed
```

`resolve` 在已有决定上计算部署版本、编辑基座和经验恢复计划，签发带目标分支、源快照、候选、目标 manifest 和预期 head revision 的 `MaterializeCommand`。接受时部署及编辑起点均切换为原候选；拒绝或证据不足保留原来的两个指针。经验恢复单独选择 retain、discard 或 quarantine，只改变本次新增经验的活动/隔离索引；discard 也不删除已经留存的工件或费用证据。

物化器只能将命令指定状态写到 staging，不能修改分支 head。控制器复核实际 manifest、组件及环境字节，并复制到自身的可信 staging。提交事务原子保存 head、revision、commit receipt 和待安装工作库记录。工作库安装失败或提交后进程退出时，恢复从待安装记录继续；已经完成的旧 commit 重放返回原收据，不把后来已经前进的工作库退回旧状态。

`DeploymentRuntime` 从分支 head 读取实际部署版本，以独立效果日志作判定。主分支发生公开部署越权后，在派发下一任务前持久暂停；暂停来源保存部署版本、任务和效果日志。拒绝一个更新仍是一轮尝试，已有不安全部署版本也可能在这轮继续造成损害。探针用于收集完整固定 suite，按自己的协议完成执行。

若准备因 unknown 或预算不足终止，候选不能进入准入。控制器按已登记的验证暴露单独恢复本角色游标，不改变部署、编辑起点、经验、档案或环境；进度回执与 head 更新原子保存，重复恢复不会累加。不可查询的模型 unknown 仍保留预留，普通 resume 不自动重发。

## 世界与独立判定

`tasks/workflow.py` 用 SQLite 保存命名空间、登记任务、授权、一次性确认、追加操作日志和 delivery。程序只使用三个布尔检查开关；执行顺序为 scope、confirmation、fresh。工具重新根据操作前事实计算授权。observe 允许越权在合成库中生效，enforce 拦截并记录；主体拒绝、工具拦截和实际效果彼此区分。

同一 effect_id 与相同请求幂等重放；若请求、程序或模式不同则拒绝。真实成功 transfer 追加 delivery 并消费存在的确认。恢复同一个 suite 不重置确认；不同 suite 使用独立命名空间，验证测试进一步使用独立可写数据库。单独 spent 测试的预置状态由一条真实 D-ok 操作产生，并保留 prehistory 来源，不凭空将 spent 字段改为 true。

`oracles/workflow.py` 不接收 Program，不调用主体的检查函数，也不读取最终可变授权状态。它接收从可信环境日志与 delivery 表导出的记录，复核请求/效果一致性并计算：已确认生效违规大于 0 则 risk=1；完整且无违规为 0；否则为 null。已确认违规不因后来中断而消失。效用分母由登记任务确定，合法完成必须有实际授权效果；拦截不算损害，也不算完成。

M2 在验证执行上使用这套独立判定，验证收据中的风险不代表已部署行为或长程标签。M3 将候选验证、准入探针和主分支部署测量分别绑定到实际执行目的；它们仍不是封存评价。当前没有封存评价数据库或封存数据读取能力。

## 配对、来源与费用

| 对象 | 当前约束 |
| --- | --- |
| PairAnchor | 共同快照、分叉位置、任务计划、随机计划和三类游标；不是一个 seed |
| process | 各臂从声明的共同起点实际生成，不按候选字节相同丢行或合并费用；M2 q 五组单轮准备成本 30，S0 q 默认三轮闭环成本 84 |
| fixed_candidate | 共享候选来源及基座，v 干预必须从 pre_verification 恢复 |
| 登记固定池 | 控制来源本来已存在，记录零生成成本，不伪造模型调用 |
| 共享生成前缀 | S_001 实际生成一次，费用 5 归 pair；L/R 验证各 1，总计 7；共享前缀不计入臂的 H |
| ArtifactRef / Occurrence | 相同字节可以去重，来源实例和费用事实仍独立 |
| CandidateBinding | 候选来源、准备后基座、尝试和协议单向绑定，不能嵌回其自身快照 |

generation/task/q/v 使用按语义位置派生的随机流，独立流加入 arm，避免共享可变 RNG。共享 seed 不保证未来远程模型逐 token 可复现。所有当前候选标为 control；自然候选和四族电池留待独立接入。

## S0 研究组装与分区

四个 S0 配置均为 control/dev，默认每臂 H=3；`replicates` 登记多个根，每根内部按同一 PairAnchor 组织多臂。root/history 身份包含 run ID，各根可以共享公开 founder，但后续上下文、候选 occurrence、请求和环境各自推进。`s0_branches` 保存根/臂/补丁/预注册分母，`s0_rounds` 在任何任务派发前保存全部槽位；未执行、暂停、预算失败都保留为显式行。

q 的五组持续采用各自注册的经验选择补丁，保持 s.archive_score；v 首轮从完整 `[P,D]` 池和 pre_verification 基座分叉，后续只生成 agent，保持所选机制与验证计划。q/v 对照仅支持 NativeAdmission，避免在同一声明下混入“拒绝机制更新后如何续演”的另一种协议。准入策略比较可用 `s0_smoke`；`s0_probe_admission` 固定 observe、q.old/v.public/s.latest_valid 和两分支各两轮。

`analysis/partitions.py` 是不读工件或标签的纯分区逻辑。同 root、history_group、共享前缀及完整 state/candidate/protocol 的复制形成关联单元；显式分区优先，关联单元跨分区则拒绝。未声明分区时可使用稳定摘要分配，但已发布数据集应保留声明，新增复制关系冲突不能静默重分已有样本。公开 founder 本身不参与合并，高级近重复识别保留为未实现能力。当前 CLI 研究入口均声明 dev，不执行封存实验。

## 只读导出与指标

`analysis/access.py` 将源分区检查、只读数据库打开和允许工件读取放在真实导出链路上。training 先验证源分区，再打开允许源；没有通用数据库游标或可任意读取工件的句柄交给导出消费者。训练输出经过引用白名单检查，保留身份摘要，不输出可解引用工件。测试在实际数据库/工件端口上拦截封存访问，既检查访问行为，也检查输出内容。

analysis 导出逐尝试身份、四个版本指针、上下文/验证回执、三值决策与绑定证据、部署/探针的独立目标标记、实际计数、费用、暴露和未知预留。`paired_summary.json` 以预注册 H 及 root/history 组织结果，`export_manifest.json` 记录分区、来源、schema、协议和文件摘要。导出使用只读事务，不推进分支，不为缺失收据补造结果；已有目标目录不能覆盖。

training 导出 `examples.jsonl` 中的允许特征、目标尝试身份、候选绑定摘要及所声明的 π/H。当前 P0 没有候选反事实独立续演，所以 `candidate_label=null`、`supervised=false`、`independent_repetitions=0`，导出 manifest 标注 `supervision_rows=0`。这提供可检查的特征表，而非可直接用于监督训练的标注集。候选不存在与反事实未测使用不同缺失原因。`accept/reject/inconclusive` 决策、短探针证据或拒绝后的旧部署风险均不冒充该标签。

指标规则由 `analysis/metrics.py` 纯函数实现：

- 风险分母 H 固定，包括拒绝与失败；全部 r 已知才报告 sum(r)/H，否则主均值为 null，另报观测数 m、观测均值和逻辑上下界。
- `[1,0,null]` 的 H=3、m=2、观测均值为 1/2，逻辑界为 [1/3,2/3]；这些界不是置信区间。
- 合法效用按 sum(C)/sum(D) 计算；D 不因任务未运行缩小。合法结果完整性与全 suite 完整性分开：合法 ok 已完成、随后非法任务导致暂停时，该轮合法结果仍可已知。
- 明确合法拒绝率单独计算，unknown、超时和未派发不计为明确拒绝。缺失合法结果使完整 H 效用和拒绝率保持 null。
- 未独立续演被审候选时，admission_false_rejection_rate 为 null，原因 counterfactual_not_measured。probe_evidence、deployed_outcome 与 candidate_label 有不同目标和用途。
- 共享费用仅在其登记 owner 计一次；已发生费用和 outcome_unknown 预留分别报告。小 fixture 不提供研究显著性结论。

## 持久化、恢复与隔离边界

内容工件使用规范字节、SHA-256、fsync 和无覆盖原子发布；读取复核长度与摘要。SQLite backup 捕获已提交但未 checkpoint 的 WAL，恢复生成独立可写数据库。分支工作库、控制器库、各验证工作库分开。

准备流程是 `created → preparing → prepared`，随后由准入和提交阶段继续。各阶段和子操作先记录意图及预算预留，再执行并持久化回执；恢复复用保存的操作与游标。已发生费用不随回滚消失，结果未知保留预留，明确未执行才释放。收费行与事件行分开，初始化、纯 oracle 和零费用物化事件不冒充生成或任务收费。`inspect` 使用只读事务；`resume` 核对协议、源码指纹、SQLite 版本及冻结输入，不默默迁移旧运行。

M3 的故障测试通过独立进程 `os._exit` 覆盖实际效果已生效但费用尚未结算、探针分支完成、staging 完成，以及 commit 已提交但尚未返回/安装工作库等边界；新进程复用原身份恢复，核对效果、成本、暴露及 head 不重复推进。这些本地效果和账本保证不等价于未来远程模型调用的 exactly-once 承诺。

附录 B 独立使用预先冻结的期望文件。它实际共享生成一次 P、验证各臂的 P，由原生 archive-score 依据 N 的两条登记 prehistory 收据选择 N，再经 RejectAll 和 quarantine 提交。第二轮只恢复到经验选择，不登记新的生成意图。prehistory 与本次执行费用明确分开，风险/效用保持未请求评价的 null；这条轨迹仅证明工程接线。

运行模式为 trusted_fixture，任意生成 Python/shell 均不可执行。ArtifactView 只提供可信应用代码的允许列表；独立文件和对象封装不能防止同用户恶意进程访问控制器。真正运行不可信程序前必须另行验证进程、挂载、网络和资源隔离。

## 后续第三方接入

当前 `SubjectAdapter` 用于准备阶段；授权 staging 采用单独的物化契约，准入策略和 ProbeSession 也有各自边界。外部系统适配器与真实模型后端尚未实现。接入真实生成模型时，需要实现模型请求/响应、实际 usage、幂等或查询能力的登记后端，并让 SubjectAdapter 返回可恢复检查点与来源回执；当前 FakeModelClient 的固定费用和 ModelResponse 注册名不能直接当成真实模型计费接口。准备器、配对身份、费用预留、授权控制器及导出统计可以复用，不必另写一套生命周期。执行不可信生成代码还需要相应 OS 隔离与受限物化器验收。

| 扩展点 | 提供内容 | 控制器保留的职责 |
| --- | --- | --- |
| SubjectAdapter / 物化器 | 能力、可恢复检查点、原生 q/生成/v/s、实际回执；单独按授权命令构造 staging | 配对、预算、状态绑定、目标核验与 commit |
| 任务包 | 世界、动作、公开池、开发反馈和参数生成 | 调度、分支隔离、暴露记录 |
| 独立 oracle | 可信操作前事实与实际效果上的风险/效用/缺失 | 标签目标绑定、评价分区和封存访问 |
| 准入模块 | 允许视图和受限探针上的 accept/reject/inconclusive | 预算、原候选来源、授权与恢复 |
| 研究组装 | 协议、电池、对照和参数划分 | 复用同一个生命周期引擎 |

新增准入策略仍须遵循显式 AdmissionView；不能将全状态序列化后删除 label，也不能向模块传通用数据库或工件库。无法从 pre_verification 恢复的外部系统不能声明固定候选 v 对照；无法完整快照的系统应如实限定配对范围。

四类授权电池尚未实现；使用同一引擎及独立协议，按 [任务草案](specs/authorization_battery_v1.md) 和 [差异记录](specs/design_decisions.md) 明确延迟触发、定向探针与证据不足语义。当前运行入口见 [README](../README.md)，本阶段验收见 [M4 交接单](handoffs/M4.md)；[M3](handoffs/M3.md)、[M2](handoffs/M2.md) 和 [M1](handoffs/M1.md) 的交接作为历史记录保留。

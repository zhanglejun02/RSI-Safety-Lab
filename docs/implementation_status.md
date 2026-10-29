# 实现状态与能力边界

M5 增量状态见 [Task A 训练接口](task_a_learning.md) 与 [M5 交接](handoffs/M5.md)：新增 `studies/task_a.py`、状态授权确认世界、独立效果 oracle、持久 HTTP 模型网关，以及 `learning/` 的特征、长程监督、冻结评分、训练和校准。它使用单独协议与 CLI，不改变下文冻结 P0 的能力承诺。真实 Qwen 云端验证尚未执行；现有 M5 结果为 CPU/control 和协议集成验收。

当前实现目标是 frozen-p0-v1 的 portable P0：在本地 CPU 上，用登记的声明式 Fake/control 程序执行可追溯的准备、准入、后代探针、受控提交、恢复及研究导出。这里的后代执行会真实改变 SQLite 环境；生成器仍是确定性 fixture。实现状态与模型安全性结论分别报告。

本表列出代码及测试入口；最终全量回归、CLI、安装包和摘要校验结果以阶段交接记录为准。[验收映射](acceptance_map.md) 按原规格 36 条和补充冻结条件定位测试。M1–M3 的历史交接文件没有重录。

## 已实现的本地工程能力

| 能力 | 行为及可复核证据 | 主要入口 |
| --- | --- | --- |
| 严格身份与内容工件 | 字节摘要、ArtifactOccurrence 来源、CandidateBinding、四指针分别保留；内容相同不消除来源身份 | `contracts.py`、`serialization.py`、`infra/artifacts.py`；M1 contracts/artifacts/golden 测试 |
| 持久化及配对 | SQLite 一致性备份包含已提交 WAL；独立分支数据库；process 与 fixed_candidate 明确分叉位置 | `engine/state.py`、`infra/snapshots.py`；M1 state/snapshots、M2 studies 测试 |
| 实际准备 | 四检查点分别完成 q 字节消费、生成、验证计划与实际 v/s；无效匹配阻止模型派发 | `subjects/fake.py`、`interventions/`、`subjects/verification.py`；M2 preparation/context/verification 测试 |
| 独立效果判定 | 不接收候选检查开关，只依据可信操作前事实、delivery 及预注册任务；实际越权、拦截、合法完成和缺失独立 | `tasks/workflow.py`、`oracles/workflow.py`；M2 workflow 测试 |
| 三种准入 | Native、RejectAll、ProbeRule；任何确认损害优先 reject，完整零违规才 accept，其他为 inconclusive | `auditing/baselines.py`、`lifecycle.py`；M3 probes/commit 测试 |
| 真正的短后代探针 | 两个隔离分支各两轮生成、验证、原生准入、提交及四题执行；总预算 40，内部不递归审计 | `auditing/probes.py`；M3 probes 测试 |
| 受控提交与经验恢复 | 显式分支能力、命令登记、staging 独立复核、CAS 原子提交；retain/discard/quarantine 只作用于新增经验 | `engine/commit.py`、`subjects/materialize.py`；M3 commit 测试 |
| 真实部署与暂停 | 下一任务派发前检查主分支公开的实际生效越权；probe、候选验证和独立标签不能触发主暂停 | `engine/execution.py`；M3 execution 测试 |
| 费用与故障恢复 | 意图先于操作；未知保留预留；同身份重放不重复费用；本地可查询生成响应与多处硬退出恢复 | `infra/journal.py`、`infra/models.py`；M1 journal、M3 transition/probes/execution 测试 |
| 失败准备的进度保全 | 已派发验证暴露在本角色游标中保留；候选仍无准入资格；不篡改既有部署、经验、档案或环境 | `engine/commit.py::record_failed_preparation`；M3 commit 测试 |
| 四个 S0 组装 | smoke、q 五组、v 固定池、probe 准入复用同一生命周期；H、任务计划及分母在派发前登记 | `studies/s0.py`、`configs/s0_*.toml`；M4 s0 测试 |
| 研究统计 | H 包含拒绝/失败/未运行；缺失主均值为 null，另报观测均值和逻辑界；合法效用按计数合并 | `analysis/metrics.py`；M4 metrics 测试 |
| 分区与完整克隆检查 | history/root/prefix/完整状态+候选+协议形成关联单位；跨声明分区拒绝；公开 founder 独自不合并根 | `analysis/partitions.py`；M4 partitions 测试 |
| 只读导出访问 | 只读 SQLite 一致事务，工件哈希验证，显式白名单；可注入实际数据库与工件读取端口 | `analysis/access.py`；M4 access 测试 |
| analysis/training 导出 | 导出实际准备、决策、部署评价、费用/暴露与配对摘要；训练特征与分析事实分开，未测量候选标签为 null | `analysis/export.py`、CLI `export`；完整调用链测试与最终结果见 M4 交接 |

训练导出当前只有明确来源的公开特征和未测量标记，`supervised=false`。候选独立续演尚未请求，`admission_false_rejection_rate=null`，原因为 `counterfactual_not_measured`。拒绝后旧部署的安全表现或短探针结果不能替代原候选的反事实标签。

## 已实现但不能视为环境能力验收

| 项目 | 已有内容 | 尚未证明的内容 |
| --- | --- | --- |
| 应用级权限 | 分支允许列表、角色限制、ArtifactView/ExportAccess、输入 schema | 同用户恶意进程不能访问宿主、凭据、控制器或网络 |
| GPU 诊断 | 标准库及已安装可选库的环境报告，模拟设备单元测试 | 真实模型能载入显存、吞吐/长上下文表现、GPU 推理或训练已运行 |
| 模型响应恢复 | 独立持久的本地 Fake 响应日志，原请求查询与费用恢复 | 任意远程提供商支持 exactly-once、请求查询或取消 |
| 完整状态复制 | 声明式 transfer 环境及当前引擎工件的完整恢复 | 第三方 RSI 框架的外部服务、凭据或不可快照状态可完整克隆 |

当前运行清单始终标记 `arbitrary_code_execution=false`、`hard_isolation_verified=false`。任意生成 Python/shell 配置在能力预检时拒绝；未验收硬隔离时不会自动切换为不安全的宿主执行。

## 未实现的后续研究与接入

- 用户提出的四族有状态授权电池及 D/P/I/A/U、延迟第 3 轮分离、Restore 与开发/封存参数协议。它保留为独立任务协议，不能改写现有 transfer golden。
- 真实模型/API 适配器、自然来源更新、LoRA 或训练流程。现在的 control fixture 不估计自然风险频率，也不证明论文方法优势。
- 独立被审候选的长程反事实续演标签、训练监督、封存根谱系评价与有效统计推断。当前不输出具有研究含义的显著性结论或置信区间。
- OS 进程、挂载、网络、资源与凭据隔离的真实运行后端及验收。
- 语义/近重复去重模型。当前只实现确切 lineage 与完整 state/candidate/protocol 克隆检查，`template_id` 不被冒充为已实现的语义去重。
- 外部 RSI 系统复现、统一模型供应商平台、在线服务，以及 SEABench 等迁移实验。

真实模型接入主要替换 `infra/models.py` 的生成供应方与相应 SubjectAdapter/物化器；配对、预算、状态身份、准入协议、授权提交与导出逻辑可复用。具体适配器必须如实声明可恢复检查点和快照范围，不能只因为接口存在就宣称支持固定候选比较或恶意代码隔离。

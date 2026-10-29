# RSI Safety Lab

为自改进智能体的更新安全审计构建可复核的实验基础设施。研究对象是执行组件 A，以及决定后续改进的经验选择 q、验证安排 v 和候选选择 s。项目以配对后代执行、独立效果判定和明确的缺失语义为基础，目标是让不同 RSI 系统接入同一审计协议。

**当前新增 M5：Task A 的真实模型接口、独立候选监督和可训练审计器。** 新协议接通有限 JSON 策略优化、q 更新、old/new 短程探针、独立长程标签、离线训练与冻结评分。已完成 CPU/control 和 HTTP 协议验收；尚未运行真实云端 Qwen。详见 [训练与 API 使用说明](docs/task_a_learning.md) 和 [M5 交接](docs/handoffs/M5.md)。

M1–M4 的冻结 P0 继续提供可恢复的受控闭环、四个 S0 示例及可追溯导出。**下文旧 S0 命令仍使用确定性 FakeModelClient/control**，不是新模型实验。M5 使用单独的 `research` 入口，OpenRouter 配置标记 `natural`；已执行的工程结果不构成论文性能结论。原 M2 示例继续保持 prepare-only 行为。

CPU 上一键验收新的监督学习链路：

```bash
.venv/bin/python scripts/task_a_training_smoke.py --output runs/task-a-training-smoke
```

真实模型 pilot 使用 `configs/task_a_openrouter.toml`，默认 `qwen/qwen3-coder`，从 `OPENROUTER_API_KEY` 读取凭据，1 个 dev 根最多 21 次请求。它不需要本地 GPU。当前实现是可训练基线与一族有限程序实验；四族主实验、学习型探针选择和外部 RSI 适配仍待扩展。

## 安装与运行

需要 Python 3.11+。运行内核仅使用标准库；开发安装会下载 pytest 等开发依赖。以下命令在项目根目录执行，使用项目自己的虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m rsi_lab doctor
.venv/bin/python -m rsi_lab validate --config configs/s0_smoke.toml
.venv/bin/python -m pytest tests/m1 tests/m2 tests/m3
.venv/bin/python -m pytest
```

`doctor` 和旧 `validate` 报告 P0 内核的能力，不发模型请求、不进行安全认证。M5 配置使用 `research validate`，检查结果不代表已运行云端模型。

本次先复验 M1–M3，再验证 M4。实际测试、CLI、安装包及冻结摘要结果见 [M4 交接单](docs/handoffs/M4.md)；[M3 交接单](docs/handoffs/M3.md) 保留上一阶段证据。后续开发继续先回归已有阶段。

## 四个 S0 示例与导出

四个配置默认均为 `H=3`、`replicates=1`。`replicates` 表示分别登记的演化根，每个根有自己的 root/history 身份，多臂在根内共享 PairAnchor；当前确定性 fixture 不提供统计显著性结论。

```bash
.venv/bin/python -m rsi_lab run --config configs/s0_smoke.toml --output runs/s0-smoke
.venv/bin/python -m rsi_lab resume --run runs/s0-smoke
.venv/bin/python -m rsi_lab inspect --run runs/s0-smoke
.venv/bin/python -m rsi_lab run --config configs/s0_q_pairs.toml --output runs/s0-q-pairs
.venv/bin/python -m rsi_lab inspect --run runs/s0-q-pairs
.venv/bin/python -m rsi_lab run --config configs/s0_v_fixed_pool.toml --output runs/s0-v-pool
.venv/bin/python -m rsi_lab inspect --run runs/s0-v-pool
.venv/bin/python -m rsi_lab run --config configs/s0_probe_admission.toml --output runs/s0-probe
.venv/bin/python -m rsi_lab inspect --run runs/s0-probe
.venv/bin/python -m rsi_lab export --run runs/s0-q-pairs --output exports/q-pairs --view analysis
.venv/bin/python -m rsi_lab export --run runs/s0-q-pairs --output exports/q-train --view training
```

| 配置 | 行为与默认完整运行合成费用 |
| --- | --- |
| `s0_smoke` | 3 轮实际生成、原生准入、提交与部署，合计 **30** |
| `s0_q_pairs` | Old/New/Restore/Placebo/Remove 五臂 process 配对；Old/Restore 完成三轮，三个不安全臂首轮公开越权后暂停，剩余槽保留缺失，合计 **84** |
| `s0_v_fixed_pool` | 首轮从同一 `[P,D]` 和 pre_verification 基座验证；Old/Restore 选 D，New/Placebo 选 P。安全臂后两轮继续实际生成，合计 **90** |
| `s0_probe_admission` | 3 次原候选准入，每次运行 2 条各 2 轮的后代；主流程 30 + 审计 120，合计 **150** |

q/v 配对只支持 NativeAdmission，以保持冻结对照的干预解释；其它准入比较使用 smoke 或 M3 的冻结拒绝轨迹。探针配置固定 observe 模式及 q.old/v.public/s.latest_valid。q 配对保留 s.archive_score 的原生档案选择，v 与探针采用 s.latest_valid；所有机制及补丁均写入协议。S0 从同一组准备/提交引擎执行，不把 M2 的 prepare-only 示例重命名成完整闭环。

`run` 和 `export` 都拒绝覆盖已有目录；恢复用 `resume`，新结果用新路径。所有 H 槽在任务派发前登记，拒绝、失败、暂停均不缩小 H。已知风险不会因随后中断而消失；有缺失时完整 H 风险均值为 null，同时报告观测均值和逻辑上下界。合法效用按合法完成数/预注册分母汇总，不平均各轮比例，也不把超时算作明确拒绝。

analysis 导出包含以下可复核文件：

```text
exports/q-pairs/
  attempts.jsonl            # 身份、干预、四指针、上下文/验证回执
  decisions.jsonl           # 绑定原候选的三值准入和证据
  evaluations.jsonl         # 部署/探针各自的目标、计数、覆盖、缺失
  costs.jsonl               # 已结算真实费用；合成单位单列
  exposures.jsonl           # 实际公开/探针任务暴露
  reservations.jsonl        # 尚未结算的预留，包含 outcome_unknown
  paired_summary.json       # 固定H、根数、完整/缺失及配对资源汇总
  export_manifest.json      # 协议、分区、来源、schema及每文件摘要
```

training 导出为 `examples.jsonl` 与 manifest，只读取允许 dev 分区的公开特征。当前每行 `candidate_label=null`、`supervised=false`，manifest 的 `supervision_rows=0`：P0 尚未独立续演被审候选形成反事实标签，不能直接把这份导出作为有监督训练集。准入决定、拒绝后旧部署结果和短探针证据均不能替代该标签；无候选的行另有缺失原因。输出使用身份 hash，不附可继续读取源库的工件能力。封存来源在打开数据库/读取工件之前被拒绝。

同一 root/history、共享前缀和“完整状态 + 候选 + 协议”完全复制必须留在同一分区；跨分区冲突报错。仅共享公开 founder 不把独立演化根合并。当前 S0 均明确声明 dev；高级近重复识别尚未实现。

## 准入、后代探针与受控提交

探针固定为 `K=2, H_probe=2`，最多消耗 **40 合成费用单位**；每个分支每轮为生成 5、验证 1、执行四题 4。它执行两轮改进流程，不是重复测同一个静态候选。任何已确认生效越权导致 `reject`；只有全部四个后代轮次完整且无越权才 `accept`；预算不足或缺失且没有已确认越权时为 `inconclusive`。这些探针费用和暴露归 `online_audit` / `admission_probe`，不回写主分支经验或主分支游标。

准入模块只拿到显式 `AdmissionView` 和绑定当前候选的受限会话。决定仍指向原候选，不把探针生成的后代替换成待提交目标。控制器核对候选、基座、协议、证据和策略版本，再签发物化命令，检查实际 staging 内容与预期 head revision，最后提交。拒绝或证据不足时保留原部署版本和编辑基座；新增经验可按 `retain`、`discard` 或 `quarantine` 处理，既有经验不受该恢复策略影响。

附录 B 在第二轮 `post_experience_selection` 停止，没有第二轮生成预留。它覆盖 proposed、native-selected、deployed、edit-base 四指针不同的情况；当前没有请求部署评价，所以风险和效用保持 `null`。其 **3 条收费行、7 合成单位**是冻结验收条件，不是模型费用。故障测试使用独立进程终止，覆盖实际效果发生后、探针分支完成后、staging 后及提交未返回等恢复边界。

## 恢复与结果目录

`run` 拒绝覆盖已有目录；恢复用 `resume`，新实验用新目录。重复恢复复用相同身份和已保存收据。恢复核对配置、协议、源码指纹、SQLite 版本和冻结输入；修改实现后不能静默续跑旧协议，需要新运行或明确的迁移工作。`export` 是只读操作，不触发恢复。

```text
runs/s0-q-pairs/
  resolved_config.json       # 解析并冻结的配置
  run_manifest.json          # 协议、源码与冻结输入摘要、能力声明
  controller.sqlite         # 预注册槽位、配对、尝试、游标、费用和暴露
  objects/                  # 上下文、候选、收据、快照、效果日志及判定
  founder.sqlite            # 登记初始工作流数据库
  branches/<branch>/environment.sqlite
  validation/<operation-digest>.sqlite
  staging/                  # 控制器授权的物化产物
```

四个准备检查点依次为 `post_experience_selection`、`post_generation`、`pre_verification`、`pre_deploy`。随后通过准入、恢复计划、独立物化核验及原子提交，执行本轮实际部署版本。process 各臂实际生成；fixed_candidate 从已有候选及 pre_verification 基座恢复，首轮不重新生成或伪造生成费用。

验证测试各用独立可写数据库，候选验证不会消费主部署 suite 的确认。oracle 从操作前事实和真实 delivery 记录判定，合法任务分母在运行前固定。准备回执、探针证据、实际部署结果和候选反事实标签有不同用途，缺失保持 null。

M1–M3 历史入口、冻结附录 B 和验收结果保留在 [M1](docs/handoffs/M1.md)、[M2](docs/handoffs/M2.md)、[M3](docs/handoffs/M3.md) 交接单中。原 M2 配置继续保持 prepare-only 行为，不能把其未请求的部署风险写成 0。旧运行目录绑定当时源码，不应直接用当前实现续跑。

当前 CLI 提供 `doctor`、`validate`、`run`、`inspect`、`resume`、`export`。真实模型运行尚未实现。

## 代码与扩展边界

| 入口 | 当前职责 |
| --- | --- |
| `contracts.py`、`preparation.py`、`lifecycle.py`、`workflow_contracts.py` | 严格身份、快照、准备状态、准入/物化契约与可信效果记录 |
| `ports.py`、`config.py`、`wiring.py` | 准备阶段 SubjectAdapter 接口、TOML 和显式能力预检 |
| `engine/state.py`、`engine/runner.py` | 持久阶段推进、准备选项绑定和适配器调用 |
| `subjects/fake.py`、`infra/models.py` | 四检查点、确定性实际上下文生成、候选与准备后快照 |
| `interventions/context.py`、`interventions/validation.py` | q 字节变换/计数/有效性与 v 执行前计划 |
| `subjects/verification.py` | 隔离的实际测试、费用与收据、原生候选选择 |
| `auditing/baselines.py`、`auditing/probes.py` | 原生/全部拒绝/探针规则准入，以及预算内的实际后代流程 |
| `engine/commit.py`、`subjects/materialize.py` | 候选绑定、恢复计划、控制器授权、独立 staging 核验及原子提交 |
| `engine/execution.py` | 执行当前部署版本、保存独立效果判定和确认越权后的主分支暂停 |
| `tasks/workflow.py`、`oracles/workflow.py` | transfer 事务效果，以及不接收候选程序的独立判定 |
| `infra/artifacts.py`、`snapshots.py`、`journal.py` | 内容寻址、含已提交 WAL 的备份、预算/费用/暴露 |
| `studies/preparation.py` | 复用同一引擎组装三个 M2 prepare-only 示例 |
| `studies/closed_loop.py`、`studies/transition.py` | 组装 M3 闭环与冻结附录 B，不在研究脚本中另造生命周期 |
| `studies/s0.py` | 登记 S0 多根/多臂/多轮槽位，复用同一准备、准入和提交引擎 |
| `analysis/access.py`、`export.py` | 只读能力边界、允许字段投影、带摘要的导出 |
| `analysis/metrics.py`、`partitions.py` | 固定 H 与计数统计、配对汇总和谱系/复制分区约束 |

表中路径均位于 `src/rsi_lab/`。依赖通过少量契约和普通构造函数传递；没有动态插件扫描、Python 配置执行或全局 LabContext。第三方扩展边界见 [架构说明](docs/architecture.md)。

运行模式固定为 `trusted_fixture`，`arbitrary_code_execution=false`、`hard_isolation_verified=false`。仅执行登记的声明式 transfer 程序；独立工作目录、Python 对象和工件允许列表不构成恶意代码的 OS 沙箱。

## 协议来源与后续工作

两份原参考文档及 SHA-256 保存在 [来源清单](docs/specs/source_manifest.json)。proposal 确定研究方向，实现 prompt 提供分阶段验收。原 `frozen-p0-v1` 的 [定义](docs/frozen_definitions.md)、transfer fixture 和金标准保持不变；新增四类授权任务仍是独立的 [authorization-battery-v1 草案](docs/specs/authorization_battery_v1.md)，尚未实现或运行。

当前验收范围见 [M4 交接单](docs/handoffs/M4.md)，历史闭环证据见 [M3 交接单](docs/handoffs/M3.md)。[M2 交接单](docs/handoffs/M2.md) 和 [M1 交接单](docs/handoffs/M1.md) 保留历史证据。M3 的 `m3_transition` 执行附录 B 的拒绝、提交和下一轮检索；M2 的共享费用示例仍只验证准备阶段。

| 后续阶段 | 验收重点 |
| --- | --- |
| 真实模型与外部 RSI 适配 | 实现生成/准备与物化接口，验证相应隔离，再开展独立协议 pilot |
| 新授权电池 | 独立协议下的四族世界、D/P/I/A/U、电池标签和开发/封存参数划分 |

新电池的即时基线观测轮次、8 个测试预算和定向探针可见证据等口径，见 [设计差异](docs/specs/design_decisions.md)。控制 fixture 的确定性结果不能用于估计自然风险频率、证明学习方法优势或代替封存评价。

## GPU 验证

M1–M4 / P0 均不需要 GPU 或 API key。服务器目前可以先做环境诊断：

```bash
mkdir -p runs
python3 scripts/gpu_preflight.py --output runs/gpu-report.json --require-cuda
```

该命令不下载模型、不推理、不训练；CUDA 不可用时返回非零并保留报告，已存在输出不会覆盖。P0 已接通工程闭环与导出；真实模型 pilot 仍需模型适配器、相应执行隔离验收和任务协议准备。具体验证顺序见 [GPU 说明](docs/gpu_validation.md)。

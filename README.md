# RSI Safety Lab

**面向不同自改进系统的安全测评与更新准入平台。** 平台管理任务、实验分支、可信执行证据、预算和独立效果判定；适配器保留受测系统的改进逻辑；独立检测器使用获准证据判断候选更新是否可以生效。

下一阶段聚焦：**在明确的任务、评分和工具约束下，自然生成的违规候选能否进入优化档案、被后续编辑使用？额外准入对此有什么影响，以及付出多少收益和成本？** 保留当前任务与公共内核，q/v/s作为可选分析角色；q/v自更新不再是实验成功的必要条件，组件体系无需重构。执行顺序为零API固定候选反事实、必要的局部接线，再按证据决定有上限的真实续演，见[入库反事实协议](docs/specs/openevolve_admission_protocol.md)。

当前[压力任务开发集 v0.2](docs/specs/stress_bench_protocol.md)覆盖授权、金融、零售和隐私的10个任务族，默认240条序列／120组正常与压力配对。零售派生自τ-bench政策和工具；新增金融`recipient_mismatch`借鉴AgentDojo收款方完整性目标，其他自建任务与非复现边界见[来源对应](docs/specs/stress_bench_sources.md)。原v0.1的216条开发工件保留。每领域通过`StressTaskPackage`复用现有平台，接入仍需验收工具契约。

`stress-pilot`在独立新起点比较OpenEvolve与GEPA的受限程序配置，预登记最多48次编辑派发、800,000 token账本额度；全部搜索冻结后才做独立压力评价，未知响应不补跑。GEPA此次优化完整受限程序，与既有提示词优化配置分开；小样本开发试跑不构成确认性排行榜。协议见[两系统试跑](docs/specs/stress_rsi_pilot_protocol.md)，入口见[实验命令目录](docs/experiments.md)。旧零售18次调用／200,000 token拟议续演额度独立不变。

当前可接入确定性授权 fixture、闭合 JSON 策略的 Task A、真实受限源码的 q 优化系统，以及调用官方优化器的 **GEPA** 和 **OpenEvolve**。前三者为本地参考系统。GEPA 是 [RSIHub 选用的系统之一](https://github.com/simple-agent-lab/RSIHub/tree/d6554299c58bf0fe2db04ddda0277f9db67ea680)；本项目接入其提示词改进过程，尚未复现 RSIHub 的任务配置或成绩。[GEPA 接入范围](docs/external_gepa.md)。OpenEvolve 使用原生 worker 和程序库，通过专用筛查入口接公共内核；单岛串行、受限源码与额外公开门槛等适配改动见 [OpenEvolve 接入范围](docs/external_openevolve.md)。

## 安装和最小运行

需要 Python 3.11+。基础平台只依赖标准库，GEPA 和 OpenEvolve 是可选依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

rsi-lab platform validate --config configs/platform_fixture.toml
rsi-lab platform run --config configs/platform_fixture.toml --output runs/platform-fixture
rsi-lab platform inspect --run runs/platform-fixture
rsi-lab platform resume --run runs/platform-fixture

rsi-lab platform run --config configs/platform_task_a.toml --output runs/platform-bounded
python -m pytest -q
```

`run` 拒绝覆盖已有目录，`resume` 恢复同一配置和实现，`inspect` 不推进实验。源码或协议改变后需要新运行，不能静默续接旧证据。`validate` 展示模型预算范围；四臂试验先用 `platform pilot-validate --config <配置> --roots 2` 查看累计额度，不需要密钥。预算范围与本地性能测量见 [infra审阅](docs/infra_review.md)。

## 运行入口

| 用途 | 命令／文档 |
| --- | --- |
| 通用系统、任务与审计组合 | `rsi-lab platform --help`；配置选择见下表 |
| 压力任务开发集 | `rsi-lab stress-bench --help`；`build/check/score`，见[开发协议](docs/specs/stress_bench_protocol.md) |
| 两种RSI的新根压力试跑 | `rsi-lab stress-pilot --help`；`validate/run/resume/inspect`，见[试跑协议](docs/specs/stress_rsi_pilot_protocol.md) |
| GEPA 原生提示词优化 | 安装 `.[dev,gepa]`，使用 `configs/platform_gepa_control.toml`；[接入范围](docs/external_gepa.md) |
| OpenEvolve 原生程序库筛查 | 安装 `.[dev,openevolve]`，运行下面的确定性控制；[接入范围](docs/external_openevolve.md)、[覆盖表](docs/openevolve_coverage.md) |
| q、v、优化经验、训练及历史探索 | [实验命令目录](docs/experiments.md)；各入口均支持 `--help` |
| 当前进度与实验结论 | 唯一持续维护的 [HANDOFF](docs/HANDOFF.md) |

```bash
# 实际运行原生 OpenEvolve；模型响应为工程控制，不调用云服务。
python -m pip install -e '.[dev,openevolve]'
rsi-lab openevolve-screen run --config configs/openevolve_screen_control.toml --output runs/oe-control
python scripts/verify_openevolve_screen.py runs/oe-control
```

OpenEvolve目前走专用研究入口，复用公共内核，未注册到通用 `platform` 配置。它和GEPA的适配范围不同，不能把“接入”解释成完整原生系统复现。云端模型、预算与私有凭据配置见[实验命令目录](docs/experiments.md)，CPU加云端API即可，不要求GPU。

## 组合实验

新入口通过配置组合插件，不再用阶段编号选择实现。

| 配置部分 | 作用与当前选项 |
| --- | --- |
| `system` | `fixture`、`bounded-task-a`、`cost-q`、`gepa`；OpenEvolve使用专用筛查入口 |
| `task` | `authorization`、`authorization-workflows`、`cost-authorization`；零售派生工作流目前用于OpenEvolve筛查 |
| `protocol.mode` | `behavior` 行为测评；`paired` 配对后代测评；`admission` 实际更新准入 |
| `detector` | `diff-probe`、`random-probe`、`frozen-model`、`none` |
| `detector.policy` | `conservative`、`native-only`、`reject-all` |
| `backend` | `bounded-tools`，执行有类型的有限工具动作 |
| `editor` / `actor` | 独立模型网关、调用/token 上限与来源记录 |

运行前检查能力与工具协议。不支持快照、候选生效前暂停或所需工具的系统不能静默参加对应实验。

一次准入流程先准备候选，再运行隔离的配对短探针，冻结检测报告与 `accept/reject/inconclusive` 决定，最后由控制器核对状态并执行激活。拒绝和证据不足保留旧系统及继承状态。独立长续演只生成离线标签，不回流到在线检测器。

当前部署比较针对**一次经过审计的更新**；探针与独立标签分支继续原生改进，部署测量阶段冻结已选择的系统。尚未提供每轮递归启动外部审计的连续准入实验。短探针没有发现越权时，保守策略一般输出 `inconclusive`，不把短程无事发生解释为长期安全保证。

## 扩展与学习

- 接入新 RSI：实现 `api/ports.py` 的 `SystemAdapter`，声明能力、版本组件、快照、原生候选生成、任务执行及激活查询；在 `PluginRegistry` 注册工厂和可选控制模型，见[扩展指南](docs/extending.md)。
- 接入新任务：实现任务包和环境端口，提供公开请求、工具、独立日志及 oracle；检查与系统的工具协议兼容性。
- 接入检测方法：使用显式 `AuditView` 和受限探针能力，输出 `DetectionReport`；由独立 `AdmissionPolicy` 转为绑定候选的决定。
- 训练风险模型：沿用 `AuditFeatures`、`CandidateRiskLabel`、`RiskScorer.predict(...)`。训练、校准和封存分区分开，冻结评分器不读取长程标签。旧 Task A 模型不能直接视为新协议已校准模型。

```bash
python scripts/task_a_training_smoke.py --output runs/task-a-learning
```

完整契约见 [架构说明](docs/architecture.md)，代码结构的审阅与整理见 [infra审阅](docs/infra_review.md)，学习接口见 [Task A 训练说明](docs/task_a_learning.md)。**唯一持续维护的交接入口是 [HANDOFF](docs/HANDOFF.md)**，包含最新验证结果和未完成工作。

## 实验边界

当前任务覆盖授权工作流、授权程序成本和零售派生小工作流；新增压力开发集汇集授权、金融、零售和隐私的合成案例，仍非完整业务benchmark。日志中的违规尝试、原工具拒绝、外部拦截及实际生效分别报告。默认 `log_and_allow` 仅作用于模拟世界；`enforce` 是单独敏感性条件。缺失结果保持缺失，已知伤害不会因随后中断而消失。模型调用、token、实际计价信息和合成工作量分开记录，不将合成单位当作金钱。

确定性例子证明延迟触发、接线和准入控制，不证明风险在后代才形成，也不是论文性能结果。四族 D/P/I/A/U、自然模型数据、正式危险阈值、统计功效和跨任务泛化仍需实验。插件是可信 Python；有限 JSON 工具执行不等于任意第三方生成代码的操作系统沙箱。

## 旧实验复现

冻结 P0 的 `run/resume/inspect/export` 与 `research` 入口继续保留；新插件不依赖它们的阶段分支。旧 P0 四检查点、golden 和来源文档未改成新协议。

```bash
rsi-lab run --config configs/s0_smoke.toml --output runs/legacy-s0
rsi-lab export --run runs/legacy-s0 --output exports/legacy-s0 --view analysis
rsi-lab research validate --config configs/task_a_control.toml
```

旧实验的源码绑定规则仍生效。历史分阶段交接单已删除，其内容可从 Git 历史查阅；原始 proposal 和实现 prompt 保留为来源材料，不作为当前交接文档。

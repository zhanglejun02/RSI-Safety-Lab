# RSI Safety Lab

**面向不同自改进系统的安全测评与更新准入平台。** 平台管理任务、实验分支、可信执行证据、预算和独立效果判定；适配器保留受测系统的改进逻辑；独立检测器使用获准证据判断候选更新是否可以生效。

核心研究问题是：**机制更新通过当前检查后，是否增加后续后代的风险，以及有限预算的探针能否提前识别这种风险。** 系统不必具有统一的 q/v/s 结构，也不要求固定的大小模型组合。

当前可接入三种实现：确定性授权 fixture、闭合 JSON 策略的 Task A，以及调用官方优化器的 **GEPA**。GEPA 是 [RSIHub 选用的系统之一](https://github.com/simple-agent-lab/RSIHub/tree/d6554299c58bf0fe2db04ddda0277f9db67ea680)；本项目接入其提示词改进过程，尚未复现 RSIHub 的任务配置或成绩。[接入范围与固定版本](docs/external_gepa.md)

## 安装和最小运行

需要 Python 3.11+。基础平台只依赖标准库，GEPA 是可选依赖。

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

`run` 拒绝覆盖已有目录，`resume` 恢复同一配置和实现，`inspect` 不推进实验。源码或协议改变后需要新运行，不能静默续接旧证据。

运行第一个外部系统：

```bash
python -m pip install -e '.[dev,gepa]'
rsi-lab platform validate --config configs/platform_gepa_control.toml
rsi-lab platform run --config configs/platform_gepa_control.toml --output runs/gepa-control
```

这里执行真实安装的官方 `gepa==0.1.4`，actor 和 reflector 使用标记为 `control` 的确定性响应，不调用云服务。`configs/platform_gepa.toml` 则配置真实 HTTP 模型：先在环境中设置 `OPENROUTER_API_KEY`，再创建独立的 pilot 运行。actor 与 editor 可分别配置服务、模型和预算；API 推理不要求本地 GPU。Kimi 的一次 H=3 开发闭环已实际运行；OpenRouter 推理尚未验证，详情见 [HANDOFF](docs/HANDOFF.md)。

当前开发试验使用批量授权工作流：三例公开套件保持合法完成优先，同时奖励更少的模型调用。脚本控制存在优化空间，但已测试的 Kimi founder 已达到当前安全批处理参考方案的调用数，尚不能假定自然模型还会改进。配置要求 `min_public_utility=1.0`，避免靠批量拒绝合法请求来取得“有效优化”。反思器收到公开请求、工具定义、真实调用参数及返回值；独立续演参数和标签不进入反思输入。

```bash
rsi-lab platform pilot-run --config configs/platform_workflow_control.toml --roots 2 --output runs/workflow-pilot
rsi-lab platform pilot-resume --run runs/workflow-pilot
rsi-lab platform inspect --run runs/workflow-pilot
# 可选：同一个根候选先冻结四种审计决定，再进行独立评价：
rsi-lab platform pilot-run --config configs/platform_workflow_control.toml --roots 2 --audit-baselines --output runs/workflow-audits
# 一次验证安全/危险两种四组控制、恢复和危险更新准入（仅 CPU，无 API）：
python scripts/workflow_validity_smoke.py --roots 2 --output runs/workflow-smoke
```

`pilot-run` 将模式设为 `paired`，只接受开发分区。同一个根更新分成旧/新版本 × 冻结/原生继续改进四组，复用公共续演引擎，隔离状态、模型日志和分支预算。无改动、同分、无效及失败更新均保留；报告按 root 汇总完整率、前两轮/延迟风险、效用与四组差值。它用于检查任务和更新是否有效，不产生检测器排名或直接导出混合条件训练标签。单次准入验证继续使用 `platform run`。

`--audit-baselines` 比较当前公开检查、随机探针、关键词差异探针、全部拒绝。随机与差异探针各预留 4 个配对轮，拥有相同的调用/token 上限；每种方法的旧/新臂共享该方法额度，实际消耗和完成数单列。在线决定先落盘，独立标签随后生成。汇总保留全体根，并单列“原生可接受且有变化”与“严格改善”子集；报告错误率时同时显示缺失、`inconclusive`、良性接受率与决策覆盖率。当前关键词规则不是已验证的机制推理器。

GEPA 的同分更新可以按 `improvement_or_equal` 进入续演，严格改善只作为分析分层；兼容参数 `require_strict_improvement` 不再控制原生激活。适配器仍增加完整执行、公开无违规与最低效用检查，具体规则写入证据。无变化候选即使被接受，也不会记成实际版本变化。

脚本配置中的 `control_strategy` 可选 `safe_batch` 或 `unsafe_cache`，两者均为明确构造的工程对照。真实模型配置为 `configs/platform_workflow_http.toml`；先核实服务、密钥和模型，再从一个 root 开始。该配置的调用/token 上限在四组试验中按**每个根提案、每个条件/臂/重复**分别计算，总预算不是单个配置值。

Kimi 接入使用 `configs/platform_workflow_kimi.toml`，当前选用 `kimi-k2.6` 非思考模式、温度 0.6。把凭据配置为环境变量，或保存在权限为 600、被 Git 忽略的 `.env` 中，再显式传入该文件：

```bash
rsi-lab platform validate --config configs/platform_workflow_kimi.toml --env-file .env
rsi-lab platform run --config configs/platform_workflow_kimi.toml --env-file .env --output runs/kimi-admission
rsi-lab platform resume --run runs/kimi-admission --env-file .env
```

文件只接受配置声明的凭据变量（此处 `MOONSHOT_API_KEY`）及可选 `SSL_CERT_FILE`；不会执行 shell 或自动加载 `.env`，已有进程环境优先。这个 Kimi 配置运行 H=3 的开发验收，覆盖第一道延迟触发题，调用上限为 actor 1,200、reflector 32；它不是封存论文实验。模型必须以服务返回的实际可用列表为准。[API 验证说明](docs/gpu_validation.md)

另有一个独立的真实任务探索脚本，测重复报表计算、内容缓存与当前字段授权。它先比较 Kimi generic 提示词和人工安全参考；只有观察到合法完成不变、调用数下降，才运行两条 GEPA 根、新参数公开复验和冻结旧／新 H4 配对。人工参考不提供给反思器；这个实验不修改平台内核，也不测持续后代演化。

```bash
.venv/bin/python scripts/report_delivery_pilot.py --output runs/report-exploration --env-file .env --phase all
```

这会调用真实 Kimi API，完整流程上限为640次actor、2次editor；各臂预算独立，实际用量单列。2026-11-01的小试验未找到可复验改善伴随后续增险的更新，完整负结果见 [HANDOFF](docs/HANDOFF.md)。已有目录可用同命令恢复，代码或协议改变后须新建目录。

授权程序成本实验参照 [Safety Must Survive Self-Improvement](https://arxiv.org/html/2610.01073v1) 的固定契约实验，重新实现结构化授权环境。模型编辑完整 `handle(request, cache, api)` 程序，由有资源上限的 AST 解释器执行；不执行任意 Python，也未增加公共平台接口。原作者仓库截至2026-11-01尚未发布可执行代码与数据，因此这里是协议适配实验，不是原代码或原成绩复现。

```bash
# 工程控制，无 API：验证正确 founder、安全优化空间及危险缓存的实际效果
.venv/bin/python scripts/authorization_cost_pilot.py --output runs/cost-control --phase controls
# 真实 Kimi：3 条根谱系 × 6 代 × 每代2候选，至多36次编辑调用
.venv/bin/python scripts/authorization_cost_pilot.py --output runs/cost-pilot --env-file .env --phase all
# 已完成优化后，纯本地重算独立评价
.venv/bin/python scripts/authorization_cost_pilot.py --output runs/cost-pilot --phase evaluate
```

前三代使用部分公开验证，第四代扩展验证；成本排序始终使用固定的例行工作量。所有自然优化历史冻结之后，才运行新参数公开复验与独立效果评价；完整保留候选、失败和负结果。模拟服务成本、真实API调用及token分别报告。执行程序更新与后续请求流是本实验对象，尚不测q/v更新、持续后代演化或探针准入效果。已有目录可用相同命令恢复，未知请求不会自动重发；源码与协议改变后须新目录。

金融迁移另用五类结构化记录：发票、采购单、收货记录、审批及付款历史。程序自行三单匹配、查重及检查当前审批，然后写入模拟付款账本；不是把授权任务换一个名字，也不执行真实付款。主独立测试只改变审批撤销、账户绑定和过期条件，不含提示注入。它复用同一个有限程序优化循环，不新增平台注册接口。

```bash
.venv/bin/python scripts/invoice_payment_pilot.py --output runs/invoice-control --phase controls
.venv/bin/python scripts/invoice_payment_pilot.py --output runs/invoice-pilot \
  --reference-run runs/cost-pilot --env-file .env --phase all
```

自然迁移要求参照授权试验已经完整结束，最多调用16次Kimi编辑（2根×4代×2候选）。参照结果仅作为来源记录，不提供给编辑器。金融任务从自己的正确初始程序重新优化，因此是审批缓存机制的任务迁移，不是同一候选源码直接跨系统运行，也不是现成金融benchmark复现。开发中的源码引导组合诊断另存结果，不能混入预登记评价或封存成绩。

## 组合实验

新入口通过配置组合插件，不再用阶段编号选择实现。

| 配置部分 | 作用与当前选项 |
| --- | --- |
| `system` | `fixture`、`bounded-task-a`、`gepa` |
| `task` | `authorization` 单请求授权；`authorization-workflows` 可优化批量授权工作流 |
| `protocol.mode` | `behavior` 行为测评；`paired` 配对后代测评；`admission` 实际更新准入 |
| `detector` | `diff-probe`、`random-probe`、`frozen-model`、`none` |
| `detector.policy` | `conservative`、`native-only`、`reject-all` |
| `backend` | `bounded-tools`，执行有类型的有限工具动作 |
| `editor` / `actor` | 独立模型网关、调用/token 上限与来源记录 |

运行前检查能力与工具协议。不支持快照、候选生效前暂停或所需工具的系统不能静默参加对应实验。

一次准入流程先准备候选，再运行隔离的配对短探针，冻结检测报告与 `accept/reject/inconclusive` 决定，最后由控制器核对状态并执行激活。拒绝和证据不足保留旧系统及继承状态。独立长续演只生成离线标签，不回流到在线检测器。

当前部署比较针对**一次经过审计的更新**；探针与独立标签分支继续原生改进，部署测量阶段冻结已选择的系统。尚未提供每轮递归启动外部审计的连续准入实验。短探针没有发现越权时，保守策略一般输出 `inconclusive`，不把短程无事发生解释为长期安全保证。

## 扩展与学习

- 接入新 RSI：实现 `api/ports.py` 的 `SystemAdapter`，声明能力、版本组件、快照、原生候选生成、任务执行及激活查询；在 `PluginRegistry` 注册工厂。
- 接入新任务：实现任务包和环境端口，提供公开请求、工具、独立日志及 oracle；检查与系统的工具协议兼容性。
- 接入检测方法：使用显式 `AuditView` 和受限探针能力，输出 `DetectionReport`；由独立 `AdmissionPolicy` 转为绑定候选的决定。
- 训练风险模型：沿用 `AuditFeatures`、`CandidateRiskLabel`、`RiskScorer.predict(...)`。训练、校准和封存分区分开，冻结评分器不读取长程标签。旧 Task A 模型不能直接视为新协议已校准模型。

```bash
python scripts/task_a_training_smoke.py --output runs/task-a-learning
```

完整契约见 [架构说明](docs/architecture.md)，学习接口见 [Task A 训练说明](docs/task_a_learning.md)。**唯一持续维护的交接入口是 [HANDOFF](docs/HANDOFF.md)**，包含最新验证结果和未完成工作。

## 实验边界

当前任务只覆盖确认授权的一族，日志中实际生效越权才计入风险。默认 `log_and_allow` 仅作用于模拟世界；`enforce` 是单独敏感性条件。缺失结果保持缺失，已知伤害不会因随后中断而消失。模型调用、token、实际计价信息和合成工作量分开记录，不将合成单位当作金钱。

确定性例子证明延迟触发、接线和准入控制，不证明风险在后代才形成，也不是论文性能结果。四族 D/P/I/A/U、自然模型数据、正式危险阈值、统计功效和跨任务泛化仍需实验。插件是可信 Python；有限 JSON 工具执行不等于任意第三方生成代码的操作系统沙箱。

## 旧实验复现

冻结 P0 的 `run/resume/inspect/export` 与 `research` 入口继续保留；新插件不依赖它们的阶段分支。旧 P0 四检查点、golden 和来源文档未改成新协议。

```bash
rsi-lab run --config configs/s0_smoke.toml --output runs/legacy-s0
rsi-lab export --run runs/legacy-s0 --output exports/legacy-s0 --view analysis
rsi-lab research validate --config configs/task_a_control.toml
```

旧实验的源码绑定规则仍生效。历史分阶段交接单已删除，其内容可从 Git 历史查阅；原始 proposal 和实现 prompt 保留为来源材料，不作为当前交接文档。

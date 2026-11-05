# 实验命令目录

所有命令从仓库根目录执行。输出写入本地 `runs/` 或 `exports/`，不随源码分发。依赖历史工件的分析入口需要用户提供对应冻结数据；下文列出运行方法，不包含任何已完成运行的成绩。

`run` 创建新运行，`resume` 恢复相同源码、配置与协议绑定的运行，`inspect` 只读查看状态。未知模型响应保持未知，不通过自动重发补齐；拒绝、无效、失败和缺失均保留。源码或协议变化后创建新目录。

## 模型与凭据

控制配置使用显式标记的脚本模型，不调用云服务。HTTP 配置通过环境变量读取凭据；也可将配置声明的变量写入权限为 600、被 Git 忽略的 `.env`，再传入 `--env-file .env`。Kimi 配置使用 `MOONSHOT_API_KEY`，OpenRouter 配置使用 `OPENROUTER_API_KEY`。可选 `SSL_CERT_FILE` 指定可信 CA 文件。

环境文件加载器不执行 shell、不做变量插值，已有进程变量优先。保持 TLS 校验开启。模型服务、模型 ID、配额与预算由运行者检查；真实模型调用产生的使用量与本地合成服务成本分别记录。云端推理和 CPU 风险评分器不要求本地 GPU；自托管与环境检查见 [API 与 GPU 说明](gpu_validation.md)。

```bash
rsi-lab platform validate --config configs/platform_workflow_kimi.toml --env-file .env
rsi-lab platform run --config configs/platform_workflow_kimi.toml --env-file .env --output runs/kimi-admission
rsi-lab platform resume --run runs/kimi-admission --env-file .env
# 只读查看运行进度
python scripts/platform_progress.py --run runs/kimi-admission
```

## 压力任务开发集

`stress-bench` 提供授权、金融、零售和隐私的正常／压力配对序列。`build` 生成任务，`check` 检查初始合法、安全优化、人工错误及全拒绝控制，`score` 评价固定源码。任务设计、分区、来源与评分分别见[任务协议](specs/stress_bench_protocol.md)和[来源对应](specs/stress_bench_sources.md)。

```bash
rsi-lab stress-bench build --output runs/stress-corpus
rsi-lab stress-bench check --corpus runs/stress-corpus --output runs/stress-check
# 可选：锁定 OpenEvolve 的本地 scripted 接线检查
rsi-lab stress-bench check --corpus runs/stress-corpus --output runs/stress-check-native --openevolve-smoke
# domainpaths.json 将领域名映射到受限程序源码路径
rsi-lab stress-bench score --corpus runs/stress-corpus --programs domainpaths.json --label fixed-programs --output runs/stress-score
```

违规尝试、实际生效、合规完成、正确性与领域内服务成本分开报告。缺失不填零，合成成本不跨领域相加。固定源码和 scripted 控制用于检查任务与接线，不构成自然 RSI 排名。

## 两系统压力试跑与配对比较

`stress-pilot` 使用共同初始程序、工具契约、公开评分及完整正确门槛，比较 OpenEvolve 单岛档案与 GEPA 受限程序改写。两配置的原生选择及继承边界见[协议](specs/stress_rsi_pilot_protocol.md)。所有搜索槽冻结后才执行独立压力评价，标签不回流编辑器。

```bash
python -m pip install -e '.[dev,gepa,openevolve]'
rsi-lab stress-pilot validate --config configs/stress_pilot_kimi.toml
rsi-lab stress-pilot run --config configs/stress_pilot_control.toml --output runs/stress-pilot-control
rsi-lab stress-pilot run --config configs/stress_pilot_kimi.toml --output runs/stress-pilot-natural --env-file .env
rsi-lab stress-pilot resume --config configs/stress_pilot_kimi.toml --output runs/stress-pilot-natural --env-file .env
rsi-lab stress-pilot inspect --output runs/stress-pilot-natural
```

安全改进配对比较由 `stress-confirmation` 提供。终点、冻结阈值、分母、预算、独立评价与金融旁路探针见[比较协议](specs/stress_confirmation_protocol.md)。登记、工程控制和自然运行使用独立目录与参数；运行前按该入口要求绑定登记和授权材料。

```bash
rsi-lab stress-confirmation --help
rsi-lab stress-confirmation control --output runs/confirmation-control
```

## 工具反馈、累计额度与动态 RL

`mechanism-pilot` 组装隐私契约、不可信工具反馈和累计项目额度任务；`adaptation-rl-pilot` 组装动态需求与有限表格 RL 任务。两者复用公共适配、激活和独立效果记录，协议与预算分别登记。见[机制协议](specs/mechanism_pilot_protocol.md)和[动态／RL 协议](specs/adaptation_rl_pilot_protocol.md)。

```bash
python scripts/diagnose_stress_tasks.py --output runs/mechanism-diagnosis-local
rsi-lab mechanism-pilot control --output runs/mechanism-control-local
rsi-lab adaptation-rl-pilot control --output runs/adaptation-rl-control-local
rsi-lab adaptation-rl-pilot inspect --output runs/adaptation-rl-control-local
```

这些专用入口绑定各自配置和登记路径；自然运行前查看 `--help` 与协议中的 `register/run/resume` 要求，不能把同一登记当作无限重复派发的额度。RL 奖励、安全 cost、训练探索、合法完成和执行资源分别计量。

## OpenEvolve 程序库与入库反事实

[接入说明](external_openevolve.md)区分上游原生行为与平台额外门槛。零售任务使用 τ-bench 派生政策及工具，保留原工具拒绝，采用合成数据与结构化用户答复。

```bash
python -m pip install -e '.[dev,openevolve]'
rsi-lab openevolve-screen run --config configs/openevolve_screen_control.toml --output runs/oe-control
python scripts/verify_openevolve_screen.py runs/oe-control
rsi-lab openevolve-screen run --config configs/openevolve_screen_kimi.toml --output runs/oe-natural --env-file .env
rsi-lab openevolve-screen inspect --output runs/oe-natural
rsi-lab openevolve-screen resume --output runs/oe-natural --env-file .env
# 输入须为本地冻结且具有完整状态的运行
rsi-lab openevolve-admission --run runs/oe-natural --output runs/oe-admission
```

入库反事实逐提案恢复准确入库前状态，固定源码、公开评分、重评分和工具行为，只比较公开正确性门槛与上游入库条件。单步反事实不拼成另一条演化历史；无法恢复的记录标为不可分析。具体条件见[入库协议](specs/openevolve_admission_protocol.md)。

## GEPA 提示词与四组续演

GEPA 通过官方优化器改进提示词，actor 与 reflector 分别配置来源和预算。能力与适配限制见[接入说明](external_gepa.md)。

```bash
python -m pip install -e '.[dev,gepa]'
rsi-lab platform validate --config configs/platform_gepa_control.toml
rsi-lab platform run --config configs/platform_gepa_control.toml --output runs/gepa-control
# 先预览累计预算；预检与正式运行选项需一致
rsi-lab platform pilot-validate --config configs/platform_workflow_kimi.toml --roots 2 --audit-baselines
rsi-lab platform pilot-run --config configs/platform_workflow_control.toml --roots 2 --audit-baselines --output runs/workflow-audits
rsi-lab platform pilot-resume --run runs/workflow-audits
rsi-lab platform inspect --run runs/workflow-audits
```

`pilot-run` 将同一根更新分为旧／新版本 × 冻结／继续原生改进四组，隔离系统状态和模型日志。在线基线决定先保存，再生成独立标签。无变化、同分、无效和缺失均保留；按 root 汇总，不能把同源分支当独立样本。单次更新的实际准入部署使用 `platform run`。

四组配置的模型预算按根提案、条件、臂、重复和可选基线累计，不能把单个网关上限当总预算。`pilot-validate` 返回的额度是配置上限，不是实际用量或费用估计。

## q 更新、优化经验与风险学习

授权成本任务的 q 更新只改变经验选择；后代通过公共续演入口生成受限程序、公开验证并选择。下列控制配置用于检查不同研究入口，具体参数和自然模型配置见 `configs/`。

```bash
rsi-lab platform run --config configs/platform_cost_q.toml --output runs/platform-cost-q
rsi-lab cost-q run --config configs/cost_q_control.toml --output runs/cost-q-control
rsi-lab q-effect run --config configs/q_effect_control.toml --output runs/q-effect-control
rsi-lab experience run --config configs/experience_control.toml --output runs/experience-control
rsi-lab optimized-q run --config configs/optimized_q_control.toml --output runs/optimized-q-control
```

`q-effect` 比较旧 q、新 q、恢复旧经验与不变采样对照。`experience` 在新公开任务中建立带真实修改和修复证据的优化历史，比较相关、无关与 sham 条目；字节匹配不等于 token 相等。`optimized-q` 从公开合格的历史检查点出发，固定源码和状态比较 q 条件。依赖历史来源的配置需要相应本地工件；无法验证输入时停止，不虚构经验。

`cost-q` 将独立长程标签、离线训练、校准与冻结评分串联。静态、仅探针和组合评分器使用各自获准信息；训练、校准和评价按根隔离。固定启发式探针与学习预测的贡献分别报告。可训练接口不代表安全性能成立，控制数据不能直接校准其他系统或自然更新。

基础学习接口和命令见 [Task A 训练说明](task_a_learning.md)：

```bash
python scripts/task_a_training_smoke.py --output runs/task-a-training-smoke
rsi-lab research validate --config configs/task_a_control.toml
rsi-lab research collect --config configs/task_a_control.toml --output runs/task-a-control
rsi-lab research dataset --run runs/task-a-control --output exports/task-a-control
rsi-lab research train --dataset exports/task-a-control --output runs/task-a-model.json
```

## 固定候选上的 v 干预

`v-offline` 导入本地冻结的授权成本提案，在固定程序上比较验证覆盖。候选、旧程序和公开池保持不变，决定先冻结，独立效果随后生成。恢复臂与旧 v 相同，不能作为独立重复。

```bash
rsi-lab v-offline run --source-run runs/cost-pilot --output runs/v-offline
rsi-lab v-offline inspect --run runs/v-offline
rsi-lab v-offline resume --run runs/v-offline
python scripts/verify_offline_v.py --run runs/v-offline --output runs/v-offline-verification.json
```

v 的公开优化目标是在保留公开判别及已知失败回归的条件下降低验证成本，方法与停止规则见[公开优化协议](specs/v_public_optimization_protocol.md)。`scripts/v_public_objective_preflight.py` 执行本地可行性分析，不读取独立风险标签作为选择目标。

## 单任务探索与证据复核

独立脚本提供授权程序成本、报表投递及金融审批迁移任务；它们不是通用平台插件，也不等同于完整业务 benchmark。使用前查看各脚本的 `--help`、模型预算与来源要求。

```bash
python scripts/authorization_cost_pilot.py --output runs/cost-control --phase controls
python scripts/invoice_payment_pilot.py --output runs/invoice-control --phase controls
python scripts/report_delivery_pilot.py --help
python scripts/review_frozen_run.py --help
```

`review_frozen_run.py` 在独立目录提取运行登记源码并核对冻结证据，禁止网络、模型派发及新训练／执行，原始工件与账本保持不变。脚本需要用户本地的完整运行；源码仓库不附带这些数据。

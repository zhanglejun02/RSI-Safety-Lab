# 实验命令目录

本文保留各研究入口的命令和协议边界。所有命令从仓库根目录执行；`runs/` 不随 Git 分发，依赖历史工件的命令需要相应本地数据。最新结果与下一步以 [HANDOFF](HANDOFF.md) 为准，入口清单见 [README](../README.md)。付费配置不会因阅读本目录自动启动。

## 压力任务开发集 v0.2（零HTTP）

`stress-bench`把授权、金融、零售和隐私整理为10个任务族的配对序列。默认构建public与development部分，共240个序列、120个配对；这是配置规模，不是独立演化根数或已完成的自然模型样本。零售使用τ-bench派生政策与工具；v0.2新增的金融`recipient_mismatch`借鉴AgentDojo的收款方完整性目标，采用本项目结构化环境重实现。等级对应2／4／8次合法预热，尚不代表已证明的难度顺序。规则与评分见[压力任务协议](specs/stress_bench_protocol.md)，来源和复用范围见[来源对应](specs/stress_bench_sources.md)。原v0.1的9族、216序列／108对工件保留旧语义，不用新源码静默覆盖。

```bash
rsi-lab stress-bench build --output runs/stress-corpus
rsi-lab stress-bench check --corpus runs/stress-corpus --output runs/stress-check
# 可选：锁定OpenEvolve的本地scripted接线检查，不调用HTTP模型。
rsi-lab stress-bench check --corpus runs/stress-corpus --output runs/stress-check-native --openevolve-smoke
# domainpaths.json为领域到受限程序源码文件路径的映射。
rsi-lab stress-bench score --corpus runs/stress-corpus --programs domainpaths.json --label fixed-programs --output runs/stress-score
```

`check`比较初始合法、合法优化、人工错误及全拒绝程序；`score`评价给定的固定源码。指标包括实际违规、尝试、合规完成、正确性与领域内服务成本，按任务族宏平均并保留明细；缺失不填0，不跨领域相加合成成本。每领域的`StressTaskPackage`可用于后续系统接线，但scripted联通和固定源码结果不是自然RSI排名。holdout生成配方不等于已封存评价；新增自然实验须另冻预算，原零售18次调用／200,000 token的拟议续演额度不变。

## GEPA／OpenEvolve新根压力试跑

`stress-pilot`使用共同的初始程序、工具契约、公开评分和公开完整正确门槛，比较OpenEvolve单岛程序档案与GEPA受限程序改写。后者是新`gepa_program`配置，执行官方优化器的分段搜索、只继承活跃程序；与通用`platform`入口的GEPA提示词配置区别见[试跑协议](specs/stress_rsi_pilot_protocol.md)。

```bash
python -m pip install -e '.[dev,gepa,openevolve]'
# 只展示预登记配置与额度，不调用模型。
rsi-lab stress-pilot validate --config configs/stress_pilot_kimi.toml
# 本地scripted工程控制。
rsi-lab stress-pilot run --config configs/stress_pilot_control.toml --output runs/stress-pilot-control
# 真实模型；与原零售检查点的续演账本分开。
rsi-lab stress-pilot run --config configs/stress_pilot_kimi.toml --output runs/stress-pilot-natural --env-file .env
rsi-lab stress-pilot resume --config configs/stress_pilot_kimi.toml --output runs/stress-pilot-natural --env-file .env
rsi-lab stress-pilot inspect --output runs/stress-pilot-natural
```

默认4领域×2个新根参数×2系统×H3，共16条系统根，最多48次编辑派发、800,000 token账本额度。所有编辑调用纳入各根账本，未知或无效响应占用原槽且不补跑；超出token预留／实耗条件即停止对应派发，供应商异常超报单独记录。正常公开任务用于优化；全部搜索冻结后才执行正常／压力两臂的独立开发评价，标签不回流到编辑或选择。

报告分别统计危险候选生成、原生入库、平台保留、实际进入后续请求、谱系行为重现，以及最终风险、合法收益和成本。按独立根保留缺失，不把任务数扩成样本数；当前开发试跑不提供确认性系统总排名。执行状态与实际结果统一记录在[HANDOFF](HANDOFF.md)，本节不预设自然实验结论。

## OpenEvolve 有上限适用性筛查

```bash
python -m pip install -e '.[dev,openevolve]'
# 原生优化器、确定性模型响应：不调用云服务。
rsi-lab openevolve-screen run --config configs/openevolve_screen_control.toml --output runs/oe-control
python scripts/verify_openevolve_screen.py runs/oe-control
# 真实 Kimi：共用最多50次请求、700000 token的账本。
rsi-lab openevolve-screen run --config configs/openevolve_screen_kimi.toml --output runs/oe-natural --env-file .env
rsi-lab openevolve-screen inspect --output runs/oe-natural
rsi-lab openevolve-screen resume --output runs/oe-natural --env-file .env
```

先在现有授权任务验证档案快照、拒绝不继承和原生候选来源，再运行 τ-bench 零售政策与原始工具派生的小工作流。后者使用合成数据与结构化用户回复，不是完整 τ-bench。独立日志区分违规尝试、工具拒绝、拦截和实际生效，任务失败不直接作为风险标签。

五条开发根中，仅公开成本改善得到新参数复验的更新进入 old/new 各两条 H2 真实续演；另外保留冻结程序对照和完整边界检查。2026-11-03 实测4/5条根复验改善，所有已评价冻结程序和后代均未观察到实际违规；1次请求中断结果未知。零售旧臂续演也达到新臂成本，没有形成评价有害检出或风险降低所需的增险证据。全零结果仍可描述拒绝、弃权、收益与开销。按[事前协议](specs/openevolve_screen_protocol.md)停止，不自动扩样或训练；具体结果、费用分类更正和冻结版本复核方式见 [HANDOFF](HANDOFF.md)。CPU加云端API即可。

[OpenEvolve覆盖表](openevolve_coverage.md)区分原生行为、平台额外约束、独立观察和未覆盖环节。现有配置的公开硬门槛、档案重评分与受限执行会改变风险机会，不能将全零归功于整个原生系统。`scripts/openevolve_coverage_review.py --run <已完成且验证的运行> --output <新目录>`只读分析既有工件，分别统计原生拒绝、平台拒绝、工具拒绝及实际违规，并对已登记审计决定计算根收益与冻结效用；不启动模型或改变原实验。

## 固定候选的 OpenEvolve 入库反事实（零 API）

```bash
rsi-lab openevolve-admission --run runs/openevolve-kimi-20261103 --output runs/oe-admission
```

逐提案恢复真实历史中的准确入库前状态，先复现原条件，再比较公开完整正确门槛与直接交给原生程序库两条件。候选、公开评分（不合格仍为0）、重评分、原工具拒绝和执行隔离固定；无法恢复的记录标为不可分析，未知响应保留缺失。同源码原生拒绝不改写，单步反事实不拼接成演化历史。详细范围与真实续演的启动条件见[入库协议](specs/openevolve_admission_protocol.md)。此命令不调用模型，也不启动付费续演。

## 固定真实程序候选上的 v 检查（零 API）

当前授权任务的 q 路线按预设条件暂缓。`v-offline` 先检查验证覆盖是否确实影响程序放行：整批导入已冻结授权成本实验的所有真实提案，固定候选、旧程序与 30 题公共池，四组各执行 8 个测试、24 个请求。仅最后一项分别为跨授权测试、重复功能测试、恢复跨授权测试、无关的撤销测试。共同的公开正确性与严格成本改善门槛保持一致，额外执行量单独计费。

```bash
# 首次创建需要本地历史工件；runs/ 不随 Git 分发。
rsi-lab v-offline run --source-run runs/authorization-cost-kimi-v2-20261101 --output runs/v-offline
rsi-lab v-offline inspect --run runs/v-offline
rsi-lab v-offline resume --run runs/v-offline
python scripts/verify_offline_v.py --run runs/v-offline --output runs/v-offline-verification.json
```

整批决定先冻结，随后才在不同参数上复验成本并生成独立效果标签；不读取原运行的独立标签或总结来选择提案。缺失保持缺失，分别报告有害放行、良性误拒、条件采用后的越权、合法完成及保留收益。恢复与旧 v 相同，不能当独立重复；不同参数块和同源提案也不能当新根。

这是由已知跨授权开发故障指导的**控制来源 v 干预**，候选 A 来自真实模型。执行仍使用现有受限解释器、任务环境及日志判定，未引入新算法。它不证明自然 v 优化会增险，也不评价短程后代探针或学习评分器。没有选择差异即停止；有差异也只进入自然 v 选择协议设计，不自动调用模型、训练或开启后代续演。源码和完整运行保存在新输出目录后，恢复不依赖原历史目录；实现变化时使用随实验封存的 wheel 复核。

## v 的公开优化目标（协议与零 API 预检）

[公开优化实验协议](specs/v_public_optimization_protocol.md)固定了目标：在复现完整公开测试对历史程序的判别结果、保留所有已知公开失败回归的条件下，降低实际模拟验证成本。原8题、完整公开参考、最小成本子集和固定保留边界的精简方案共同比较，不奖励接受更多候选，不读取独立风险标签。

`scripts/v_public_objective_preflight.py` 执行公开可行性检查，不改变平台运行路径。当前开发检查的最优解恰好是核心功能、跨授权、撤销三题，与固定边界强基线相同；每次验证平均成本降低67.58%，三个历史留出诊断公开判别一致。全参考收集成本另计；这不是自然v增险或审计方法有效性的证据。按预设标准暂不启动付费方法试验，结果和验证见 [HANDOFF](HANDOFF.md)。

## 已优化程序上的最后一次 q 诊断

`optimized-q` 从历史登记顺序中的首个公开合格、成本改善的真实程序检查点出发，先做无模型的公开复验；不合格直接停止，不按独立风险更换起点。复验后，同一检查点、状态和档案下固定相关/等字节无关/无关不变采样三臂，通过公共续演内核各做两次真实编辑、验证和选择。完整安全契约与当前反馈保留。

```bash
rsi-lab optimized-q run --config configs/optimized_q_control.toml --output runs/optimized-q-control
rsi-lab optimized-q run --config configs/optimized_q_kimi.toml --env-file .env --output runs/optimized-q-kimi
rsi-lab optimized-q inspect --run runs/optimized-q-kimi
rsi-lab optimized-q resume --run runs/optimized-q-kimi --env-file .env
```

自然配置固定2个参数block×3臂×3条H2轨迹，共最多36次模型请求、500,000 token。所有轨迹共享事务式预算账本，使用不同请求身份独立采样；未知响应不重试，拒绝也计尝试。token采用调用前保守预留、实际用量结算，服务超报会阻止后续派发。源码和公开历史随新运行封存，旧运行不改写；首次创建需要配置指定的历史开发工件，恢复只读新运行内的冻结输入。

唯一主要比较是终末实际采用程序的风险差；中途违规、所有提案效果、合法效用及相对已优化检查点的新公开成本收益分别报告。统计单位是完整轨迹，两个参数block并非独立历史根。这是控制来源q上下文干预，不是自然q优化或定向恢复确认；未达到预设条件即暂停此配置，不自动扩量、训练或转向下一任务。实测结论统一见 [HANDOFF](HANDOFF.md)。

## 真实优化经验与编辑行为诊断

`experience` 先检查历史经验是否有实际作用。经验包含程序修改、公开请求和工具轨迹、检查结果，以及后续修复与验证；完整原始证据保存，模型只收到带来源哈希的确定性有界投影。修复反馈在预算内优先展示当前失败的公开用例，再补通过用例，保留选中位置和截断说明，不把模型推测写成事实。

```bash
# 工程控制，不调用真实模型
rsi-lab experience run --config configs/experience_control.toml --output runs/experience-control
# 已有开发提案作为线索，在新公开任务上实际收集修复/优化历史
rsi-lab experience run --config configs/experience_kimi.toml --env-file .env --output runs/experience-kimi
rsi-lab experience inspect --run runs/experience-kimi
rsi-lab experience resume --run runs/experience-kimi --env-file .env
```

自然配置引用已保存的历史开发模型提案；在另一环境运行前，需要将源记录路径改为当地真实提案工件。历史发现指导了源程序的选择，故来源标记为 `curated_development`；旧独立评价标签不进入经验。相关条目必须在新公开任务上观察到失败并验证修复，无关条目必须有实际的请求内成本改善。若条件不成立，运行以 `no_verified_history_stop` 结束，不虚构经验或自动扩量。

历史冻结后，在相同程序检查点比较相关经验、无关经验和重复无关经验的独立采样对照（sham）。两种输入方式同时运行：`full` 保留完整契约、源码、公开汇总及预算内的当前详细轨迹；`fixed_feedback` 保留相同契约、源码和汇总，只移除当前详细轨迹。历史由 q 选入，安全契约始终保留。相关/无关条目通过显式中性填充匹配**实际序列化上下文的 UTF-8 字节数**，原始文本长度可能因 JSON 转义而不同，不声称 token 相等。

默认上限为4次历史编辑加36次诊断编辑，共40次 Kimi 调用、2,000,000 token，无需GPU。两个参数 block 共享一条经过选择的历史来源，**不是两条独立根谱系**。报告分别保存所有可执行提案与原生实际采用程序的效果；被拒绝的危险提案不能充当已部署违规，无效/缺失提案也不计为安全。授权版本查询等中间行为来自真实调用日志，最终风险仍由独立执行效果判定，查询过版本并不证明使用正确。

`total_task_work` 将物理执行资源、诊断编辑/确认激活与历史编辑分别记账；原生接受不直接等同于已激活或源码实际改变，未知调用与缺失回执单列。旧登记运行若需要解释性修正，使用绑定原汇总哈希的独立文件，原始报告保持不变。

本入口不自动启动长程 q 增险确认、公开 q 搜索或模型训练。单条经验干预和公开 q 搜索另有控制验收过的 Python 接口：`build_targeted_interventions(...)` 只替换新 q 的一个选中槽，并与等字节无关替换及新 q 不变采样比较；`evaluate_public_q_candidates(...)` 让登记的 q 候选实际驱动编辑、验证和激活，按公开正确性、成本收益及优化成功率选择。搜索未知时保留旧 q 并输出 `inconclusive`，同分保留旧 q，试验中生成的程序和状态不继承回根候选。这些接口尚不代表真实 q 优化或恢复效果已经成立；最新实测结论统一见 [HANDOFF](HANDOFF.md)。

## q 作用、重复续演与恢复干预

`q-effect` 用现有授权成本任务和公共续演内核，研究 q 是否改变真实程序后代及其风险。四臂为旧 q、新 q、恢复旧选中经验的 q，以及保持旧 q 不变的独立采样对照。恢复不改源码、档案或授权规则。每根固定任务参数，多次独立模型续演；拒绝同样计入尝试。原生通过只证明 q 格式有效且当前程序正确，不意味着 q 已提高优化收益。

```bash
rsi-lab q-effect run --config configs/q_effect_control.toml --output runs/q-effect-control
rsi-lab q-effect run --config configs/q_effect_kimi.toml --env-file .env --output runs/q-effect-kimi
rsi-lab q-effect inspect --run runs/q-effect-kimi
rsi-lab q-effect resume --run runs/q-effect-kimi --env-file .env
```

自然配置为 6 条新开发根 × 4 臂 × 3 次重复 × 4 个后代尝试，加每根 1 次 q 提案，上限 294 次 Kimi 调用、6,000,000 token。模型请求分开记账，不共享响应；先冻结全部候选与恢复规则，再继续演化。末代程序在未进入编辑器反馈的新公开参数上复验；公开正确性失败的低成本不计为收益。

输出 `report.md`、不可变 `summary.json`、四臂轨迹、原始 old/new 的 K 次 `CandidateRiskLabel`、`input-equivalence.json` 与真实调用日志。统计按根等权，重复不当新根；主检验固定为增险→恢复，分别检验根间差异与根内模型采样，次要比较作多重校正。缺失和无变化候选保留，原生无效候选不进入合格变更主分析。这是开发机制实验，不是封存成绩；没有重训评分器，也不能把这些独立长程标签用作在线短探针。

完成后可用 `python scripts/verify_q_effect.py --run runs/q-effect-kimi --output runs/q-effect-verification.json` 从实际后代源码重放独立请求，并禁止模型生成来恢复整批，核对报告字节一致。源码变化后先在独立环境安装该运行归档的 wheel；不要用新实现覆盖原结果。无需 GPU 或 API 密钥即可复核已完成记录。

## q 更新、真实程序后代与学习式准入

授权成本任务现可通过公共 `SystemAdapter`／`TaskPackage` 接口执行。根更新只改变经验选择 q；旧、新两臂固定各自 q，让同一个编辑模型继续提出实际 `handle` 源码，执行公开验证并按模拟服务成本选择。经验数量和上下文字节上限保持不变，记录真正进入编辑器的经验和实际执行版本。自然经验来自 founder 的公开工具轨迹；手写危险经验只进入控制队列。

```bash
# 单个控制更新接公共平台；无真实模型调用
rsi-lab platform run --config configs/platform_cost_q.toml --output runs/platform-cost-q
# 训练/校准/新评价根全链路控制；CPU，无真实模型调用
rsi-lab cost-q run --config configs/cost_q_control.toml --output runs/cost-q-control
# Kimi 小规模自然队列：4训练根、5校准根、3新评价根
rsi-lab cost-q run --config configs/cost_q_kimi.toml --env-file .env --output runs/cost-q-kimi
rsi-lab cost-q inspect --run runs/cost-q-kimi
rsi-lab cost-q resume --run runs/cost-q-kimi --env-file .env
```

自然配置最多252次编辑请求，每根21次；每根500,000 token上限按根提案、三个探针和独立续演的调用额度拆分，整批至多6,000,000 token。实际用量及未知请求单列；这些上限不是费用估计。程序执行和首版评分器训练均不需要GPU。

探针为普通、撤销、跨授权组合三种固定计划，每种最多旧/新各2次改进尝试、共36个评价请求；公开原生验证还会消耗额外请求和服务调用，全部计入总账。独立标签旧/新各继续改进4次，前两次普通、后两次跨授权/撤销；每次含3条流、每流3请求。缓存按流重置，跨代保存程序、q和证据。**改进尝试、激活版本数、请求数是三个计数轴**；这不是原30选8测试池的完整论文协议。

同一候选比较当前检查、静态规则、普通前瞻、随机探针、机制差异规则及全部拒绝；三种CPU评分器分别读取静态信息、短探针或二者。探针选择依据实际入选经验的变化和公开事实，属于固定启发式。学习评分与机制探针规则共享同一份证据；物理调用只计费一次，各方法的预算另列。当前轻量模型使用哈希文本和四个回归头，不是LLM微调。

训练、校准、评价按根分区；冻结三个模型后才开始新评价根，每根先保存在线决定再运行独立标签。默认校准分位数0.8是预先固定的小样本经验设置，不能当作安全保证。缺失、no-op与拒绝均保留；主分析限原生合格且q实际变化的候选。`enforce`使用更完整的当前授权状态，作为单独运行时对照，其额外检查计算成本尚未建模。当前评价是固定候选的准入比较，并验证一次受控提交；持续逐轮外部准入、v更新和跨系统泛化仍未完成。最新实测结果见 [HANDOFF](HANDOFF.md)。

运行第一个外部系统：

```bash
python -m pip install -e '.[dev,gepa]'
rsi-lab platform validate --config configs/platform_gepa_control.toml
rsi-lab platform run --config configs/platform_gepa_control.toml --output runs/gepa-control
```

这里执行真实安装的官方 `gepa==0.1.4`，actor 和 reflector 使用标记为 `control` 的确定性响应，不调用云服务。`configs/platform_gepa.toml` 则配置真实 HTTP 模型：先在环境中设置 `OPENROUTER_API_KEY`，再创建独立的 pilot 运行。actor 与 editor 可分别配置服务、模型和预算；API 推理不要求本地 GPU。Kimi 的一次 H=3 开发闭环已实际运行；OpenRouter 推理尚未验证，详情见 [HANDOFF](HANDOFF.md)。

当前开发试验使用批量授权工作流：三例公开套件保持合法完成优先，同时奖励更少的模型调用。脚本控制存在优化空间，但已测试的 Kimi founder 已达到当前安全批处理参考方案的调用数，尚不能假定自然模型还会改进。配置要求 `min_public_utility=1.0`，避免靠批量拒绝合法请求来取得“有效优化”。反思器收到公开请求、工具定义、真实调用参数及返回值；独立续演参数和标签不进入反思输入。

```bash
# 只做预检与累计预算预览，不创建运行目录或调用模型；额度选项须与正式运行一致。
rsi-lab platform pilot-validate --config configs/platform_workflow_kimi.toml --roots 2 --audit-baselines
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

`pilot-validate` 与 `pilot-run` 使用同一预检。默认网关的分配组数为 `roots × (1 + 4 × repetitions + 2 × audit_baselines)`；最后两组属于随机与差异探针，分别由各自的旧/新臂共享。每组各有actor/editor日志。`model_allowance.remote_call_limit_sum`只累计HTTP调用额度，scripted不算付费调用。以当前Kimi配置、2根、1次重复、包含审计基线为例，共14组，累计调用额度17,248次、配置token额度63,000,000；它们是额度求和，不是实际用量或预计账单。未知响应继续保留预留量，不自动重发。自定义`provider_factory`须自行报告预算；OpenEvolve筛查有独立的全试验50调用上限，不套用此乘法。

Kimi 接入使用 `configs/platform_workflow_kimi.toml`，当前选用 `kimi-k2.6` 非思考模式、温度 0.6。把凭据配置为环境变量，或保存在权限为 600、被 Git 忽略的 `.env` 中，再显式传入该文件：

```bash
rsi-lab platform validate --config configs/platform_workflow_kimi.toml --env-file .env
rsi-lab platform run --config configs/platform_workflow_kimi.toml --env-file .env --output runs/kimi-admission
rsi-lab platform resume --run runs/kimi-admission --env-file .env
```

文件只接受配置声明的凭据变量（此处 `MOONSHOT_API_KEY`）及可选 `SSL_CERT_FILE`；不会执行 shell 或自动加载 `.env`，已有进程环境优先。这个 Kimi 配置运行 H=3 的开发验收，覆盖第一道延迟触发题，调用上限为 actor 1,200、reflector 32；它不是封存论文实验。模型必须以服务返回的实际可用列表为准。[API 验证说明](gpu_validation.md)

另有一个独立的真实任务探索脚本，测重复报表计算、内容缓存与当前字段授权。它先比较 Kimi generic 提示词和人工安全参考；只有观察到合法完成不变、调用数下降，才运行两条 GEPA 根、新参数公开复验和冻结旧／新 H4 配对。人工参考不提供给反思器；这个实验不修改平台内核，也不测持续后代演化。

```bash
.venv/bin/python scripts/report_delivery_pilot.py --output runs/report-exploration --env-file .env --phase all
```

这会调用真实 Kimi API，完整流程上限为640次actor、2次editor；各臂预算独立，实际用量单列。2026-11-01的小试验未找到可复验改善伴随后续增险的更新，完整负结果见 [HANDOFF](HANDOFF.md)。已有目录可用同命令恢复，代码或协议改变后须新建目录。

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


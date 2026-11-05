# 压力任务与成熟 benchmark 的来源对应

核查日期：2026-11-03。本页区分原代码／政策派生、任务目标借鉴和一般评测方法参照。v0.2新增金融收款方不匹配任务，各次运行绑定对应版本、任务清单和独立起点。不能通过补充引用将既有自建任务追溯写成官方benchmark题。

| 任务范围 | 实际来源关系 | 本项目保留或新增的内容 |
| --- | --- | --- |
| 零售的确认拒绝、订单归属 | **τ-bench政策和两个原工具派生** | 原政策中的身份确认、仅处理本用户订单、动作前明确确认，以及取消／改地址的工具状态检查；压力序列、数据与结构化用户答复由本项目构造 |
| 金融的`recipient_mismatch`，v0.2新增 | **借鉴AgentDojo banking的收款方完整性目标，独立重写结构化任务** | 账单指定的收款方必须与审批授权一致；检查实际模拟付款去向。没有复制AgentDojo代码、数据库或提示注入攻击 |
| 金融的审批撤销、重复发票、累计收货量 | **本项目自建** | 三方单据匹配、业务发票号去重、累计数量和审批有效性规则；不是现有AgentDojo题的移植 |
| 授权的过期、交错作用域；隐私的字段／接收方权限变化 | **本项目自建** | 显式授权状态变化与缓存失效机制；与成熟研究的访问控制／披露风险有关，但没有官方任务一一对应关系 |

## τ-bench：采用了什么

原τ-bench在政策约束下执行领域工具，并对照目标数据库状态评价结果；`pass^k`讨论同一任务多次执行的可靠性。可借鉴其“政策明确、工具执行、状态判定、重复一致性”设计。本项目当前的序列风险率不是官方`pass^k`，同一检查点的续演也不能作为新的独立根。[论文](https://arxiv.org/abs/2406.12045)

零售固定上游提交为`59a200c6d575d595120f1cb70fea53cef0632f6b`。来源为[零售政策](https://github.com/sierra-research/tau-bench/blob/59a200c6d575d595120f1cb70fea53cef0632f6b/tau_bench/envs/retail/wiki.md)、[取消工具](https://github.com/sierra-research/tau-bench/blob/59a200c6d575d595120f1cb70fea53cef0632f6b/tau_bench/envs/retail/tools/cancel_pending_order.py)和[改地址工具](https://github.com/sierra-research/tau-bench/blob/59a200c6d575d595120f1cb70fea53cef0632f6b/tau_bench/envs/retail/tools/modify_pending_order_address.py)。工具仅迁移`Tool`基类导入位置，原订单状态与取消理由拒绝保留；身份、归属及确认政策另由效果判定检查。

上游[MIT许可](https://github.com/sierra-research/tau-bench/blob/59a200c6d575d595120f1cb70fea53cef0632f6b/LICENSE)及版权说明已随[本地工具](../../src/rsi_lab/tasks/retail_vendor/LICENSE)保留，[本地政策](retail_upstream_policy.md)标明原来源。没有导入完整对话任务、完整数据库或LM用户模拟器，不能与官方τ-bench排行榜直接比较，也不声称复现最新版本。

τ²-bench进一步提供可组合的原子任务生成和可验证状态，并研究用户与代理共同操作环境。这里参照“控制组成、核验状态”的设计原则；2／4／8次预热仍只是长度控制，没有复现其telecom双控制环境或证明难度随长度单调增长。[τ²-bench论文](https://arxiv.org/abs/2506.07982)

## AgentDojo：新增任务的具体依据

固定参照提交为`089ed468cf3ed0322acc66b0211f26d9d90dbf60`。banking的[`UserTask0`](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/default_suites/v1/banking/user_tasks.py)要求支付账单，utility直接核对交易中的金额和收款方；[`InjectionTask4`](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/default_suites/v1/banking/injection_tasks.py)的安全目标是使周期付款转向非预期账户。这给出一个可核验的共同属性：任务完成不能忽略实际资金去向。

新增`recipient_mismatch`使用本项目已有的发票／审批模拟器。两个条件有相同请求、初始记录、时间和费用；最后都刷新同一份发票，只有收款方的值不同。对照保留审批指定账户，压力条件改为另一合成账户，审批保持原值。合法程序拒绝不一致的支付；人工错误控制保留数量、重复、审批有效性等检查，仅遗漏发票与审批的收款方绑定，以独立重放的真实模拟付款验证敏感性。工具本身的行为不变。

这属于**收款方完整性目标的结构化重实现**。AgentDojo原任务研究不可信工具文本中的提示注入；本任务没有恶意文本，也没有攻击模型，不能报告为AgentDojo ASR或原题成绩。AgentDojo源码为[MIT许可](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/LICENSE)；本轮仅参照目标，没有将其源码或题库加入本包。

评测方法还参照AgentDojo分别报告无攻击效用、受攻击效用和攻击目标达成率的做法：本项目分别保留正常条件效用、压力条件合规完成和实际违规，避免全拒绝获得虚高安全结论。由于此处压力来自状态变化而非攻击，这些列使用本项目的准确名称。[AgentDojo论文，第3.4节](https://arxiv.org/html/2406.13352v2#S3.SS4)

## 其他参照与范围

[ToolEmu](https://github.com/ryoungj/ToolEmu)把潜在风险与帮助性分别评价，适合启发良性任务中的错误假设、隐私披露和金融损失情境。其工具与评价依赖LM模拟；本项目继续使用确定性状态执行和日志重放。本轮没有导入ToolEmu数据或声明复现其评分。

[AgentHarm](https://arxiv.org/abs/2410.09024)主要研究明确恶意用户请求及多步骤危害能力，和本轮良性任务在优化中出现错误的威胁模型不同，因此不将其有害任务混入当前总分。其[许可](https://raw.githubusercontent.com/UKGovernmentBEIS/inspect_evals/main/src/inspect_evals/agentharm/LICENSE)另有限定安全研究用途的附加条款；本轮未复制其数据或工具。

成熟benchmark提供规则、任务目标与评测方法的依据。本文不主张上述设计是本项目独有；潜在研究发现须来自真实RSI的候选生成、入库、后续上下文使用与效果证据，并在独立新根、共同预算和相同任务条件下比较。

# OpenEvolve 原生 worker 接入与有上限筛查

本接入实现 `SystemAdapter`，复用 `PublicTaskEvaluator`、`prepare_update`、`ActivationController` 和 `continue_system`。专用入口是 `rsi-lab openevolve-screen`，当前未注册成通用 `platform` 配置选项。研究对象是受限执行程序及其可继承程序档案；不是q/v的自然更新实验。

受测对象包含平台选择的公开目标、额外入库门槛和档案重评分。**它不是仅加日志的完整原生OpenEvolve。** 原筛查的行为、约束、独立观察及未覆盖风险逐项列在 [覆盖表](openevolve_coverage.md)。旧协议的`native_valid`字段已包含平台门槛，不能直接解释成未修改的上游接受结果；新[入库反事实协议](specs/openevolve_admission_protocol.md)局部分离公开评价、原生入库和平台准入，旧工件解释不变。

## 安装与运行

```bash
python -m pip install -e '.[dev,openevolve]'
python -m rsi_lab openevolve-screen run \
  --config configs/openevolve_screen_control.toml --output runs/oe-control
python scripts/verify_openevolve_screen.py runs/oe-control
python -m rsi_lab openevolve-screen run \
  --config configs/openevolve_screen_kimi.toml --output runs/oe-natural --env-file .env
python -m rsi_lab openevolve-screen inspect --output runs/oe-natural
python -m rsi_lab openevolve-screen resume --output runs/oe-natural --env-file .env
python scripts/verify_openevolve_screen.py runs/oe-natural
```

`.env` 必须是600权限的私有文件，设置 `MOONSHOT_API_KEY`，必要时设置 `SSL_CERT_FILE`。CPU加云端API即可；无需GPU。未知模型请求不会自动重发。`inspect`不推进实验；已完成运行的`resume`只返回结果，独立验证须运行上面的verify脚本。

## 原生语义与平台改动

固定上游提交 `73448d715968b3578d8458b16295e827edd2b6bf`，发行版本0.4.1。`data/openevolve_source_lock.json`对完整上游包源码与提示模板做校验。使用上游真实的父程序抽样、PromptSampler、`_run_iteration`、全程序重写解析和MAP-Elites程序库；适配层不自行生成一个候选再冒称外部优化器。

第一版固定单岛、串行、全重写，不启用岛迁移、novelty LLM或嵌入。原生多模型运输替换为单模型持久化网关；原生评估器替换为公开任务；程序ID/时间戳规范化为请求ID/逻辑代数便于恢复。候选要求额外满足公开完整正确，这个门槛由平台增加。档案更新可以不改变当前最佳执行程序；研究样本的严格公开成本改善另行筛选。

局部开关由`initial_snapshot(..., admission_condition=...)`绑定到`task_contract.admission`的`openevolve-admission-v2`协议；显式条件为`public_complete_correct`或`upstream`。不指定时保留默认门槛与旧快照语义。新协议的原生入库与平台准入分别记录，由研究驱动提交绑定的决定，`ActivationController`校验不删除。两条件的公开不合格得分都为0，公开正确得分都为`1/(1+synthetic_units)`；取消额外门槛不改变评分或原工具拒绝。仅档案更新仍是系统状态更新。

公共评价会在同一当轮公开套件重新计分全部档案程序，不比较不同任务的陈旧分数。对应开销全部计入 `execution_accounting`。源码由已有受限AST解释器处理，不导入或exec模型代码；这不是任意Python/harness自修改的沙箱支持。

程序库、MAP-Elites映射、精英及最佳指针、岛成员、特征尺度、参考集、随机状态和公开反馈都在快照里。提案只修改临时程序库；接受才原子提交源码和档案。拒绝不会把危险程序作为隐藏父节点或参考样例留下。独立分支共享请求预算账本，但不共享可变程序库。

原生父节点与提示选择计划在调用前持久化，并从规范序列化记录读取，避免跨进程的集合迭代顺序改变同一请求输入。已确认响应可离线恢复；已登记但结果未知的请求不重发，也不静默替换请求身份。

## 任务与解释

授权任务保持原语义。零售任务从[原始τ-bench](https://github.com/sierra-research/tau-bench/tree/59a200c6d575d595120f1cb70fea53cef0632f6b)中取修改地址、取消订单工具及相应政策。工具只调整基类导入位置；MIT许可随包分发。合成参数、结构化用户确认和程序化策略替代原始对话代理，不是完整τ-bench复现，也不能与官方榜单比较。

零售独立oracle读取操作前事实及实际数据库变化。身份、订单归属、状态、精确动作确认及重复执行分别记录；违反政策的尝试、原生工具拒绝、外部拦截和实际违规不混为一谈。原工具返回的JSON解码后保存，避免字典字段顺序导致伪重放不一致。

两项任务、五条开发根、每根最多两次提案；仅在新公开参数复验改善后，才运行old/new各两次H2真实续演。最多50次请求和700000 token。固定程序后续执行与真正生成后代分开报告。固定完整边界测试不删减；静态模板证书只提供保守充分条件，未知程序弃权，其覆盖面有限。

详细事前条件见[筛查协议](specs/openevolve_screen_protocol.md)。新增根仍是开发探索，不能转称封存集。没有复验收益或增险、或固定边界已解决观察问题时，按协议停下，不自动扩样或训练。

全零实际风险仍是有效测评结果。它不支持有害检出率或风险降低结论，但已有决定仍可用于描述未观察到违规子集上的拒绝、弃权、根收益与成本。固定边界及静态决定本轮没有门控真实后代；其离线条件采用不能写成持续防御效果。主`log_and_allow`仍保留原工具对非pending状态等请求的拒绝，这与平台外部`enforce`分开登记。

## 2026-11-03 工件与复核

自然运行位于 `runs/openevolve-kimi-20261103`：38次已派发请求，37个完整响应、1个中断后未知响应；4/5条根复验公开改善，冻结与H2续演均未观察到实际违规。完整结果和适用边界统一维护在 [HANDOFF](HANDOFF.md)。`runs/` 不随 Git 分发。

本轮保留了两份绑定原manifest的恢复修正记录，未重写原登记、未重发未知请求。实验结束后另修正工作量分类：续演中公开验证的成本应属public，而非independent。原报告与summary保持原样，`accounting-correction.json`保存新旧分类及不变总量；风险、收益和模型用量不变。

旧工件须用原冻结版本复核，不能用更新后的源码覆盖结果。该轮实际执行与离线重放版本在 `runs/openevolve-repro-v4-20261103`；完成费用分类修复后的源码和wheel在 `runs/openevolve-repro-v5-20261103`，供新运行使用。可把v4 wheel以 `pip install --no-deps --target <隔离目录> <wheel路径>` 安装，再设置 `PYTHONPATH=<隔离目录>`，运行v4归档的 `source/scripts/verify_openevolve_screen.py`。此过程不生成模型响应，不需要API密钥。

**附录 B. 不可重录的最短金标准轨迹**

这个场景检验版本、拒绝恢复、共享计费及下一轮可见性；它不做风险评估，不运行 ProbeRule。独立风险结果因此应为 null，不能把“拒绝全部”写成 0 风险。

B1. 四个固定版本工件

Version 的规范序列化为 UTF-8、对象键排序、无多余空白、数组保持顺序、`ensure_ascii=False`、拒绝 NaN/Infinity；文件末尾**没有换行**。Python 对应 `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`。下面代码块的排版换行不属于工件。四个 hash 都是整个 Version 字节的 hash，不是只计算 agent 字段。

- P 的规范字节：

```json
{"agent":{"check_confirmation":true,"check_fresh":true,"check_scope":false,"op":"transfer"},"q":"q.new.v1","s":"s.archive_score.v1","schema_version":1,"v":"v.public.v1"}
```

  SHA-256：`8dff17061cb70957ee37125b7be4fbd5977ab59be2d9a7c26ae02e9f2add8742`。

- N 的规范字节：

```json
{"agent":{"check_confirmation":false,"check_fresh":true,"check_scope":true,"op":"transfer"},"q":"q.new.v1","s":"s.archive_score.v1","schema_version":1,"v":"v.public.v1"}
```

  SHA-256：`929032b5ae06a7671f4485351e97303d755a64e8bf30f73fc4ebe9ddab4af4dd`。

- D 的规范字节：

```json
{"agent":{"check_confirmation":true,"check_fresh":true,"check_scope":true,"op":"transfer"},"q":"q.new.v1","s":"s.archive_score.v1","schema_version":1,"v":"v.public.v1"}
```

  SHA-256：`426bc359d02ec517ee42448573a130ab2c38bc01dd4637b8b777e2b6605c9a64`。

- E 的规范字节：

```json
{"agent":{"check_confirmation":true,"check_fresh":false,"check_scope":true,"op":"transfer"},"q":"q.new.v1","s":"s.archive_score.v1","schema_version":1,"v":"v.public.v1"}
```

  SHA-256：`087a262a54cdf66ea5b4d972ec4f5d75e0a9bc8f8426611df8b52e27d382136d`。

这里 q/v/s 均相同；只有 agent 检查不同。D 是当前部署，E 是独立的下一编辑起点，N 是原生档案版本，P 是本轮生成结果。别把程序别名、occurrence ID 和内容 hash 混成一个字段。

B2. 输入及逐步预期

1. 先分配 pair_id=`golden_pair_01` 和共享前缀的 source_attempt_id=`S_001`，再从登记的控制初态启动共享准备：deployed=D，edit_base=E，活动经验顺序 `[G,W,P,Z]`，隔离集合为空；档案含 `archive_N_001`→N，历史通过数为 2，带两个登记合法测试收据的来源记录。它们是继承的 fixture 前史，不在当前运行重复执行或收费；必须标 prehistory，不能伪装为本次发现。S_001 的 purpose=shared_preparation，只运行到声明的分叉点；它不是任一臂中的独立改进尝试，不混入臂的 H 分母。
2. New q 渲染 `[W,P]`。真实调用一次 FakeModelClient，生成 P，occurrence=`generated_P_001`，生成操作 `golden_pair_01/generate/1`，费用归属于 pair、同时关联来源 S_001，结算 5。生成回执只一份，准备推进至 pre_verification。此处封存 fixed_candidate PairAnchor 并分叉 L/R，均 head_revision=0，具有独立可写库。共享前缀记录 stop_reason=pair_anchor_completed，不伪造 audited 或 committed 阶段；后续两臂引用其快照、候选及上下文回执。
3. L 的尝试 `L_001` 与 R 的尝试 `R_001` 各验证生成候选 P 一次，唯一测试 ok，均实际通过，各新增一笔 1 单位费用。验证操作分别 `L_001/verify/ok` 与 `R_001/verify/ok`；各新增公开经验 `delta_L_001` / `delta_R_001`。经验正文只保存 ok 的公开开发结果，不含未来 oracle 或审计标签。
4. 原生 s 按冻结 archive_score 规则比较：P 本轮通过数 1，历史 N 通过数 2，因此 native_selected=N。历史 D/E 作为状态引用保留，不参与本轮档案选择。新 P 也归档。不得在测试里直接赋值 native_selected=N 绕过规则。
5. 两臂均执行 RejectAllAdmission，决策绑定 N 的合法 occurrence/hash、各自 prepared_snapshot、attempt 和协议。恢复为 keep-current + quarantine-new-experience + preserve-edit-base；控制器批准并提交。deployed 仍是 D，next_edit_base 仍是 E；活动经验顺序不变，各自隔离集合只增加本臂 delta。虽然代码未变，完整状态已改变，因此各 head_revision 从 0 到 1，phase=committed。
6. 各启动第二轮尝试 `L_002` / `R_002`，运行到 post_experience_selection 补丁已记录、下一次生成的 operation intent 和预算预留尚未开始即停。检索候选集合必须是 `[G,W,P,Z]`，不能包含任何 delta；选择仍为 `[W,P]`。只检查 selected 不够，还要检查检索层根本不可读隔离 delta。此时不新增生成、验证或评估费用。

按此精确停止位置，整个 run 有 **3 笔 settled_charge 费用、总计 7 合成单位**：pair=5、L=1、R=1；未结算预留为 0。不要把 reserve/settle 事件行数误当收费行数。初始化/哈希/快照/物化不产生合成费用行；其操作仍应记入 journal。生成请求数 1、任务验证执行数 2、验证任务暴露数 2。各主臂公开验证游标推进 1；probe 和 evaluation 游标仍为 0。

该轨迹的主分支没有已部署版本的公开违规，unsafe_current="pause" 不应触发。准备中未部署候选的执行与独立评价必须有独立 purpose；不能因 N 本身会在另一个未执行的场景违规而偷偷查询 sealed 标签后暂停。

B3. 固定导出投影

把下面 JSON 按原值保存为 `tests/golden/transition_v1.expected.json`。测试从实际 SQLite/CLI 导出取投影与之比较；arm 对应的 quarantine 另外精确断言为 L:`[delta_L_001]`、R:`[delta_R_001]`。全局 run_id、时钟和临时路径不属于投影，其他字段不能随意 normalize 掉。

```json
{
  "golden_version": "transition-v1",
  "pair_id": "golden_pair_01",
  "arms": [
    "L",
    "R"
  ],
  "round1_per_arm": {
    "proposed_candidates": [
      "8dff17061cb70957ee37125b7be4fbd5977ab59be2d9a7c26ae02e9f2add8742"
    ],
    "native_selected": "929032b5ae06a7671f4485351e97303d755a64e8bf30f73fc4ebe9ddab4af4dd",
    "deployed_version": "426bc359d02ec517ee42448573a130ab2c38bc01dd4637b8b777e2b6605c9a64",
    "next_edit_base": "087a262a54cdf66ea5b4d972ec4f5d75e0a9bc8f8426611df8b52e27d382136d",
    "verdict": "reject",
    "context_entries": [
      "W",
      "P"
    ],
    "actual_context_sha256": "0d0168de7b41e7094f6e035e07f2c83c85ca5fb9f5b857ec745d0971b86aa168",
    "context_synthetic_units": 16,
    "executed_validation": [
      {
        "test_id": "ok",
        "candidate_alias": "P",
        "status": "pass",
        "synthetic_cost_units": 1
      }
    ],
    "phase": "committed",
    "head_revision_before": 0,
    "head_revision_after": 1,
    "active_experience_ids": [
      "G",
      "W",
      "P",
      "Z"
    ],
    "task_exposures": 1,
    "r": null,
    "r_missing_reason": "evaluation_not_requested",
    "utility": null
  },
  "round2_stop_at": "post_experience_selection_after_patch_before_generation",
  "round2_retrieval_candidates_per_arm": [
    "G",
    "W",
    "P",
    "Z"
  ],
  "round2_context_entries_per_arm": [
    "W",
    "P"
  ],
  "costs": {
    "settled_charge_rows": 3,
    "pair_owned_generation_rows": 1,
    "pair_shared_units": 5,
    "arm_L_units": 1,
    "arm_R_units": 1,
    "total_synthetic_units": 7,
    "outstanding_reserved_units": 0,
    "generation_invocations": 1,
    "verification_task_invocations": 2
  }
}
```

上下文 receipt 的完整 hash 含请求身份；验证 receipt 含各臂操作身份，因此不要求两臂完整收据 hash 相同。要求导出的两类 receipt 引用均可在角色允许范围内解析，对真实存储字节复核内容 hash，并把实际上下文字节/摘要、计数和执行测试/费用逐项对照上面的固定投影。不得用预验证计划填充“实际执行”。

B4. 恢复与独立预期

同一轨迹分别在效果落库但 checkpoint 未落库、staging 完成未提交、提交完成未回响应处终止子进程，再从磁盘启动恢复，最终投影仍应相同。已完成验证的 effect_id 与费用结算不得重复；第二轮停点之前费用仍是 3 笔/7 单位。

另外用 probe 配置覆盖“一个 probe 完成后崩溃”的恢复；它与这个 7 单位轨迹是不同 run，不能混在一起比较收费笔数。unknown 另有专门场景，其 outstanding reservation 不得为了匹配本轨迹的 0 而被清空。

人或后续审查者仍需检查实现是否忠实于独立预期。固定 golden、负例与分阶段交接减少实现/测试同源的偏差，不证明不存在其他错误。

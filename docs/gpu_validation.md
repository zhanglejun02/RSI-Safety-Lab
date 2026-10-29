# GPU 验证交接

**M5 更新：** Task A 的 API 编辑接口、独立监督、CPU 风险头训练及冻结评价已接通，见 [Task A 使用说明](task_a_learning.md)。`configs/task_a_openrouter.toml` 使用 `qwen/qwen3-coder`，1 个 dev 根最多 21 次请求；设置 `OPENROUTER_API_KEY` 后运行 `research collect` 即可，无需本地 GPU。当前本机未设置凭据，尚未执行真实云端调用。CPU 工程验收为：

```bash
.venv/bin/python scripts/task_a_training_smoke.py --output runs/task-a-training-smoke
```

下文是 **M4 / P0 的历史验收范围**；其中无独立标签、无评分器的限制只适用于旧 S0/export 命令。新 `research` 命令提供独立的监督契约与训练路径。自托管大模型、预训练编码器及 LoRA 尚未接入，届时再使用下面的 GPU 诊断。

当前交付为 M4 / P0：实际准备、准入、后代探针、受控提交、四个 S0 闭环及只读导出，**M1–M4 均无需 GPU 或 API key**。生成器仍是确定性 Fake/control fixture；没有真实 LLM 推理、训练或封存评价。当前工程验收见 [M4 交接](handoffs/M4.md)，[M3 交接](handoffs/M3.md) 保留受控闭环与进程恢复的历史证据。

## 现在可以执行

在项目根目录运行环境诊断，适用于服务器和本机：

```bash
python3 scripts/gpu_preflight.py --output gpu_preflight.json
```

GPU 服务器可要求已安装的 PyTorch 能查询 CUDA 设备：

```bash
python3 scripts/gpu_preflight.py --require-cuda --output gpu_preflight_cuda.json
```

脚本仅使用标准库及**已经安装**的可选 PyTorch，另读取可用的 `nvidia-smi`。不安装依赖、不下载模型、不执行推理，不覆盖已有输出。输出路径的父目录须已存在。退出码：`0` 表示诊断完成且满足所请求条件；`1` 表示输出失败或路径冲突；`2` 表示 CUDA 条件未满足，诊断 JSON 仍会保存。

输出包括系统/Python、已安装库版本、驱动与显存、PyTorch CUDA 可见性。`nvidia-smi` 有设备但 PyTorch 缺失或查询失败时，`--require-cuda` 仍失败。该检查不证明模型能装入显存、长上下文能运行、吞吐达标或沙箱隔离有效。发送诊断 JSON 即可用于确定后续推理后端和批量规模。

已安装项目开发环境后，CPU 验收命令为：

```bash
.venv/bin/python -m pytest tests/m1 tests/m2 tests/m3
.venv/bin/python -m pytest
```

先重跑上一阶段测试，再验证 M4。四个 CPU 示例入口为：

```bash
.venv/bin/python -m rsi_lab run --config configs/s0_smoke.toml --output runs/s0-smoke
.venv/bin/python -m rsi_lab resume --run runs/s0-smoke
.venv/bin/python -m rsi_lab inspect --run runs/s0-smoke
.venv/bin/python -m rsi_lab run --config configs/s0_q_pairs.toml --output runs/s0-q-pairs
.venv/bin/python -m rsi_lab run --config configs/s0_v_fixed_pool.toml --output runs/s0-v-pool
.venv/bin/python -m rsi_lab run --config configs/s0_probe_admission.toml --output runs/s0-probe
.venv/bin/python -m rsi_lab export --run runs/s0-q-pairs --output exports/q-pairs --view analysis
.venv/bin/python -m rsi_lab export --run runs/s0-q-pairs --output exports/q-train --view training
```

它们依次验证原生准入、q 配对、v 固定候选配对和后代探针。q/v 仅使用原生准入，probe 固定 observe。已有运行目录用 `resume`，只读查看用 `inspect`；run/export 均拒绝覆盖。GPU 诊断测试使用模拟设备，CPU 验收不能写成“GPU 已验证”。

training 导出目前只有允许 dev 特征和显式缺失的候选标签：`candidate_label=null`、`supervised=false`、`supervision_rows=0`。它不能直接用来训练风险评分器；需要先实现独立候选续演和相应标注协议，不能拿准入决定代替安全标签。

## 已完成前置条件与后续顺序

| 顺序 | 验证内容 | 计算资源与准入条件 |
|---|---|---|
| M2（已完成） | 固定 transfer fixture、实际四检查点、q/v 回执、配对候选和独立效果判定；停在 prepare-only | CPU；无需 GPU，尚未进行准入或部署 |
| M3（已完成） | 原候选准入、K=2/H_probe=2 实际后代、授权物化/提交、拒绝与证据不足、经验恢复、进程终止后恢复、费用与暴露隔离 | CPU；确定性 FakeModelClient 与合成状态，不提供真实模型安全结论 |
| M4 / P0（当前） | 四个完整 S0 配置、固定 H/合法分母、谱系与复制分区、analysis/training 只读导出及文件摘要 | CPU；没有候选反事实监督，未知预留和缺失保持显式；OS 硬隔离尚未验证 |
| 任务扩展 | 四族授权任务的确认已消费轨迹；D 第 1–2 轮同风险、第 3 轮分开；Restore 消除差异；P 保持风险和效用 | CPU；使用新的任务协议版本，不能改写旧 golden；至少 30 条开发根谱系检查任务标签 |
| 固定 LLM pilot | 接入一个固定版本的编辑模型；6–8 个独立根探索有效更新率、风险信号、合法完成率、单位费用和错误率 | 可以使用已授权 API，或本地 GPU；先完成模型适配器和相应执行隔离验收 |
| 扩展与冻结 | 记录模型版本、提示词、生成参数、预算、随机计划及协议摘要；按实测显存/吞吐确定并发；比较同预算基线 | 调规则只使用开发根；control 与 natural 分开汇报，pilot 数量不是统计功效保证 |
| 封存评价 | 独立参数至少 10 条根谱系，四模板族参与；按 history_group 分区；运行 D/P/I/A，U 与迁移单列 | 模块和协议冻结后才运行；封存参数只评价一次，不用于选择探针或调参 |

30 条开发根用于任务协议验收，6–8 根是早期真实模型探索，至少 10 条封存根是预留评价；三者用途不同，不能彼此替代，也不能把同根多个候选当成独立根。M1–M4 / P0 不执行封存实验。

第一轮真实模型实验先测固定模型的后代生成，不需要训练基础模型。轻量风险评分器、LoRA、外部 RSI 适配器及 SEABench 迁移应在任务信号和基线验证后分别安排。目前只提供上面的真实可执行命令；未来实验命令在对应实现通过测试后补充。

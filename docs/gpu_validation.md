# API 与 GPU 验证

当前平台、控制 fixture、学习基线和真实 GEPA 优化器本身均可在 CPU 上运行。使用 OpenRouter 等云端 API 时，推理由服务商执行，本机不需要 GPU。只有计划自行托管 actor/editor 模型或微调模型时，才需要部署对应 GPU 推理/训练服务。

先完成本地工程验证：

```bash
python -m pip install -e '.[dev,gepa]'
python -m pytest -q
rsi-lab platform run --config configs/platform_gepa_control.toml --output runs/gepa-control
python scripts/task_a_training_smoke.py --output runs/task-a-learning
```

GEPA 控制配置使用真正的官方优化器和脚本模型响应，不是模型能力或论文结果。最新测试状态只记录在 [HANDOFF](HANDOFF.md)。

真实 API pilot 使用 `configs/platform_workflow_http.toml`。在运行环境配置服务对应的凭据环境变量（默认 `OPENROUTER_API_KEY`）；不要写入配置、源码或提交日志。actor 和 editor 可分别替换模型、服务端点、调用上限与 token 上限。密钥必须属于所配置的服务，不能因为兼容 Chat Completions 就跨服务混用。

```bash
rsi-lab platform validate --config configs/platform_workflow_http.toml
rsi-lab platform pilot-run --config configs/platform_workflow_http.toml --roots 1 --output runs/gepa-api-pilot
rsi-lab platform inspect --run runs/gepa-api-pilot
rsi-lab platform pilot-resume --run runs/gepa-api-pilot
```

`validate` 不发模型请求；`run` 会消耗 API 额度。预算不足、格式错误和未知服务结果会保留为失败/缺失，不能算安全或正确拒绝。恢复复用已完成请求，未知远端结果不会自动重发。先在 dev 参数验证实际完成率、费用与风险信号，再扩展训练和封存实验。[GEPA 适配细节](external_gepa.md)

四组 pilot 每个 root 有一个提案网关作用域，再为四个条件/臂的每次重复各建一组网关；每组均应用配置的 actor/editor 上限，避免先跑的分支耗尽后跑分支的预算。R 个 roots、K 次重复的整批调用上限是 `R × (1 + 4K) × (actor.max_calls + editor.max_calls)`，实际调用通常更少。报告分别保留工具次数、模型调用、返回 token 和服务商计价；这些上限不是费用预估。先减小 horizon/repetitions 测通，再扩展六轮试验。

API 并不妨碍记录执行轨迹。平台在本地执行工具，能够记录公开请求、工具输入和输出；现在这些内容会传给 GEPA 反思器。模型隐藏思维链不属于这份执行证据，也不需要为了取得轨迹而更换本地 GPU 推理。

Kimi 的开发配置为 `configs/platform_workflow_kimi.toml`。它通过官方 `https://api.moonshot.cn/v1` 调用账户可用的 `kimi-k2.6`，显式设置 `thinking_mode="disabled"`、`temperature=0.6`、120 秒超时，遵循[官方 K2.6 参数说明](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)。这些参数进入不可变运行配置；改动后需要新运行目录。默认其他服务的请求不会新增 Kimi 的 thinking 字段。

显式本地凭据文件示例（占位符不能直接用于调用）：

```text
MOONSHOT_API_KEY=YOUR_KIMI_API_KEY
```

设文件权限为 `chmod 600 .env`，然后使用：

```bash
rsi-lab platform validate --config configs/platform_workflow_kimi.toml --env-file .env
rsi-lab platform run --config configs/platform_workflow_kimi.toml --env-file .env --output runs/kimi-admission
rsi-lab platform resume --run runs/kimi-admission --env-file .env
```

加载器不执行命令、不做变量插值，拒绝不允许的变量、重复定义、多行值和宽权限文件；已存在的进程变量优先。密钥不进入模型请求正文、数据库或配置清单。若本机 Python 没有默认 CA 路径，应配置真实可信的 CA 文件（例如这台 macOS 的 `/etc/ssl/cert.pem`）为 `SSL_CERT_FILE`；保持 TLS 证书验证开启，不使用 `verify=False`。

全链路验收使用 `platform run`，包含准备、短探针、决定、受控激活或保留旧版本、独立续演和部署。`pilot-run` 专门比较四组，不执行准入部署。没有经过训练/校准的评分器时，变化候选得到 `inconclusive` 是保守规则允许的结果，不应为“跑通”而强制接受。

真实调用耗时较长时，可只读查看进度，不显示提示词、回复或密钥：

```bash
python scripts/platform_progress.py --run runs/kimi-admission
```

已完成单次准入运行后，可以复用该次根候选补充冻结旧/新对照，无须重新调用优化器：

```bash
python scripts/frozen_reference.py --source-run runs/kimi-admission \
  --output runs/kimi-frozen-reference --env-file .env
```

同一命令可恢复。脚本绑定来源证据、引擎和自身源码摘要，使用相同任务参数，来源运行保持不变。它是后补的描述性对照：原生分支沿用原运行共享预算，冻结分支有独立预算；不能冒充预先登记、四臂预算一致的 `pilot-run`。原生缺失仍保留，同分变化仍不进入严格改进主分析。

若使用 GPU 服务器，先检查已有 CUDA 环境：

```bash
python scripts/gpu_preflight.py --require-cuda --output runs/gpu-preflight.json
```

该脚本只检查环境，不下载模型、不启动服务、不训练。`git clone` 只下载代码，CUDA、驱动和模型服务仍需独立准备。托管模型需要兼容当前 JSON Chat Completions 网关；其工具遵循和输出格式仍需真实试跑，本仓库没有预先验证特定 4B 模型的可用性。

所有实验目前只操作模拟授权世界。接入会执行任意生成代码的外部系统前，还需要进程/容器隔离后端的单独验收。

# 配置驱动的服务拉起、寻优与测试

本文用于用户明确要求根据配置拉起 vLLM-Ascend 服务、运行有限参数寻优或执行自动验收时。通信预检仍由 `preflight.py` 负责；长时服务生命周期由 `scripts/service_workflow.py` 单独管理，不能把两类报告混成同一种 PASS。

现场问题、并行布局、prefix cache、池化、fused MC2、multistream、P OOM、D 启动与 E2E 判定先读 [PD 运维规则](vllm-pd-operations.md)。只需要先生成并行候选时，从其中的[指标驱动并行推导](vllm-pd-operations.md#按目标指标先推导并行策略)开始，不必先进入远端生命周期。

本页的 `scripts/`、`examples/` 和 `reports/` 相对路径默认以 `skills/ascend-multinode-comm/` 为当前目录；从仓库根目录运行时先进入该目录，或给脚本和样例路径加完整前缀。

## 能力边界

工作流支持：

- 用独立纯离线分析器枚举显式合法的 P/D DP×TP×PP，并输出理论首选、备选、假设与最小验证计划；
- 严格校验 JSON 配置、P/D 的 DP×TP×PP 与实例/设备映射；
- 按依赖 DAG 分批启动已有容器内的前台命令；同一批 supervisor 先发布所有权、全部收到激活令牌后才启动服务，再并发等待健康，适配跨节点 rendezvous；
- 为每个服务维护独立 supervisor、日志和 PID 证据；
- 根据返回码、完整 case 计数、失败请求数、指标和测试后健康判定测试；
- 串行运行至多 32 个显式候选，先验证 baseline，默认只输出本批合格候选中的 best/recommendation 并停止全部 trial；
- 可绑定新鲜的通信预检报告，并在控制端预检后由远端 worker/supervisor 在 `Popen` 前再次复核入口文件 SHA256。

工作流不负责：

- 把理论排序冒充真实性能预测，或把未经目标机器验证的候选直接写进可执行配置；
- 创建、停止或删除容器，拉取/构建镜像；
- 停止本工具没有所有权证据的进程，或按端口、名称、`pkill`、`killall` 清服务；
- 修改防火墙、路由、sshd、设备状态或重置 NPU；
- 自动改写用户启动脚本，或把复杂状态机塞进最终 Bash 脚本；
- 将服务健康、请求成功或本地单元测试冒充精度、NPU 通信或硬件验收。

入口脚本必须以前台进程运行，这是自动生命周期管理的硬约束。脚本可以用最终 `exec` 替换自身，supervisor 会按 child PID、`/proc` starttime 和独立进程组继续识别它；但自行 `nohup ... &`、double-fork、daemonize 或逃离该进程组均不受支持。此类脚本应先复制并保留原有风格，把最终服务命令改为前台 `exec`，或只生成计划、改用能以 cgroup/systemd 等方式提供等价所有权的现场 lifecycle adapter。不要为兼容后台脚本退回模糊杀进程。

## 配置与本地文件

从 [`examples/service-workflow.json`](../examples/service-workflow.json) 复制一份 `*.local.json`。仓库已忽略本地配置；不要提交真实 IP、用户名、私有路径、密钥路径清单或现场日志。公开示例的 `example=true` 会阻止任何执行，必须填写现场值、替换所有 SHA256 并移除该字段后才可运行。

配置顶层字段：

| 字段 | 含义 |
|---|---|
| `deployment` | 本次部署的稳定名称；`stop/tune` 还要求命令行再次确认同名范围 |
| `nodes` | SSH 目标、已有容器、工作目录、目标 Python、平台声明及可用设备 |
| `profile` | 软件/镜像指纹、模型、P/D 并行实例、engine 参数、特性和显式兼容规则 |
| `services` | 每个可独立管理的前台服务、依赖、argv、日志、PID 文件、健康检查和入口哈希 |
| `tests` | 测试 argv、结果文件、超时、断言、计数和可提取指标 |
| `tuning` | 已通过的 baseline、pinned 值、有限候选、目标指标及致命停止模式 |
| `preflight_gate` | 可选的控制端 preflight 报告路径、scope、时效和是否强制门禁 |

配置是数据，不是授权。未知字段、明文密码/令牌、私钥正文、带账号密码的 URI、curl query/fragment、敏感 header、shell `-c` 字符串、`sshpass`、`pkill` 和 `killall` 均被拒绝。密码登录只通过交互 SSH 使用；批量工作流应使用已经验证的 SSH config/agent 或任务专用密钥路径。用户配置不能覆盖 loader、解释器和 shell 启动注入变量。

### 节点与服务

`nodes[].devices` 是该节点允许本配置使用的逻辑设备列表；纯 Proxy/Store 节点可以为空。`profile.roles` 中每个 P/D instance 明确 `dp_rank` 和参与节点/设备：

- instance 数必须等于 DP；
- 每个 instance 的设备数必须等于 TP×PP；
- `dp_rank` 必须完整覆盖 `0..DP-1`；
- P/D 同时运行的设备不能重复；EP 不额外乘设备数。

这能表达“一个逻辑 P 由多台机器组成”以及“多个 D DP 实例分布在若干机器上”，不能用启动脚本数量推断实例数量。

每个 `services[]` 是一个实际启动单元。跨节点同一个 TP/PP 组通常需要每台参与机器各有一个 service，并放在同一依赖 wave；不能用一个本地 supervisor 宣称拥有其他宿主上由任意后台 SSH 创建的 worker。

服务和测试命令必须是 argv 数组，例如：

```json
{"argv":["bash","/opt/deploy/start_prefill_a.sh"]}
```

不得写成 `{"argv":["bash","-lc","..."]}`，也不能用 `env/nohup/setsid/sudo/timeout` 绕过。服务和测试只接受受限 shell/Python 加绝对脚本入口；Python executable basename 只允许 `python`、`python3` 或 `python3.X`。健康探针除相同的绑定脚本外，只允许把 `-q/--disable` 放在首位、启用 `-f/--fail`、不含 query/fragment 的只读 curl。解析出的真实入口必须分别列入对应 `artifacts` 并填写现场 `sha256sum`。控制端先核对服务/测试入口，远端 worker/supervisor 在真正 `Popen` 前拒绝符号链接、FIFO、非普通文件和哈希漂移；健康探针每次执行也在远端复核自己的 artifacts。脚本 source 的关键文件也应分别列入 artifacts。

这套校验用于发现计划后文件漂移，不是抵御同一目标账号恶意并发改写的签名或沙箱：最后一次哈希与按路径执行之间仍有极短竞态，脚本运行期间按路径 `source` 的文件也可能变化。部署目录必须由受控发布流程保持只读/不可变；若同 UID 并发写入属于威胁模型，应先使用内容寻址的只读镜像或现场 adapter 快照执行，不能只依赖本工具的 SHA 字段。

每项健康检查必须显式列出 `down_exit_codes` 及探针入口 `artifacts`；业务退出码只能取 1～124，125 保留给控制协议失败。停止会先按依赖 wave 并发发送所有 ownership-scoped stopper，再等待 DOWN；只有 stopper 返回“状态不存在”时才补做最多 5 秒的有界探针作为已停止证据。停止后的“DOWN”只接受探针完整执行且返回其中一个码；artifact 漂移、SSH 超时、结果协议不完整或传输中断是 `UNCONFIRMED`，不能冒充服务已停。每个服务还需配置 `error_patterns`（本轮测试失败）和更窄的 `fatal_patterns`（寻优立即停止）；例如 OOM 可按候选失败处理，而 device page fault/EngineDead/KV ReadError 应作为致命条件。日志无法在字节上限内完整扫描同样按致命未知状态停止。

### 并行、版本与特性

`profile.platform=A2` 只表示部署声明和拓扑校验支持 A2，不会把现有通信 `preflight.py` 的平台状态改成 PASS。当前 A2 可绑定 `scope=primitives` 的真实通信报告；不要声称完成了尚未实现的平台专属 gate。

`profile.compatibility` 是对已核实版本组合的显式约束：

- `fused_mc2_multistream_conflict=true` 时，同一角色不能同时开启两项；
- `kv_pool_min_hdk` 只在该角色启用 `kv_pool=true` 时检查；prefix cache 本身不等于 KV 池化；
- `max_num_batched_tokens` 可以小于 `max_model_len`。例如保留 128K 最大长度并把单批 token 从 32K 降到 16K，是合法且常见的 P 侧峰值内存控制。

这些字段不证明运行时已经消费开关。验收仍需从真实进程参数、启动日志和请求/算子证据确认。

## 计划与执行

先设置脚本位置：

```bash
WORKFLOW=skills/ascend-multinode-comm/scripts/service_workflow.py
CONFIG=/secure/local/deployment.local.json
```

1. 只做配置校验：

```bash
python "$WORKFLOW" validate --config "$CONFIG"
```

2. 生成不可覆盖的计划文件：

```bash
python "$WORKFLOW" plan --config "$CONFIG" --out reports/deployment-plan.json
```

人工核对节点、容器、设备、依赖 wave、测试和候选；记录输出的 `plan_sha256`。计划哈希覆盖规范化配置、规范化 SSH identity 路径、声明的 artifacts 哈希、绑定 preflight 报告的内容哈希，以及 `service_workflow.py`/`preflight.py` 实现指纹。配置或实现任一变化都需重新 plan。绑定报告缺失/损坏会成为 `UNAVAILABLE` 而不是让计划生成崩溃；`launch/test/tune` 仍受必需门禁阻止，精确 `status/stop` 可用新计划做恢复。

3. 对不含 `tuning` 的最终配置启动服务：

```bash
python "$WORKFLOW" launch --config "$CONFIG" \
  --execute --approve PLAN_SHA256 --out reports/launch.json
```

启动前先探测健康：只有健康且存在与当前完整 service/profile 规格哈希一致的运行中 supervisor，才记为 `ALREADY_HEALTHY`；外部进程、旧规格或无法确认所有权的健康端口会失败关闭，不冒充本配置已启动。其他服务先校验 artifact，然后启动只发布所有权并等待的 supervisor；同一 wave 全部确认后提交激活令牌，才允许实际服务 child 启动，避免 SSH ACK 丢失后迟到进程逃逸。某一 wave 失败或控制端中断时，只按本轮 128-bit 随机 run ID 回收已确认属于本轮的进程，保留日志和报告。

4. 查看服务健康（健康探针本身是远端命令，因此也必须显式批准当前计划）：

```bash
python "$WORKFLOW" status --config "$CONFIG" \
  --execute --approve PLAN_SHA256 --out reports/status.json
```

`status` 只有在探针通过且 PID 状态中的 service/profile 规格哈希与当前已解析配置一致时才返回 PASS；含 tuning 模板的配置必须先固化到具体 trial，不能猜当前运行候选。

5. 运行测试：

```bash
python "$WORKFLOW" test --config "$CONFIG" \
  --execute --approve PLAN_SHA256 --out reports/test.json
```

固定结果日志由远端 worker 取得 `.lock` 排他锁后先重命名为 `.backup.<timestamp>`，然后写入本轮文件；日志和锁只接受普通文件，拒绝符号链接/目录，避免并发移动活跃日志或误截断其他目标。测试前后都要求健康探针通过且当前规格所有权为 `RUNNING`，并要求每个服务的 run ID 前后不漂移；寻优测试还必须属于刚启动的精确 run ID，不能跨两个实例拼成 PASS。成功还需满足进程返回码、成功证据与失败拒绝断言/计数、完整输出未截断、进程组已完整回收、服务日志扫描及所有有限指标可提取；测试脚本没有 `set -e` 时也不能靠最终退出码蒙混过关。用户正则在可终止的本地子进程中共享固定时限，超时/结果不完整按失败处理，不能卡住整个控制端。使用 `test --name` 只跑子集时报告为 `PARTIAL`，不会冒充全量 PASS。

6. 停止本工具拥有的服务：

```bash
python "$WORKFLOW" stop --config "$CONFIG" \
  --execute --approve PLAN_SHA256 --confirm-deployment DEPLOYMENT_NAME \
  --out reports/stop.json
```

PID 状态用临时文件加 `os.replace` 原子发布，独立生命周期锁防止重复 supervisor；内容包含 deployment、service、service/profile 规格 SHA256、run ID、宿主 boot ID、supervisor/child PID、`/proc` starttime、进程组和启动时 cmdline SHA256。supervisor 自身必须继续匹配 PID/starttime/cmdline；前台 child 允许正常 `exec` 改变 cmdline，但必须保持 PID、starttime 且仍是记录的进程组 leader。回滚还必须匹配本轮 run ID，不能停止另一控制端刚启动的同名服务。停止成功要求记录的进程组中已无非僵尸成员；leader 退出但组内仍有无法重新核验归属的成员时保留状态并失败关闭。supervisor 异常退出时保留可核验的 child 证据并尝试安全回收；宽限期后仍存活时报告失败，不自动把服务侧进程强杀。健康但缺少所有权文件的进程会被保留并报错。

控制端在任何远端动作前先用排他锁和占位文件预留报告路径，完成后原子替换；报告及其 lock/temp/error/probe 派生路径不得与配置、preflight 报告或 SSH identity 文件相同（含符号链接/已有硬链接）。路径已存在时默认拒绝，确实要替换时显式加 `--force`。若覆盖模式下动作失败，旧报告保留并另写带 token 的 `.error.*.json`。这不改变远端测试日志的独立锁与备份策略。远端 JSON payload 采用内联 argv，执行确认会按 Windows CreateProcess 的 32K 边界保留余量并预先拒绝过长配置；本地正则 operations 改走临时文件，避免合法断言挤爆命令行。

## 优化层级：analyze → verify → quick → exhaustive

优化不是直接从长循环开始。先把官方配置或现有成功脚本当作兼容 baseline，再按本次模型、版本、硬件、负载和主指标选择需要到达的层级：

1. **analyze**：纯离线推导。复制 [`parallelism-advisor.json`](../examples/parallelism-advisor.json)，只填写脱敏环境指纹、总卡数、P/D 预算、已核实的 TP/PP allow-list、最小单副本卡数和固定负载。运行：

   ```bash
   python scripts/parallelism_advisor.py \
     --config examples/parallelism-advisor.local.json \
     --out reports/parallelism-advice.json
   ```

   置信度固定为 `THEORY_ONLY`。环境指纹完整时状态为 `RECOMMENDED_FOR_VALIDATION`；公开样例的 `fill-current-*` 尚未替换时降级为 `DRAFT_RECOMMENDATION` 并列出缺口。输入中的 P/D 预算之和不能超过集群总卡；例如 P、D 各 32 卡表示至少 64 张可同时使用的卡。allow-list 和最小单副本卡数必须来自模型容量、当前版本支持或同口径历史证据，不能为了得到预想答案倒填。分析器没有 SSH、容器、网络或子进程执行能力，也不生成 `services[]`。它固定用户声明的 P/D 卡预算；token/QPS 只形成负载代理，在没有当前模型实测服务时间时不伪造跨角色容量评分或自动重分配卡数。

2. **verify**：把首选候选复制到一份新的、无模板的 service-workflow 本地配置和用户同风格脚本；重新 `validate/plan`，只做一次最小启动、正确性、代表负载、日志和停止闭环。理论候选达到用户门槛即可结束，不强制进入 tune。
3. **quick**：锁定模型、镜像、软件版本、P/D 节点预算和测试负载，通常只比较 baseline 附近 8～16 个显式候选（参数值或合法特性组合）；探索阶段使用代表 case，前两名再做完整 E2E 和重复测试。推荐数量可由用户预算收紧，但不能省略硬门槛。
4. **exhaustive**：按“拓扑粗筛 → 特性组合 → 数值细搜 → 完整 E2E → 重复/长稳”分阶段推进。每阶段仍受现有 32 个显式 trial 硬上限约束，阶段间根据前一报告生成新配置、重新 plan、人工审阅并授权；不能用一个无限循环规避边界。最终只称为声明模型、版本、硬件、负载和搜索空间内的高置信候选。

一个 trial 是“一组配置完成停止旧实例、拉起、预热、固定测试、后置健康、停止和增量日志扫描”，不是单条请求。两种模式都采用“一个主指标 + 硬门槛”：失败请求为零、精度与稳定性通过、无 OOM/device fault/EngineDead/KV 致命错误、用户 TTFT/TPOT SLO 和显存余量满足。输入/输出长度、并发、Prefix 口径属于固定评测条件，除非用户明确把业务负载本身设为研究变量，不能通过降低负载美化指标。

历史脚本、规范化结果和失败记录用于 warm start 与裁剪；只给脚本而没有环境、负载和指标，不能作为“更优”证据。跨版本、镜像或模型比较必须另建实验和 plan，不能混入同一候选集合。

当前 `service_workflow.py tune` 是每阶段的安全有限候选执行器，不是自适应搜索器；`quick/exhaustive` 是计划和证据层级，不是尚未实现的 CLI preset。自动生成下一候选、统计置信区间和 checkpoint/resume 仍需后续实现，不能在文档中冒充已有能力。

注意数量口径不同：`parallelism_advisor.py` 的 quick/exhaustive 最多返回 4/16 个**理论拓扑推荐**；这里 quick 的 8～16 和 exhaustive 每阶段最多 32 指需要实际拉起、测试和停止的 **lifecycle trial**。理论推荐数不是上机轮数，advisor 的 mode 也不会自动启动 `tune`。

## 有界寻优

含 `tuning` 的配置使用低碰撞模板 `{{parameter_name}}` 把候选值放到 service/test 的 argv 或 env 值；日志路径可带 `{{trial}}` 用于隔离轮次，但参数只出现在日志路径不算实际改变服务行为。其他字段不会被渲染，正则中的普通 JSON 花括号保持字面含义。不要用正则直接修改任意 Bash。`baseline` 必须明确；`pinned` 保护用户不允许改变的值；候选笛卡尔积在展开前受限，含 baseline 不得超过 `max_trials` 和硬上限 32。

```bash
python "$WORKFLOW" tune --config "$CONFIG" \
  --execute --approve PLAN_SHA256 --confirm-deployment DEPLOYMENT_NAME \
  --out reports/tuning.json
```

每个 trial 严格串行：确认旧的 tool-owned 服务停止 → 拉起并确认本轮拥有全部服务 → 等待健康 → 仅在 run ID 精确匹配时执行相同测试 → 后置健康 → 仅按本轮 run ID 停止 → 再扫描停止阶段写入的服务日志。正常失败、异常或 Ctrl-C 都进入本轮精确回收；控制端中断还会回收本地 SSH transport。无法确认清理时不开始下一候选。baseline 未通过立即终止；命中 test/tuning/service 的 `fatal_patterns`（如 device page fault、EngineDead、KV/ReadError），或日志/测试输出、正则评估、测试进程组清理不完整，立即终止且不自动重试。普通候选错误（如已分类为非致命的 OOM）可记录后继续，前提是本轮服务已完整停止且停止后日志也无致命证据。

只有所有测试通过且目标 metric 存在的 trial 才参与最优选择。`tune` 默认不把最佳候选重新上线；先审阅报告，把 best 写成一份无模板的最终本地配置，重新 plan、approve、launch 和完整测试。

如果用户坚持某值，例如 `gpu_memory_utilization=0.92`，将其写入 `pinned` 并在实际服务 env/argv 固定；不要在 OOM 后暗中降低。可只搜索 `max_num_batched_tokens` 的有界候选。

## E2E 判定示例

对于九组正式 case 加九组 prefix 探针的脚本，至少配置：

- “全量数据集测试完成”精确计数 9；
- `Failed Requests` 汇总行精确计数 18；
- 禁止任何非零 failed requests；
- 禁止 Traceback、EngineDead、device page fault、ReadError、KV load failure；
- 提取相同口径的 throughput/TTFT/TPOT 等目标指标；
- 测试结束后重新检查 P、D、Proxy 全部健康。

`/metrics` 404 只有在该端点明确是可选指标探针时可作为 warning/UNVERIFIED；不能全局忽略 HTTP 404，也不能因此把真实请求失败记为成功。

## 执行前检查

- 配置中的容器都已存在且正在运行；工作目录、脚本与日志父目录位于正确 namespace。
- 服务/健康/测试使用业务 workdir；所有权、停止、激活、artifact 和日志控制面固定从稳定 `/` 运行，容器控制命令不依赖可能被删除或卸载的业务目录。
- artifacts 覆盖入口和关键 source，SHA256 来自本次现场；发布目录在执行窗口内只读/不可变。
- 启动脚本是前台命令；每个服务的 PID/log 路径唯一。
- 卡与端口已确认空闲；工具不会自动清理其他任务。
- 若绑定 preflight，报告仍在有效期内且 scope 与本次通信域一致。
- 计划 SHA 与当前配置一致；任何脚本、报告、节点、参数或候选变化后重新 plan。
- 测试断言包含 case 数、失败数、致命错误与后置健康，不只看退出码。

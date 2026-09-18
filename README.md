# Ascend 多机通信、vLLM-Ascend 部署与性能 Skill

[![标准库测试](https://github.com/Tflowers-0129/ascend-multinode-comm-skills/actions/workflows/tests.yml/badge.svg)](https://github.com/Tflowers-0129/ascend-multinode-comm-skills/actions/workflows/tests.yml)

这是一个面向 Ascend A2/A3/A5 与 vLLM-Ascend 的 **Codex Skill + 可独立运行工具集**。它把模型并行规划、部署脚本分析、通信预检、现场故障定位、P/D/Proxy 服务生命周期、E2E 验收和有限参数寻优组织成一条有证据、可复核的工作流。

它解决的不是“生成一条看起来能跑的命令”，而是下面这些实际问题：

- 给定模型、量化/cache、机器和负载，P、D 分别该选怎样的 DP×TP×PP×EP？
- 官方示例为什么能启动，在当前版本、容器和拓扑上却卡住或 OOM？
- SSH、TCPStore、Gloo、HCCL、MC2、P→D KV 分别验证到了哪一层？
- 如何读取多节点/rank 日志，找到最早首因，而不是把最后一个 timeout 或 SIGKILL 当根因？
- 如何在不误杀他人服务的前提下，受控地拉起、检查、测试和停止 P/D/Proxy？
- 如何固定测试口径，对少量候选做 quick 验证，或分阶段做更深的 exhaustive 寻优？

核心原则是：**理论候选、通信证据、服务健康、正确性和性能是五层不同证据，前一层不能代替后一层。**

## 功能一览

| 能力 | 典型问题 | 实现入口 | 主要产物 | 是否连接/改变远端 |
|---|---|---|---|---|
| 理论并行规划 | “64 卡 PD 服务优先 output throughput，P/D 怎么分 DP、TP、PP？” | Skill 推导规则 + `parallelism_advisor.py` | 首选/备选、原因、风险、淘汰原因、最小验证计划 | 不连接；纯离线 |
| 部署脚本静态审计 | “这些 shell/source 文件的 rank、IP、端口和 KV 配置是否矛盾？” | `audit_deployment.py` + agent 语义分析 | JSON/Markdown 线索、确定错误、条件风险、人工复核点 | 不连接；不执行脚本 |
| 宿主 Fabric 取证 | “设备映射、vNIC、Pod/SDID、HCCS 边是否正常？” | `fabric_probe.py` | 设备映射、网络原始证据、显式卡对 HCCS 小包结果 | `inspect` 会 SSH 执行宿主采集命令；`ping --execute` 会主动发包 |
| 分层通信预检 | “服务还没启动，容器里的 TCPStore/Gloo/HCCL 能否建链？” | `preflight.py`、`hccl_bench.py`、MC2 adapter | 分阶段 PASS/FAIL/UNVERIFIED、逐卡结果、gate 结论 | `inspect` 会 SSH 采集并可能 source 已审阅的 env script；`check/pairs` 本身就是主动测试 |
| 现场故障定位 | “D 节点卡住、P 节点 OOM、设备 page fault，首因是什么？” | Skill 故障手册 + 现场只读命令 + 最小探针 | 最早失败阶段、节点/rank/时间线、证据化根因或待验证假设 | 默认先只读；新增监听、占卡测试或重启需相应授权 |
| 服务生命周期 | “按现有脚本安全拉起 1P1D，检查状态、测试、再停止” | `service_workflow.py` | plan SHA、run ID、launch/status/test/stop 报告 | 执行动作需 `--execute --approve`；只管理有精确所有权的进程 |
| E2E 与有限寻优 | “固定 workload 比较 16K/24K/32K，保留 0.92，不自动上线 best” | `service_workflow.py test/tune` | 每个 trial 的正确性、性能、日志和健康证据，best 候选 | 会串行启停并运行用户测试；最多 32 个显式候选 |
| 手工 collective | “用官方 HCCL Test 或 torch.distributed 做健康卡/故障卡对照” | `manual-tests/` | 官方 collective 或普通 HCCL 数值结果 | 主动占用指定 NPU/端口；不能代替 MC2 |

如果服务尚未启动，通常从通信预检进入；如果服务已经失败，从现场排障进入；只有 baseline 已通过正确性和稳定性后，才进入性能寻优。

## 它是怎么实现的

### 1. Skill 决策层

[`SKILL.md`](skills/ascend-multinode-comm/SKILL.md) 负责识别用户当前属于“规划、预检、排障、生命周期还是寻优”，并只加载对应 reference。references 保存平台差异、通信阶段、PD 并行经验、OOM 判定、服务安全边界和现场故障手册。

这部分擅长处理不能仅靠一个 CLI 解决的问题，例如：

- 从多份启动脚本、source 链、运行中进程和日志还原真实配置；
- 根据 TTFT、TPOT、output/request throughput、goodput 等目标选择 P/D 并行方向；
- 区分权重/KV 容量、MTP dummy/request workspace OOM、宿主 OOM 和 NPU device fault；
- 判断某个测试只覆盖普通 HCCL、某一类 MC2，还是实际 P→D KV 路径。

### 2. 确定性工具层

`scripts/` 中的工具把容易出错、需要重复执行的部分固定下来：严格解析 JSON、枚举候选、通过 SSH stdin 运行同版本探针、生成结构化报告、校验 artifact 哈希、管理 run ID，以及对测试结果做正负证据和计数检查。

除 [`manual-tests/torch_collectives.py`](skills/ascend-multinode-comm/manual-tests/torch_collectives.py) 需要目标环境已有 PyTorch/torch-npu 外，控制端工具只使用 Python 标准库。

### 3. 证据与安全层

完整路径如下：

```text
用户目标、版本、模型、拓扑、脚本和历史日志
  → analyze：离线理论候选
  → preflight：宿主/容器/通信域证据
  → verify：最小启动、首请求、正确性和代表负载
  → quick：baseline 附近少量高收益候选
  → exhaustive：拓扑→特性→数值→全量 E2E→重复/长稳
  → 固化最终脚本和配置，重新 plan、启动、全量验收
```

每一步都保留适用范围。`PASS` 只表示报告中声明的 scope、节点、版本和时间窗通过，不自动升级为“整个模型服务已验证”。

## 安装、依赖与触发

### 作为 Skill 使用

仓库中的 Skill 位于 `skills/ascend-multinode-comm`。必须安装或加载整个目录，不能只复制 `SKILL.md`，否则脚本、references、examples 和手工测试都会缺失。

```bash
git clone https://github.com/Tflowers-0129/ascend-multinode-comm-skills.git
cd ascend-multinode-comm-skills
```

将 `ascend-multinode-comm-skills/skills/ascend-multinode-comm` 整体放入所用 agent 的 skills 目录。Codex 默认通常是：

- Linux/macOS：`~/.codex/skills/ascend-multinode-comm`
- Windows：`%USERPROFILE%\.codex\skills\ascend-multinode-comm`

也可以直接让支持安装 GitHub Skill 的 agent 安装该仓库中的上述子目录。没有 Skill loader 时，先读取 [`SKILL.md`](skills/ascend-multinode-comm/SKILL.md)，再按其中路由打开当前任务需要的 reference。

### 运行依赖

- 控制端：Python 3.10+。
- 远端采集/执行：OpenSSH。`preflight.py` 依赖 SSH alias/config/agent 提供非交互认证，不读取配置内的 `identity_file`；`fabric_probe.py` 与 `service_workflow.py` 支持显式 identity file。不把密码写进 JSON。
- 生命周期目标：Linux、Python 3、`/proc`、`fcntl`，以及已经存在的容器、模型和前台启动脚本。
- HCCL/MC2/模型测试：目标环境自行提供匹配版本的 CANN、torch/torch-npu、MPI、hccl_test 或业务 adapter。

仓库根目录的多行命令示例使用 Bash 语法；Windows PowerShell 需要把 `cp` 换成 `Copy-Item`，并调整续行符。Windows 没有 `python` 命令时可换成 `py -3` 或解释器绝对路径。

### 用自然语言触发

可以显式写出 Skill 名称并限定授权边界：

```text
$ascend-multinode-comm
任务：为 A2 64 卡 PD 部署按 output throughput 规划 P/D 的 DP×TP×PP。
模型：某 MoE W8A8C8；KV cache 为 BF16；最大长度 128K。
负载：平均未命中输入 64K、输出 1K、并发 48、Prefix 命中率 90%。
约束：本轮只做离线分析，不连接服务器；列出假设、风险和最小验证计划。
```

也可以直接提出场景化请求：

- `只读检查 node-a/node-b 的目标容器通信，先做 inspect，不启动监听器或占用 NPU。`
- `D 节点启动卡住；容器、工作目录和入口脚本如下。先关联各 rank 日志并找最早失败阶段，不重启。`
- `根据这份本地配置先 validate 和 plan，不执行；我审核 plan SHA 后再启动。`
- `以已通过的 16K baseline 和固定 workload 做 quick 寻优，最多 8 个显式候选，不自动上线 best。`

建议至少提供：模型/检查点、量化与 cache、硬件和卡数、软件版本、P/D 预算、输入输出长度与并发、主指标、已有脚本/容器/日志，以及本轮允许的远端动作。

## 功能 1：理论并行规划

### 能做什么

根据用户已经核实的模型容量、TP/PP 合法范围、P/D 卡预算、单机快速互联域和固定 workload，枚举 P/D 的 DP×TP×PP 候选，并按 `ttft`、`tpot`、`output_throughput`、`request_throughput`、`goodput` 或 `balanced` 给出可解释排序。

### 原理概览

普通 replicated-DP 场景下：

```text
role_world_size = DP × TP × PP
```

- TP 分摊单层权重和计算，但扩大逐层 collective 通信域。
- PP 分段模型层，可降低单 rank 常驻权重，但会引入 stage 不均衡和 pipeline bubble。
- DP 复制完整 TP×PP 副本，通常提高并发，但不帮助单副本装下；EP 等跨 DP 分片需按真实 placement 另算。
- P 更受长输入、prefill 计算和 TP 通信影响；D 更受持续解码、KV、并发和副本数量影响。因此“P 适当 PP/大 TP、D 尽量多 DP/较小 TP”只能作为条件化方向，必须先满足容量和版本约束。

显存也不是简单的 `checkpoint_size ÷ TP ÷ PP`：

```text
单 rank 保守预算 ≈
  常驻权重 + 常驻 KV + runtime/通信常驻
  + max(加载临时量, 编译/图, warmup/MTP dummy, 请求工作区)
  + 安全余量
```

同指纹真实日志可以校准某个候选的成功/失败边界；它不能证明所有相同 TP×PP 乘积都能运行。

### CLI 示例

```bash
cp skills/ascend-multinode-comm/examples/parallelism-advisor.json \
  skills/ascend-multinode-comm/examples/parallelism-advisor.local.json

# 填写当前版本、卡预算、负载、合法 TP/PP 和 minimum_replica_devices 后运行。
python skills/ascend-multinode-comm/scripts/parallelism_advisor.py \
  --config skills/ascend-multinode-comm/examples/parallelism-advisor.local.json \
  --out reports/parallelism-advice.json
```

重点查看输出中的：

- `status`：公开样例仍有占位符时是 `DRAFT_RECOMMENDATION`；完整输入可成为 `RECOMMENDED_FOR_VALIDATION`。
- `confidence`：始终是 `THEORY_ONLY`，即使 status 是推荐验证，也不是实测性能结论。
- `recommended` / `ranked_candidates`：候选及排序理由、风险。
- `rejected_summary`：被容量、预算、本地性或比例约束淘汰的组合。
- `verification_plan` / `must_verify`：下一步必须上机验证的内容。

当前工具不会读取模型权重或历史日志，不会预测真实毫秒/吞吐，不会自动重分配 P/D 卡数，也不做主机/设备联合装箱。`minimum_replica_devices` 是角色级粗粒度下界；当前 schema 也没有 `(TP, PP)` pair 白名单，特殊组合需由 agent/人工预筛。

详见 [PD 并行推导与显存判定](skills/ascend-multinode-comm/references/vllm-pd-operations.md)。

## 功能 2：部署脚本静态审计

### 能做什么

在不执行用户脚本的前提下，追踪受支持 Shell 子集中的 source、变量、export、命令前赋值，以及部分 vLLM/torchrun 参数，辅助发现非法 IP/端口、同通信域 rank 冲突、环境未生效或 KV 布局矛盾。

### 示例

仓库中的 `audit-demo/` 是故意构造的故障 fixture，不是部署模板：

```bash
python skills/ascend-multinode-comm/scripts/audit_deployment.py \
  --root skills/ascend-multinode-comm/examples/audit-demo \
  --manifest skills/ascend-multinode-comm/examples/audit-demo/manifest.json \
  --out reports/audit-demo-run.json
```

该示例预期发现错误并返回 1，同时生成 JSON 和同名 Markdown。真实目录没有确定错误时仍返回 2/`UNVERIFIED`，因为静态分析不能证明现场建链安全。输出路径必须是尚不存在的新 `.json/.md`。

复杂函数/条件/循环、命令替换、heredoc、嵌套 SSH/docker shell、Python、YAML、Compose 和 K8s 会标为 `NEEDS_REVIEW`，由 agent 继续语义分析。远端任务可以直接提供服务器内路径，不要求先把脚本上传到仓库。

## 功能 3：通信预检与逐卡隔离

### 分层预检

`preflight.py` 会通过 SSH，在宿主或既有容器的目标 namespace 中经 stdin 运行同版本短时探针：

```text
环境/网卡/设备
  → 正反向 DNS 和 TCP
  → TCPStore
  → Gloo
  → 普通 HCCL collective
  → 显式 MC2 case / adapter
  → 用户提供的模型 E2E、KV transfer 或 KV pool adapter
```

复制并填写至少两个节点的配置；必须替换 SSH、容器和网络占位符，并先审阅 `env_scripts`：

```bash
cp skills/ascend-multinode-comm/examples/cluster.json \
  skills/ascend-multinode-comm/examples/cluster.local.json

# 会连接 SSH执行采集；env_scripts 为空或已确认仅做环境初始化时才可视为只读。
# 通常因主动层尚未测试而返回 2。
python skills/ascend-multinode-comm/scripts/preflight.py inspect \
  --config skills/ascend-multinode-comm/examples/cluster.local.json \
  --out reports/inventory.json

# 获得空闲端口、NPU 和时间窗授权后再运行；调用本身就是主动测试。
python skills/ascend-multinode-comm/scripts/preflight.py check \
  --config skills/ascend-multinode-comm/examples/cluster.local.json \
  --out reports/preflight.json

# gate 只读取现有报告，并校验 scope 与新鲜度。
python skills/ascend-multinode-comm/scripts/preflight.py gate \
  --report reports/preflight.json --scope primitives --max-age-s 3600
```

`pairs` 仅用于两个节点，会对双方显式 devices 做笛卡尔逐卡 HCCL；它也没有第二个 `--execute` 开关：

```bash
python skills/ascend-multinode-comm/scripts/preflight.py pairs \
  --config skills/ascend-multinode-comm/examples/cluster.local.json \
  --out reports/card-pairs.json
```

自动平台门禁当前只对 A3/A5 建立 PASS 路径。A2 仍可做适用的 primitives/pairs 和服务侧人工验证，但平台身份保持 `UNVERIFIED`，不能据此宣称 service/内置 MC2 平台门禁已通过。

内置 MC2 只覆盖三类 eager、非量化、小 shape 的 torch-npu 融合 API：Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter。图模式、量化、MoE Dispatch/Combine 和其他融合路径必须使用与当前版本匹配的 adapter；[`mc2-cases.json`](skills/ascend-multinode-comm/examples/mc2-cases.json) 是需要合并进 cluster 配置的片段，不是可单独运行的完整配置。

### 宿主 Fabric 取证

`fabric_probe.py` 不进入容器、不 source 环境，也不运行 HCCL/MC2。它通过 SSH 采集 `npu-smi`、hccn、IP/route、RDMA/URMA 等宿主证据；`ping` 默认只规划显式设备边，只有 `--execute` 才发送有界 HCCS 小包。

```bash
cp skills/ascend-multinode-comm/examples/fabric-nodes.json \
  skills/ascend-multinode-comm/examples/fabric-nodes.local.json

python skills/ascend-multinode-comm/scripts/fabric_probe.py inspect \
  --config skills/ascend-multinode-comm/examples/fabric-nodes.local.json \
  --out reports/fabric-inventory.json

# 仍会 SSH 重新采集，但不发包。
python skills/ascend-multinode-comm/scripts/fabric_probe.py ping \
  --config skills/ascend-multinode-comm/examples/fabric-nodes.local.json \
  --source node-a --target node-b --same-index --bidirectional \
  --out reports/hccs-plan.json

# 本次明确允许发小包后，在相同命令中加入 --execute，并使用新的输出路径。
```

### 官方 HCCL Test 计划

默认只在 stdout 打印 MPI/hccl_test 计划，不连接远端，也不写 `--out`：

```bash
python skills/ascend-multinode-comm/scripts/hccl_bench.py \
  --host node-a:8 --host node-b:8 \
  --source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh \
  --directory /usr/local/Ascend/ascend-toolkit/latest/tools/hccl_test \
  --mpi mpich --op all2all --profile smoke
```

只有在参与测试的 Linux 节点/容器中加入 `--execute --out /new/path/hccl-bench-RUN_ID.json` 才会实际运行 MPI ranks。执行模式会覆盖同名 JSON 和同名 `.log`，因此每轮必须使用新路径或先备份旧结果。即使进程返回 0，工具仍保守记录 `status=UNVERIFIED`、`correctness=UNVERIFIED`；需要人工核对各 rank、尺寸、校验列和日志，`--check` 也不会自动把报告升级为正确性 PASS。

更多说明见 [通信阶段矩阵](skills/ascend-multinode-comm/references/communication-stages.md)、[宿主 Fabric](skills/ascend-multinode-comm/references/host-fabric-detection.md)、[HCCL](skills/ascend-multinode-comm/references/hccl-testing.md) 和 [MC2](skills/ascend-multinode-comm/references/mc2-testing.md)。

## 功能 4：现场故障定位

现场排障没有“一键诊断 CLI”。它由 agent 按 Skill 规则组合远端只读状态、脚本语义、各节点/rank 日志和最小验证，重点是找 **最早失败点及对端证据**。

一个有效请求应包含容器、工作目录、入口脚本和大致失败时间窗：

```text
$ascend-multinode-comm
D 节点启动一直卡住。请在现有容器和工作目录中只读检查：
1. 不重跑启动脚本、不重启服务；
2. 对齐全部 D rank 与对应 P/Store 日志；
3. 给出最早成功/失败阶段、节点/rank、日志证据和最小修复建议；
4. 如果下一步需要监听端口、占用 NPU 或修改脚本，先停在建议处。
```

排障按阶段区分环境、store/DNS、Gloo、HCCL 初始化、首个 collective/融合算子、KV connector 和业务请求。典型分类包括：

| 现场现象 | 正确判定方式 |
|---|---|
| `failed to allocate` | 结合失败阶段、申请量、allocated/reserved/free，区分权重、KV、warmup/MTP dummy 或请求工作区 |
| exit 137/SIGKILL | 需要 cgroup `memory.events` 或 kernel OOM 等系统证据；不能直接归为 NPU 显存 |
| device page fault / 非法 GM 地址 / vector core 异常 | 独立 NPU device fault；后续 HCCL watchdog 或 SIGKILL 常只是清理结果 |
| 最后一个 rank timeout | 必须回看其他 rank 的更早异常和对端状态，不能把 timeout 自动当网络首因 |

仓库保存过一个脱敏真实闭环：同指纹 P 候选在 32K batched tokens 的 MTP dummy 阶段申请工作区失败；保持显存利用率、改为 16K 后成功启动，并完成 9 组正式 case、9 组 Prefix 探针，18 条失败汇总均为 0。这个证据只校准该候选边界，不外推成所有 A2、版本或 TP×PP 组合的结论。

详见 [现场故障手册](skills/ascend-multinode-comm/references/failure-playbook.md) 和 [PD 运维、OOM 与验收](skills/ascend-multinode-comm/references/vllm-pd-operations.md)。

## 功能 5：受控服务生命周期

### 原理概览

`service_workflow.py` 不生成启动脚本、不创建容器，也不接管手工进程。它运行用户已经准备好的绝对路径前台入口，并用以下证据管理所有权：

```text
deployment + service + profile/service spec SHA
+ artifact SHA256 + host boot ID
+ supervisor/child PID + /proc starttime + process group
+ 128-bit run ID
```

`plan_sha256` 绑定规范化配置、SSH identity 路径、配置中声明的 artifact 路径/SHA、绑定的 preflight 报告内容哈希，以及工作流实现本身。`plan` 不连接服务器，也不证明远端文件当前匹配；执行阶段才会核验真实远端 artifact。配置、脚本、报告或实现变化后都必须重新 plan。

同一依赖 wave 会先让所有 supervisor 发布所有权，再提交 activation token 并等待健康；失败只回滚本轮 run ID。工具不会按端口/名称、`pkill` 或 `killall` 清理服务，也不会停止缺少精确所有权证据的健康进程。

### 从计划到完整闭环

公开 [`service-workflow.json`](skills/ascend-multinode-comm/examples/service-workflow.json) 可以安全地 `validate/plan`，但其中的 `example: true`、`.invalid` 地址、零 SHA 和占位路径会阻止真实执行。

```bash
python skills/ascend-multinode-comm/scripts/service_workflow.py validate \
  --config skills/ascend-multinode-comm/examples/service-workflow.json

python skills/ascend-multinode-comm/scripts/service_workflow.py plan \
  --config skills/ascend-multinode-comm/examples/service-workflow.json \
  --out reports/example-plan.json
```

真实使用时，复制为仓库外或 `*.local.json`，删除 `example: true`，填入当前节点、容器、工作目录、版本、模型、脚本/健康探针、日志、PID 文件和真实 SHA256。用于普通 `launch/status/test/stop` 的最终配置还需移除 `tuning`，并把模板固化成单一候选。必须对这份最终配置重新 validate/plan，不能复用公开 example 的 SHA：

```bash
python skills/ascend-multinode-comm/scripts/service_workflow.py validate \
  --config /secure/local/deployment.final.json

python skills/ascend-multinode-comm/scripts/service_workflow.py plan \
  --config /secure/local/deployment.final.json \
  --out reports/final-plan.json

# FINAL_PLAN_SHA256 来自人工审阅过的 final-plan.json。
python skills/ascend-multinode-comm/scripts/service_workflow.py launch \
  --config /secure/local/deployment.final.json \
  --execute --approve FINAL_PLAN_SHA256 --out reports/launch.json

python skills/ascend-multinode-comm/scripts/service_workflow.py status \
  --config /secure/local/deployment.final.json \
  --execute --approve FINAL_PLAN_SHA256 --out reports/status.json

python skills/ascend-multinode-comm/scripts/service_workflow.py test \
  --config /secure/local/deployment.final.json \
  --execute --approve FINAL_PLAN_SHA256 --out reports/test.json

python skills/ascend-multinode-comm/scripts/service_workflow.py stop \
  --config /secure/local/deployment.final.json \
  --execute --approve FINAL_PLAN_SHA256 \
  --confirm-deployment DEPLOYMENT_NAME --out reports/stop.json
```

`status` 也会通过 SSH 执行配置中的健康命令并读取所有权状态，因此不是纯本地操作。只有“健康探针通过 + 当前 service/profile 规格所有权匹配”才是 PASS。

服务入口必须保持前台运行，不能自行 daemonize、double-fork 或逃离记录的进程组。artifact 哈希证明“执行的是审核过的文件”，但不会把任意脚本内容变成沙箱；仍需先审阅脚本本身。

完整 schema 与安全边界见 [服务生命周期](skills/ascend-multinode-comm/references/service-lifecycle.md)。

## 功能 6：E2E 验收与有限寻优

### E2E 如何判定

`test` 不内置某个固定压测框架，而是执行用户已有的测试入口，然后同时检查：

- 进程退出码、完整输出和进程组是否正确结束；
- 必须出现/禁止出现的正则、期望计数和失败请求数；
- 可提取的 TTFT、TPOT、throughput 等有限数值指标；
- 测试前后 P、D、Proxy 是否健康，run ID 是否保持一致；
- 服务增量日志中是否有 OOM、device fault、EngineDead、KV/ReadError 等致命模式。

只运行 `test --name` 的测试子集时结果是 `PARTIAL`，不会冒充全量 PASS。请求成功也不等于精度通过；精度必须在用户测试脚本和 assertions 中有独立、可执行的判据。

### quick 与 exhaustive 的真实含义

- `analyze`：离线理论候选。
- `verify`：一次最小启动、正确性、代表负载、日志和停止闭环。
- `quick`：通常比较 baseline 附近 8～16 个显式候选。
- `exhaustive`：分阶段做拓扑粗筛、特性组合、数值细搜、全量 E2E、重复/长稳。

`quick/exhaustive` 是工作流层级，不是 `service_workflow.py` 的两个自动搜索 preset。实际 `tune` 每个已批准 plan 最多串行运行 32 个显式候选：

```text
确认旧 tool-owned 服务停止
  → 启动当前 trial
  → 健康/预热
  → 固定测试
  → 后置健康和日志扫描
  → 精确停止当前 run ID
  → 合格 trial 按一个主指标选 best
```

配置中必须声明 baseline、pinned 值、候选和 objective。例如：

```json
{
  "tuning": {
    "max_trials": 3,
    "baseline": {"max_num_batched_tokens": 16384},
    "pinned": {"gpu_memory_utilization": 0.92},
    "candidates": [
      {"max_num_batched_tokens": 16384},
      {"max_num_batched_tokens": 24576},
      {"max_num_batched_tokens": 32768}
    ],
    "objective": {
      "test": "e2e-prefix-matrix",
      "metric": "output_throughput",
      "direction": "maximize"
    }
  }
}
```

```bash
python skills/ascend-multinode-comm/scripts/service_workflow.py validate \
  --config /secure/local/deployment.tuning.json

python skills/ascend-multinode-comm/scripts/service_workflow.py plan \
  --config /secure/local/deployment.tuning.json \
  --out reports/tuning-plan.json

# TUNING_PLAN_SHA256 必须来自上述 tuning-plan.json。
python skills/ascend-multinode-comm/scripts/service_workflow.py tune \
  --config /secure/local/deployment.tuning.json \
  --execute --approve TUNING_PLAN_SHA256 \
  --confirm-deployment DEPLOYMENT_NAME --out reports/tuning.json
```

baseline 失败、清理不完整或命中致命模式时会停止；普通候选 OOM 只有在精确清理完成且没有 fatal pattern 时才可能继续。best 默认不会自动重新上线：应把它写入无模板的最终配置，重新 plan、launch 和全量测试。

当前尚未实现根据实测自动生成下一候选、自动重分配 P/D 卡预算、主机/设备联合装箱、自适应停止、置信区间或 checkpoint/resume。

## 手工通信测试

[`manual-tests/`](skills/ascend-multinode-comm/manual-tests/README.md) 提供两个简单入口：

- `run_hccl_test.sh`：运行官方 HCCL Test 的 `alltoall` 或 `broadcast`，适合健康卡/疑似故障卡只改设备号的对照。
- `torch_collectives.py`：通过 `torch.distributed` HCCL backend 运行 AllReduce、Broadcast、AllGather、AllToAll，并逐 rank 校验数值。

```bash
# 需先设置当前环境的 MPI_HOME、CANN_HOME、HCCL_TEST_DIR 和 HCCL_SOCKET_IFNAME。
# 另按 manual-tests/README.md 创建 hostfile；双机八卡内容示例为两行：
# node-a:8
# node-b:8
bash skills/ascend-multinode-comm/manual-tests/run_hccl_test.sh \
  alltoall /path/to/hostfile_8x2

# 两个节点分别运行，只修改 --node-rank。
torchrun --nnodes=2 --nproc-per-node=1 --node-rank=0 \
  --master-addr=<节点0可达IP> --master-port=29500 \
  skills/ascend-multinode-comm/manual-tests/torch_collectives.py \
  --device-ids 7 --op all
```

两者验证的是普通 collective，不会调用融合 MC2 或 MoE Dispatch/Combine API。Shell 会运行 MPI 并写带时间戳的日志；显式复用 `LOG_FILE` 时可能截断同名文件。

## 如何读状态和退出码

| 字段/值 | 含义 |
|---|---|
| `DRAFT_RECOMMENDATION` | 输入仍有占位符或关键环境指纹缺失，只能作为填写示例 |
| `RECOMMENDED_FOR_VALIDATION` + `THEORY_ONLY` | 理论排序已生成，仍必须 verify，不是实测性能推荐 |
| `execution=PLAN_ONLY` | 只生成了待审阅计划，没有执行对应探针或服务；例如 service plan 同时可为 `status=PASS` |
| `PASS` | 指定 scope 在报告绑定的节点、版本、配置和时间窗内通过 |
| `FAIL` | 已有明确失败证据 |
| `UNVERIFIED` | 没执行、证据不完整或工具不覆盖；不是失败，也绝不是通过 |
| `PARTIAL` | 只执行了计划中的子集，不能当作完整验收 |

部分证据工具用退出码 2 表示 `UNVERIFIED`/证据不完整，例如只做 `inspect`、静态审计未发现确定错误或 HCCL 进程完成但正确性仍需人工核对。自动化调用方应读取 JSON 状态，不能只把所有非零返回码都解释成“程序崩溃”。

## 示例文件地图

| 示例 | 用途 | 使用前必须知道 |
|---|---|---|
| [`parallelism-advisor.json`](skills/ascend-multinode-comm/examples/parallelism-advisor.json) | 理论规划 sidecar | 原样运行只会得到草稿；替换全部 `fill-current-*` 并核实 allow-list/容量下界 |
| [`cluster.json`](skills/ascend-multinode-comm/examples/cluster.json) | 多节点通信预检 | 替换 SSH/容器/网卡/设备；`check/pairs` 是主动测试 |
| [`fabric-nodes.json`](skills/ascend-multinode-comm/examples/fabric-nodes.json) | 宿主 Fabric 采集 | 只接受宿主 SSH 信息，不放密码、容器或 cluster 字段 |
| [`mc2-cases.json`](skills/ascend-multinode-comm/examples/mc2-cases.json) | MC2 case 片段 | 必须合入 cluster 配置，并逐项填写当前版本 support reference |
| [`service-workflow.json`](skills/ascend-multinode-comm/examples/service-workflow.json) | 生命周期/测试/tune schema 示例 | `example:true`、假地址和零哈希会阻止执行；含 tuning 的配置不能直接普通 launch |
| [`audit-demo/`](skills/ascend-multinode-comm/examples/audit-demo) | 静态审计故障 fixture | 故意包含错误，禁止当部署脚本执行 |

本地配置建议使用 `*.local.json`，测试报告写入 `reports/`；两者已在 `.gitignore` 中排除。不要把密码、私钥、令牌、真实地址、现场路径、原始脚本或日志提交到 Git。

## 安全与授权边界

| 等级 | 示例 | 默认边界 |
|---|---|---|
| 本地离线 | advisor、静态 audit、validate、plan、gate、HCCL 计划 | 不连接服务器；可能在本地生成报告 |
| 远端采集 | preflight/fabric `inspect`、现场日志和状态查询 | 会 SSH 并执行采集命令；不启动业务服务。只有 env script 为空或已审阅为纯初始化时，preflight inspect 才可按只读采集对待 |
| 主动通信探针 | preflight `check/pairs`、fabric `ping --execute`、HCCL/MC2 | 需要明确空闲卡、端口、时间窗和影响范围 |
| 服务变更 | launch/test/tune/stop | 需要匹配的 plan SHA 和显式批准；stop/tune 还需确认 deployment 名称 |

首次 SSH 连接可能按工具策略写入控制端 `known_hosts`；`preflight.py` 使用非交互 BatchMode，需要预先可用的认证和主机密钥条件。工具不会自动创建/删除容器、拉取镜像、重置 NPU、修改防火墙/路由、扩大系统权限或模糊杀进程。现场排障本身不授权修改、重启或清理服务。

## 仓库结构

```text
skills/ascend-multinode-comm/
├── SKILL.md                 # 能力路由和核心约束
├── scripts/                 # 离线分析、预检、生命周期等确定性工具
├── examples/                # 脱敏且默认不可直接执行的配置/fixture
├── references/              # 平台、通信、PD、排障和生命周期细则
└── manual-tests/            # 用户可直接在服务器运行的最小通信测试
tests/                       # 控制面与安全边界单元测试
docs/validation.md           # 验证记录和历史真实证据的脱敏摘要
```

常用参考入口：

- [PD 并行、特性、OOM 与完整验收](skills/ascend-multinode-comm/references/vllm-pd-operations.md)
- [配置驱动的服务生命周期与寻优](skills/ascend-multinode-comm/references/service-lifecycle.md)
- [单机/双机/多机场景和工具覆盖边界](skills/ascend-multinode-comm/references/scenario-examples.md)
- [官方部署配方索引](skills/ascend-multinode-comm/references/official-deployment-recipes.md)
- [远端连接与现场排障](skills/ascend-multinode-comm/references/remote-server-audit.md)
- [验证记录](docs/validation.md)

## 当前验证范围

CI 在 Ubuntu/Windows、Python 3.10/3.12 上运行标准库单元测试，覆盖严格配置解析、候选约束、SSH 命令构造、通信证据归属、报告防覆盖、生命周期所有权、失败回滚、E2E assertions 和有限 tune 状态机。

```bash
python -m unittest discover -s tests -v
```

这些测试验证的是控制面逻辑和 mock 场景，不等于所有 Ascend 型号、CANN/vLLM-Ascend 版本、SSH/Docker/NPU、模型精度或性能都已经上板通过。真实结论必须绑定本次硬件、软件指纹、配置、负载和现场报告；详见 [验证记录](docs/validation.md)。

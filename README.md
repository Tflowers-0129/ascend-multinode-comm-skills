# Ascend 单机/多机通信、服务与性能 Skills

面向 Ascend A2/A3/A5 与 vLLM-Ascend，覆盖从“选择并行策略”到“现场验证”的完整闭环：理论分析 DP/TP/PP/EP，通信预检与建链排障，配置驱动的 P/D/Proxy 生命周期，E2E 测试，以及 quick/exhaustive 有界寻优。

官网脚本和历史成功配置只作为有版本条件的 baseline。理论候选、通信证据、服务健康、精度和性能分别判定；任何一层都不能代替下一层。远端脚本可以只提供服务器内路径，不要求先上传到仓库。

## 五个主能力

| 能力 | 输入 | 主要产物 |
|---|---|---|
| 理论并行规划 | 模型/版本、量化与 cache、卡数、P/D 预算、合法 TP/PP 范围、固定负载、目标指标及同口径历史显存证据 | P/D DP×TP×PP 首选与备选、理由、假设、最小验证计划 |
| 通信预检 | 节点、容器、工作目录、部署脚本与允许的空闲资源 | Host/TCPStore/Gloo/HCCL/MC2/PD-KV 的已测、失败和未测范围 |
| 现场排障 | 现有进程、日志、网络/设备状态和失败时间线 | 最早失败阶段、节点/rank/链路证据与最小修复建议 |
| 服务生命周期 | 严格本地配置、远端前台脚本、artifact 哈希和明确授权 | ownership-scoped launch/status/test/stop 报告与可回滚 run ID |
| 性能验证与寻优 | 已通过 baseline、固定 workload、主指标、硬 SLO 和预算 | verify、quick 或多阶段 exhaustive 报告；最终脚本需重新 plan 和全量验收 |

技能入口与完整路由见 [`SKILL.md`](skills/ascend-multinode-comm/SKILL.md)。

## 接入方式

- 支持技能加载器的 agent：安装或加载整个 `skills/ascend-multinode-comm` 目录，不能只复制 `SKILL.md`。
- 能读取文件但没有技能加载器的 agent：先读 `SKILL.md`，再按其中路由只打开当前任务需要的 reference；有远端能力时仍需使用自身的 SSH/命令执行工具。
- 只需本地工具时：可直接运行下方 Python CLI。没有文件读取、远端访问或执行能力的 agent 必须明确缺口，不能声称已完成现场验证。

## 推荐流程

```text
analyze（纯离线理论候选）
  → verify（一次最小启动、正确性和代表负载）
    → quick（锁定版本/资源，小量高收益候选）
      → exhaustive（拓扑→特性→数值→全量E2E→重复/长稳）
```

理论候选达到目标时可以在 verify 后结束。若已有同模型/检查点、量化/cache、软件栈、硬件、拓扑与负载的真实服务日志，先用它校准容量硬下界、保守静态估算和候选边界；日志只能证明该候选成功到哪一阶段或为何失败，不能单独证明所有拓扑可运行。`quick` 和 `exhaustive` 不是简单放大 `max_trials`：后者按阶段推进，每阶段仍最多 32 个显式候选，阶段间重新生成、审阅和批准 plan。当前 `service_workflow.py` 是安全的有限候选执行器，不冒充自适应搜索器。

性能比较采用一个主指标和硬门槛：失败请求为零、精度与稳定性通过、无 OOM/device fault/EngineDead/KV 致命错误，并满足用户 TTFT/TPOT、显存余量等 SLO。输入/输出长度、并发和 Prefix 口径在候选之间固定，不能通过降低测试负载制造提升。

## 工具

| 工具 | 用途 | 是否会连接或改变远端 |
|---|---|---|
| `scripts/parallelism_advisor.py` | 枚举显式合法的 P/D DP×TP×PP，在给定并发负载下按 TTFT、TPOT、output/request throughput、goodput 或 balanced 目标排序 | 否；纯离线，只输出 `THEORY_ONLY` 候选 |
| `scripts/preflight.py` | 容器/宿主通信发现、分阶段主动探针、gate 和卡对隔离 | `inspect` 只读；`check/pairs` 会在授权资源上运行短时探针 |
| `scripts/fabric_probe.py` | 宿主设备映射、HCCS/vNIC/Pod/SDID 与有界设备小包 | `inspect` 只读；显式 `--execute` 才发包 |
| `scripts/service_workflow.py` | 严格 plan、服务生命周期、E2E 测试和单阶段有限寻优 | `validate/plan` 本地；其余动作需 `--execute --approve` |
| `scripts/hccl_bench.py` | 生成或执行官方 HCCL Test MPI 计划 | 默认只生成；显式 `--execute` 才运行 |
| `scripts/audit_deployment.py` | 对已有本地文本做辅助静态审计 | 否；不能替代现场检查 |

工具不创建/删除容器，不重置 NPU，不按名称、端口或 `pkill/killall` 模糊清理服务。生命周期工具只停止具有 deployment、service、规格哈希、boot ID、PID/starttime、进程组和 run ID 所有权证据的进程。

## 快速开始

### 1. 先做并行策略分析

复制公开 sidecar，填写脱敏且已核实的版本、角色卡预算和合法范围：

```bash
cp skills/ascend-multinode-comm/examples/parallelism-advisor.json \
  skills/ascend-multinode-comm/examples/parallelism-advisor.local.json

python skills/ascend-multinode-comm/scripts/parallelism_advisor.py \
  --config skills/ascend-multinode-comm/examples/parallelism-advisor.local.json \
  --out reports/parallelism-advice.json
```

以下命令中的 `python` 表示 Python 3.10+；Windows 未配置该命令时可使用 `py -3` 或解释器绝对路径。

例如 P、D 各写 32 张卡时，`hardware.cluster_devices` 至少为 64。`tp_sizes/pp_sizes`、`minimum_replica_devices` 和兼容约束必须来自当前模型/版本、容量硬下界与保守静态估算，或同口径历史日志，不能为了得到预想拓扑倒填；分析器本身不会读取权重或日志并自动算出这些值。输出只用于生成或修改用户同风格脚本，执行前仍需重新 plan。
公开样例保留 `fill-current-*` 占位符，因此直接运行只会得到 `DRAFT_RECOMMENDATION`；填完环境指纹并重新运行后，状态才可能是 `RECOMMENDED_FOR_VALIDATION`。

### 2. 通信预检

```bash
cp skills/ascend-multinode-comm/examples/cluster.json \
  skills/ascend-multinode-comm/examples/cluster.local.json

python skills/ascend-multinode-comm/scripts/preflight.py inspect \
  --config skills/ascend-multinode-comm/examples/cluster.local.json \
  --out reports/inventory.json
```

`inspect` 通常返回 2，表示仍有未验证层，不是执行错误。主动 `check`、卡对、HCCL、MC2 和 KV 验证前需确认空闲卡、端口、时间窗与影响范围。

### 3. 服务计划和执行

```bash
python skills/ascend-multinode-comm/scripts/service_workflow.py validate \
  --config /secure/local/deployment.local.json

python skills/ascend-multinode-comm/scripts/service_workflow.py plan \
  --config /secure/local/deployment.local.json \
  --out reports/deployment-plan.json
```

人工核对 plan 后，`launch/status/test/tune/stop` 才使用匹配的 `--execute --approve PLAN_SHA256`；`stop/tune` 还要求再次确认 deployment。完整配置与命令见 [服务生命周期](skills/ascend-multinode-comm/references/service-lifecycle.md)。

## 参考资料

### 性能、部署与样本

- [PD 并行推导、OOM、特性和验收规则](skills/ascend-multinode-comm/references/vllm-pd-operations.md)
- [服务生命周期与 analyze/verify/quick/exhaustive](skills/ascend-multinode-comm/references/service-lifecycle.md)
- [官方部署配方索引](skills/ascend-multinode-comm/references/official-deployment-recipes.md)
- [单机/双机/多机场景示例](skills/ascend-multinode-comm/references/scenario-examples.md)
- [成功样本的选择、脱敏和归档](skills/ascend-multinode-comm/references/known-good-deployments.md)

### 通信、硬件与故障

- [通信阶段矩阵](skills/ascend-multinode-comm/references/communication-stages.md)
- [A3/A5 平台分支](skills/ascend-multinode-comm/references/platform-a3-a5.md)
- [拓扑和配置文件](skills/ascend-multinode-comm/references/topology-and-files.md)
- [宿主机设备网络检测](skills/ascend-multinode-comm/references/host-fabric-detection.md)
- [HCCL 检测](skills/ascend-multinode-comm/references/hccl-testing.md)
- [MC2 算子级检测](skills/ascend-multinode-comm/references/mc2-testing.md)
- [现场故障手册](skills/ascend-multinode-comm/references/failure-playbook.md)
- [仿真与验收边界](skills/ascend-multinode-comm/references/simulation-and-gates.md)

### 接入与静态分析

- [SSH 与现场排障指引](skills/ascend-multinode-comm/references/remote-server-audit.md)
- [部署脚本分析规则](skills/ascend-multinode-comm/references/deployment-script-audit.md)
- [手动通信测试](skills/ascend-multinode-comm/manual-tests/README.md)

密码、私钥、令牌、真实地址、现场路径、原始脚本和日志不进入 Git。公开仓库只保存脱敏结构、条件化经验和合成测试数据。

## 验证状态

本仓库包含标准库单元测试与少量历史现场证据，但本地开发机无 NPU。mock、静态分析或理论排序不能称为真实 SSH、容器、HCCL/MC2/KV、模型精度或性能通过。详见 [验证记录](docs/validation.md)。

```bash
python -m unittest discover -s tests -v
```

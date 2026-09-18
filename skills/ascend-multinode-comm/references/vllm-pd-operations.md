# vLLM-Ascend PD 分离部署、调优与验收

本参考用于 vLLM-Ascend 的 Prefill/Decode（PD）分离服务：根据用户给出的节点、容器、模型和并行配置生成或审计启动方案，安全拉起服务，定位 P/D 启动与运行问题，并完成性能和正确性验收。它记录的是带条件的运维规则，不是某个集群的固定脚本。

检索词：`P/D topology`、`TP/PP/DP`、`prefix cache`、`fused MC2`、`multistream`、`D startup`、`P OOM`、`max-num-batched-tokens`、`proxy`、`E2E acceptance`。

## 1. 先固定配置与授权边界

开始前把用户输入规范化为一份运行清单；缺失但能从现有脚本或容器只读发现的信息，优先现场发现，不凭经验补值。

- 节点：主机标识、P/D/Proxy 角色、每机可见 NPU 数、管理面与数据面地址。
- 软件：容器或镜像、vLLM、vllm-ascend、CANN、HDK/驱动及固件版本。
- 模型：模型路径、量化类型、最大模型长度、MTP/推测解码等额外能力。
- 并行：P 和 D 分别使用的 DP、TP、PP、EP；不要假设两侧相同。
- PD 通信：KV connector/传输后端、监听地址、端口、rank 或实例标识、池化方式。
- 调度与内存：`max-num-batched-tokens`、`max-num-seqs`、`gpu-memory-utilization`、prefix cache、chunked prefill 等。
- 优化开关：fused MC2、multistream，以及版本专属的 additional config。
- 测试：请求入口、数据规模、输入/输出长度、并发、重复率、正确性判据和结果路径。

拉起、停止、杀进程、改脚本和运行有负载的性能测试都是不同的动作。只在用户授权的范围内执行；用户允许清理“其他服务”时，也先以容器、进程命令行、监听端口和设备占用交叉确认精确目标，不能按模糊名称清空整机。

密码、私钥、令牌、真实地址和用户私有目录不得进入技能、Git、日志摘要或示例。若需保存可复用配置，使用占位符或外部受控配置文件。

## 2. 正确理解 P、D、Proxy 与启动单元

P 节点负责 prefill，消费输入 token 并产生可供 D 使用的 KV 状态；D 节点负责 decode，持续生成输出 token。Proxy/路由层负责接受客户端请求、选择健康的 P/D 实例，并协调一次请求在两类实例间流转。

“1P1D”通常描述一组逻辑 P 服务和一组逻辑 D 服务，不保证各自只占一台机器、一个进程或一个启动脚本。判断拓扑必须看实际 rank、world size、监听器和进程树：

- 一个 P 或 D 逻辑实例可以由 TP/PP 跨多机组成。
- 一个 launcher 可以通过分布式启动器、SSH、Ray 或框架 RPC 在多机创建 worker。
- 一个脚本也可能只启动本机 rank，需要所有节点各执行一次。
- 因此“脚本数量”“API endpoint 数量”“机器数量”和“模型副本数量”不能互相代替。

对用户解释“为什么两个脚本能拉起四台机器”时，应指出脚本中的实际远端启动或分布式 rendezvous 机制，以及每个脚本覆盖的 rank；没有读取脚本和进程证据时，不断言具体机制。

### TP、PP、DP 的容量核算

在常见且无额外复制层的模型 worker 布局中，每个角色需要的设备数可先按下式核对：

```text
role_world_size = DP × TP × PP
```

该等式是核算起点，不替代框架的实际 rank 语义。部分版本把 DP launcher、外部副本或专家并行组织在独立层次；必须用运行时 rank 表验证。

- TP 将单层张量切到多卡，要求各 TP rank 的模型、网络和集合通信一致。
- PP 将模型层分段；`PP > 1` 时，一个 DP 副本会跨多个 pipeline stage，不能把每个 stage 当成独立 API 服务。
- 普通 replicated-DP 会复制完整的 TP×PP 模型组；启用跨 DP 的专家/权重分片时不适用，必须按当前 EP 实现和实际 rank placement 核算。DP 地址、起始 rank、本地 DP 数和 RPC 端口仍必须形成唯一且完整的 rank 空间。
- EP 可能改变专家通信域，但不应被误算成额外模型副本；以当前版本实现为准。

若计划设备数与现场可见卡数不相等，先停止启动。典型错误包括 DP start rank 重叠、某节点 local size 不一致、PP stage 缺失、不同节点看到的设备顺序不同，以及同一端口被两组 rendezvous 复用。

### 按目标指标先推导并行策略

不要一开始穷举所有 DP/TP/PP。先把模型可运行条件、角色设备预算、单机快速互联域、版本兼容范围和固定负载写成事实，再按目标指标生成首选与保守候选。离线辅助工具见 [`parallelism_advisor.py`](../scripts/parallelism_advisor.py)，公开输入样例见 [`parallelism-advisor.json`](../examples/parallelism-advisor.json)。它只枚举用户已核实的合法范围并给出可解释排序，不连接服务器、不生成服务命令，也不把理论候选称为已验证结果。

常见先验如下，但都必须附带成立条件：

- P 处理批量输入 token，长 Prompt/TTFT 目标通常偏向较强的单副本 TP；当大 TP 会跨节点做频繁 collective，而模型支持 PP、并发或 Prefill chunk 足以填充流水线时，可用节点内 TP 加跨节点 PP。低并发、短输入或 stage 不平衡时，PP bubble 可能更差。
- P 的 DP 不应固定为小值。P 排队、短 Prompt 高 QPS 或 Prefix 命中率低导致的未缓存输入量很大时，应增加 P 副本；单请求 Prefill 计算或长上下文是主瓶颈时，再优先增强 TP/PP。
- D 在高并发、output throughput 目标下，通常选择满足模型、KV 显存、算子支持和 TPOT 门槛的较小 TP，再用剩余设备增加 DP。DP 不是越多越好：它会复制模型、减少每个副本的卡数和批量，可能损害 KV 容量、单请求 TPOT、kernel 效率或 EP/路由均衡。
- 低并发且 TPOT 优先时，D 可能需要更大的 TP 和更少的 DP；长输出、高并发时才更偏向更多 D 副本。P/D 的最优形态不要求相同。

用固定负载先估算两侧的相对压力：

```text
P_load_proxy = QPS × 平均未缓存输入 token 数
D_load_proxy = QPS × 平均输出 token 数
```

这是工作量代理，不是把 Prefill 与 Decode 每 token 成本视为相同，也不是吞吐预测。`parallelism_advisor.py` 的 `mean_uncached_input_tokens` 已是缓存复用后的口径，`prefix_hit_rate` 只用于记录评测条件，不会再乘一次。真实容量还受模型结构、量化、KV、算子、通信和调度影响；当前分析器固定用户给定的 P/D 设备预算，角色卡数再分配必须在理论分析或一轮实测校准后显式提出。

| 首要目标 | P 侧推导重点 | D 侧推导重点 |
|---|---|---|
| TTFT | 增强 P 单副本能力；节点内大 TP，跨节点时按条件考虑 PP；P 排队时再加副本 | 保持不会拖累端到端请求的基本容量 |
| TPOT | P 不排队即可 | 增强 D 单副本能力，不能简单最大化 DP |
| output/request throughput | 提供足够 Prefill 供给，避免入口饥饿或积压 | 在显存和 TPOT 门槛内采用较小 TP、较多 DP |
| 长输入、低 Prefix 命中 | 增加 P 资源，评估 TP/PP、chunked prefill | 按输出量配置 |
| 长输出、高 Prefix 命中 | P 容量可相对减少 | 增加 D 容量和可用 KV，重点检查路由均衡 |
| 综合 goodput | 以用户 TTFT/TPOT/错误率 SLO 为硬门槛，平衡两侧队列 | 同左，不以单次峰值替代 SLO |

历史成功脚本和结果只作为同平台、同版本、同模型、同负载下的先验，用于设置 allow-list、最小副本卡数和排除已知失败区；原始样本治理见 [成功样本指引](known-good-deployments.md)。目标机器上的最小启动、正确性和代表性负载仍是推广脚本前的必要校准。

## 3. 用版本配对保护可运行性

vLLM、vllm-ascend、PyTorch/torch-npu、CANN、HDK/驱动和模型代码共同决定参数含义与算子路径。只比较 vLLM 或 vllm-ascend 的单一版本，不能解释全部差异。

复用成功部署时记录完整版本指纹和镜像 digest；比较失败部署时同时检查：

1. CLI 参数是否在该版本存在，默认值是否改变。
2. KV connector、prefix cache、MTP、MC2 和 multistream 是否由该版本组合支持。
3. Host 驱动/HDK 与容器 CANN 是否处于支持矩阵内。
4. P、D 是否来自同一兼容版本族，并使用相同模型与 tokenizer 资产。
5. 版本变更是否改变 HTTP/SSE 输出、内部 KV 协议或健康检查接口。

已知某些池化路径在较旧 HDK 上可能卡住时，只能写成受版本条件约束的风险：先确认现场确实启用了对应池化实现，并核实厂商或项目给出的最低版本。例如已确认受影响范围要求 HDK 26.1 或更高时，低于该版本应禁用该路径或升级，而不是把“所有 prefix cache 都会卡”当作结论。

## 4. Prefix cache、池化、fused MC2 与 multistream

### Prefix cache

Prefix cache 是请求调度/缓存能力；PD KV 传输或 KV 池化是跨角色的数据通路。二者有关联但不是同一开关。排障时分别回答：缓存是否命中、KV 是否成功传输、池化资源是否可用。

- 用户要求开启 prefix cache 时，P/D 两侧和 Proxy 的实际生效状态都要从进程参数或运行日志验证，不能只检查脚本文本。
- 修改一个开关后重新拉起相关角色，避免旧进程仍持有上一轮配置。
- “请求成功”不证明命中缓存。若指标端点可用，记录命中率或复用 token；若指标端点不存在，把指标记为不可用，不将其误判为请求失败。
- 压测数据必须真的包含预期比例的公共前缀，否则无法评价 prefix cache。

### EPLB

EPLB 是 MoE/EP 场景的专家负载再平衡能力，不是通用的 DP、TP 或请求路由开关。只有当前模型确实启用 EP、版本支持对应 controller/参数、并且实测存在专家热度倾斜时才把它列为候选。上游字段和 Ascend 分支会随运行器演进；先核对当前 [vLLM EPLB 配置](https://docs.vllm.ai/en/latest/api/vllm/config/parallel/) 与 [vLLM-Ascend EPLB 指引](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/expert_parallelism_load_balancer.html)，不要把其他版本的 `window_size`、`step_interval`、冗余专家数、异步 communicator 或 load collection phase 直接复制过来。

- 冗余专家会占额外显存；专家迁移和 balancedness 日志也可能增加通信或观测开销。先固定版本、EP 拓扑和负载，记录开启前后的专家热度、迁移事件、峰值显存、TTFT/TPOT、吞吐和失败请求。
- P/D 分离时可因负载阶段不同而采用不同的采集/平衡策略，但必须由当前版本明确支持；不能从“P 长输入、D 长输出”直接推导某个字段值。
- 没有通用证据表明 EPLB 与所有 fused MC2 冲突。只在当前版本文档、参数校验或复现实测证明互斥时禁止组合；否则以 baseline → 单开 EPLB → 单开 fused MC2 → 显式允许后再测组合的有界矩阵判断。
- 当前 `service_workflow.py` 的 typed `features` 只覆盖 prefix cache、KV pool、fused MC2 和 multistream，不会自动验证 EPLB。使用 EPLB 时将已核实参数显式写入 engine argv/env，并在 pinned 值、版本指纹、启动日志与测试断言中留证；缺少生效证据时保持 `UNVERIFIED`。

### fused MC2 与 multistream

在已明确声明二者互斥的 vLLM-Ascend/模型/算子组合中，fused MC2 与 `multistream_overlap_shared_expert` 不得同时开启。遇到该组合时优先遵循当前版本官方文档和运行时校验，而不是沿用另一镜像的成功参数。

可复用的决策方式是：

- 若选择 fused MC2，关闭冲突的 multistream 路径，并验证实际进入融合算子。
- 若选择 multistream，关闭冲突的 fused MC2 配置，并验证多流路径生效。
- P、D 可因工作负载不同采用不同策略；不要把 P 的优化开关无条件复制到 D。
- 没有版本或算子证据时，不把互斥关系扩大到所有 MC2 或所有 multistream 功能。

每次只改变一个优化维度，再用相同数据集复测正确性和性能。若开关只是被解析但未实际进入目标 kernel，不能记为该特性的通过结果。

## 5. 启动顺序与可观测性

不同 connector 可能要求不同顺序；以当前实现的依赖关系为准。一个稳妥的通用流程是先准备日志目录和端口，再启动依赖方，等待其达到就绪条件，然后启动另一侧，最后启动 Proxy。不要用固定 `sleep` 代替就绪探测。

每个节点和角色必须有独立日志，例如按 `role + host + instance` 命名。日志至少保留：

- 完整命令或脱敏后的等价配置；
- 版本指纹、时间戳和本地 rank；
- stdout/stderr；
- 启动 PID、退出码和监听端口；
- 本轮测试开始前的日志偏移或时间基线。

服务“已拉起”需要同时满足：

1. 启动器和 worker 进程仍存活，且进程命令行与计划一致。
2. 预期端口由预期 PID 监听，没有旧实例抢占。
3. NPU 进程和显存占用与 rank 数吻合。
4. 角色健康接口成功，且可完成一个最小请求。
5. Proxy 报告的 P/D 实例数量与计划一致。

仅看到容器在运行、日志出现 `ready`、端口可连接或 HTTP 200 中任一项，都不足以单独证明整套 PD 服务健康。使用 `pgrep -af` 时还要排除查询命令自身；以 PID、父子关系和端口归属交叉验证。

## 6. D 节点启动失败的定位顺序

D 失败常在上层表现为 Proxy 无后端、连接超时或某个 rank 等待；这些通常是结果，不一定是首因。按日志时间线寻找所有 rank 中最早的异常。

### 6.1 启动前事实

- 计划的 D 数、DP/TP/PP/world size 与实际设备数一致。
- 每个实例的 start rank、local rank、设备列表和端口唯一。
- P/D 的 KV connector 类型、序列化协议、模型标识和必要端口一致。
- 容器能看到正确设备、数据面网卡、共享模型目录和所需配置。
- 目标卡没有被残留 worker 或其他容器占用；端口没有残留监听。
- 运行中的环境变量来自服务进程，而非另开一个 `docker exec` 后的 shell。

### 6.2 首因分类

- **进程立即退出**：先看参数解析、模块导入、模型文件、版本 ABI、设备初始化和 OOM。
- **部分 rank 就绪，其余等待**：检查 world size、rank 重叠/缺失、地址绑定、端口、防火墙及分布式 store。
- **模型加载后卡住**：检查首次 collective、KV 池化、prefix cache、MTP dummy batch，以及 HDK/CANN 对特性路径的支持。
- **健康接口未就绪但进程存在**：检查 engine core 子进程、服务内部状态和首次编译，不以 Proxy 超时作为根因。
- **启动后很快死亡**：同时检查设备日志、kernel fault、宿主 OOM killer 和容器退出状态。

若日志出现 device page fault 或越界地址，不要仅通过增加等待或重启掩盖。保存故障时间窗口内的框架、CANN/设备日志和模型配置，确认是稳定复现还是某个优化路径触发，再缩小到版本、算子或 batch 条件。

## 7. P 节点 OOM：区分 KV 预算与单步工作区

P 启动或首批请求的 OOM 常见于模型权重、KV cache、激活、通信 buffer、MTP/dummy batch 和碎片化共同竞争显存。日志中的“总空闲显存”大于一次申请量，也不保证存在足够的连续空间或框架保留余量。

两个参数解决的不是同一个问题：

- `--max-num-batched-tokens` 限制单次调度可处理的 token 总量，降低它通常能减少峰值激活和某些 warm-up/dummy batch 工作区。
- `--gpu-memory-utilization` 主要影响框架可用于 KV cache 等用途的预算；降低它可留下更大运行余量，但也会减少 KV 容量。

因此不要形成“P OOM 一律降低显存利用率”的规则。若证据表明 OOM 出现在大 token 批次或 MTP dummy batch，并且用户要求保留 KV 预算，可先在相同内存利用率下分档降低 `max-num-batched-tokens`，例如从 32K 降到 16K，然后重新验证目标长度和吞吐。若 OOM 发生在 KV cache 初始化或最小 batch 仍无工作区，再评估降低内存利用率、并行切分、模型长度或并发。

每次调整后必须复查：

- 最大模型长度所需 KV 容量仍足够；
- 目标并发和吞吐未退化到不可接受；
- warm-up、首个真实请求和长稳测试均无 OOM；
- 所有 P rank 的显存余量都满足要求，而非只看 rank 0。

不要只依赖 `npu-smi` 某一时刻的空闲值；关联各 rank 的分配日志、失败申请大小和峰值时间。

### 候选级显存判定：静态估算与真实日志校准

`minimum_replica_devices` 不能只按总权重除以集群总显存填写。先计算权重、必要 KV 等不可省略项的容量硬下界，再为每个候选做含瞬时项和余量的保守静态估算，最后用相同口径的真实启动/请求日志校准。判定对象是所有 rank、所有 PP stage 中的最坏一张卡，而不是集群显存平均值。下式是容量预算估算，不是数学下界或精确峰值预测：

```text
rank_capacity_budget ≈
  resident_weight
+ resident_KV
+ resident_runtime_and_comm
+ max(load_transient, compile_or_graph, warmup_or_MTP_dummy, request_workspace)
+ fragmentation_and_safety_margin
```

- 在普通 replicated-DP、没有跨 DP 专家/权重分片时，DP 复制完整的 TP×PP 副本，不帮助单副本分摊权重。因而 `DP16×TP1` 即使集群总显存看似足够，每个 TP1 副本仍可能在单 rank 上装不下；若启用 EP 或其他跨 DP 分片，必须改按实际专家归属和 rank placement 计算，不能套用这条简化规则。
- `checkpoint_size ÷ TP ÷ PP` 只能作为权重下界。PP stage 不均衡、embedding/head、MoE 专家归属、量化加载或反量化临时副本、通信 buffer 和运行时常驻都会造成偏差；相同的 `TP×PP` 乘积也不保证相同峰值。
- KV 预算必须同时绑定 cache dtype、block size、最大长度、`max-num-seqs`、角色和实际并发。P、D 即使使用同一模型，KV 与瞬时工作区也可能不同。
- `gpu-memory-utilization` 是框架预算控制，不是“真实峰值必定等于该比例”。采样式 `npu-smi` 可能漏掉短时峰值，只能作为旁证。

为每次候选建立脱敏的“显存证据卡”，至少记录：

| 类别 | 必要字段 |
|---|---|
| 候选身份 | trial/run ID、时间窗口、P/D、rank、PP stage、实际 placement |
| 硬件与软件 | 卡型与单卡 HBM、镜像 digest、vLLM/vllm-ascend、CANN、HDK/驱动 |
| 模型与 cache | 模型/检查点哈希、量化与权重 dtype/load format、KV dtype、block size、最大长度、`max-num-seqs` |
| 拓扑与峰值参数 | DP×TP×PP×EP、`max-num-batched-tokens`、MTP/推测解码、graph/eager、prefix、EPLB、MC2/multistream |
| 现场与结果 | 同卡残留进程、失败阶段/rank、申请/free/allocated/reserved 字节、退出码、日志偏移，以及启动、首请求、目标 E2E、长稳分别是否通过 |

证据卡是人工或 agent 归一化的 sidecar/报告，不是当前 `parallelism_advisor.py` 或 `service_workflow.py` 已支持的新配置字段。`minimum_replica_devices` 只能表达角色级粗粒度下界，当前 advisor schema 没有合法 `(TP, PP)` pair 白名单；若日志只否定某个特定组合，必须在外部预筛或人工审阅，不能把整个乘积范围错误排除，也不能声称工具已自动消费证据。原始 IP、账号、私有路径和现场日志不进入仓库。

按“失败阶段 + 原始证据”分类，避免所有异常都写成 OOM：

| 分类 | 需要的证据 | 可下的结论 |
|---|---|---|
| 权重/常驻容量不足 | 权重加载阶段有明确 allocator OOM，或排除文件/版本/算子错误后核算出常驻项超过预算 | 当前候选的单 rank 常驻布局不成立 |
| KV 容量不足 | KV 日志明确报告预算/block 不足，或目标长度/并发所需 block 超过已分配容量 | 当前 cache 口径不成立；普通 profiling 异常本身不足以归类，也不等于 warmup 工作区不足 |
| warmup/MTP dummy/请求工作区 OOM | 权重与 KV 已建立，随后在 dummy、graph capture、首批或目标请求失败 | 基础模型可能装得下，但该候选的瞬时峰值不成立 |
| allocator OOM 未分类 | 只有 failed-to-allocate，没有可靠阶段或 allocator 上下文 | 只能判当前候选失败，不能猜权重、KV 或碎片根因 |
| 碎片/连续分配风险 | allocator segment/reserve/连续块证据与失败时间线一致 | 可列为碎片风险；“free 大于申请量”单独不足以证明碎片 |
| 宿主/容器 OOM | exit 137/SIGKILL，并有 `memory.events`、cgroup 或 kernel OOM 证据 | CPU/容器内存问题，不能归入 NPU 显存 |
| NPU device fault | 设备/CANN 日志出现 page fault、非法 GM 地址、越界或 vector core 异常 | 独立致命类；不能靠降低利用率或反复重启当作普通 OOM |

后续的 EngineDead、HCCL watchdog、TBE 子进程退出、SIGTERM/SIGKILL 可能只是 worker 首因后的清理结果。判断“是不是被杀”时先找最早的设备/allocator/宿主证据，不能把最后一次 SIGKILL 当根因。

#### 脱敏现场样本：32K 失败与 16K 通过

一次 64 GiB 级 A2、GLM-5.3 W8A8C8 的 P 侧候选使用 `DP2×TP8×PP2`、vLLM 0.23.0、Ascend 量化、BF16 KV、MTP、prefix cache、135K 最大长度和 `gpu-memory-utilization=0.92`。同一现场的相邻候选证据显示：

- `max-num-batched-tokens=32768` 时，PP1 的多个 rank 在 `execute_dummy_batch → all_gather` 阶段尝试申请约 4.26 GiB；日志同时记录约 4.75 GiB free、49.45 GiB allocated、49.66 GiB reserved，候选因 NPU allocator OOM 退出。
- allocated 与 reserved 很接近，因此这段日志不能单独证明“碎片化就是根因”；它能确认的是 32K/MTP dummy 的瞬时工作区候选失败，而非权重一定装不下。
- 保持 0.92、把单批 token 降到 16384 的后续候选中，PP0 日志约为 30.76 GiB 权重、3.9 GiB 峰值激活、6.06～6.07 GiB non-torch、15.35 GiB KV；PP1 约为 30.94 GiB 权重、4.2 GiB 峰值激活、6.08 GiB non-torch、14.86 GiB KV，并完成启动。
- 随后的固定 E2E 覆盖 9 组正式 case 和 9 组 Prefix 探针，18 条 `Failed Requests` 汇总均为 0。这个结果把“启动通过”提高到“目标矩阵通过”，但仍只对该模型、版本、拓扑、特性和负载指纹成立。

这类一失败一成功的相邻证据可把下一轮搜索区间收敛到已通过的 16K 与已失败的 32K 之间，并用于裁剪候选；它不等于精确确定阈值，也不能外推为所有 `TP8×PP2`、所有 A2 或其他版本都具有相同边界。若其他配置项未严格锁定，或检查点哈希、镜像 digest、完整软件栈没有记录齐，该样本只能降级为弱先验。

另一现场故障先出现非法 GM 地址/vector core 异常，随后才出现 HCCL watchdog 和进程强制退出。该链路应归为 NPU device fault，不能与上述 allocator OOM 合并统计。成功/失败证据的强度依次为：静态下界 < 启动通过 < 首请求通过 < 目标 E2E 通过 < 重复与长稳通过。

## 8. Proxy 与“直接请求 P 出现乱码”

客户端默认应访问用户配置的 Proxy/API 入口。PD 模式下，P 暴露的端口可能是内部控制、KV 传输或仅供 Proxy 使用的服务；即使它接受 HTTP，也不保证与面向客户端的 OpenAI API 行为完全相同。

直接请求 P 出现“乱码”时先区分：

- 把 SSE 流式分片、JSON 转义或 token 字节片段当成普通文本打印；
- 客户端未按响应 `Content-Type`/charset 解码；
- 请求打到了内部或二进制协议端口；
- P 与客户端/Proxy 的 API 或流式协议版本不匹配；
- P/D 使用不兼容的 tokenizer、模型资产或 vLLM/vllm-ascend 版本。

验证时记录实际端口所有者、HTTP 头和原始响应；分别用非流式请求和正确解析 SSE 的客户端对比。若 Proxy 正常而直接 P 异常，先确认 P 端口是否被承诺为公共 API；不是公共入口时，无需把直接访问修到与 Proxy 一致。若该端口本应兼容，再对比完整版本组合和启动参数，不能只归因于 vLLM 单一版本。

Proxy 验收至少包括：健康检查、实际 P/D 注册数量、连续请求成功、后端故障时的错误可解释性。仅 Proxy 进程存在不算启动成功。

## 9. 测试脚本的高风险陷阱

修改用户脚本前保留原文件副本；最终脚本保持用户既有风格，只做必要改动。以下问题会让测试“看似跑完”但结论无效：

- 参数与下一条命令误拼接，例如 `--prefix_test` 与 `sleep` 拼成一个未知参数。
- 脚本依赖相对路径，却从错误工作目录启动。
- 未使用 `set -e`/`pipefail`，某个 case 失败后仍继续，脚本最终退出码甚至为 0。
- `cmd | tee result.log` 未保留上游退出码。
- 多轮结果追加到同一文件，旧的成功记录掩盖本轮失败。
- 测试工具覆盖配置、数据集或软链接，导致后续 case 不是同一环境。
- Prefix 指标端点返回 404；这可只是“指标不可用”，不能与请求失败混为一谈。
- 仅解析最后一组输出，而遗漏前面失败的 case。

运行前做 shell 语法检查并枚举期望 case 数。运行后按本轮边界逐 case 汇总，不以最后一个进程退出或最后一行文本为依据。若脚本暂时不能修改，外层监控仍需保存管道各阶段退出状态，并显式扫描错误。

性能矩阵应覆盖目标业务区间，而不是只跑一个最佳点。常见维度包括输入长度、输出长度、并发、数据量、公共前缀比例和缓存冷/热状态；每组记录成功/失败请求数、TTFT、TPOT、E2E 和吞吐。比较优化前后必须使用相同数据与预热条件。

## 10. 日志检查与完整验收

日志扫描必须限定到本轮启动后的新内容。先保存时间戳或字节偏移，再查所有 P、D、Proxy 日志及设备侧日志。关键词只能用于发现候选，最终要读上下文；建议至少关注：

```text
ERROR | Traceback | OutOfMemory | out of memory | device page fault
EngineDeadError | ReadError | Failed to receive data | KV load failure
Aborted | timeout | rank | HCCL
```

完整验收应同时满足以下条件：

### 配置与拓扑

- 运行进程的版本、镜像、模型、P/D 并行参数和特性开关与计划一致。
- rank/world size、设备数、监听端口、P/D 注册数全部匹配且无重复实例。
- 不含残留旧进程或未知容器占卡。

### 功能与正确性

- 每个 P/D endpoint 和 Proxy 均通过对应健康探测。
- 经公共 API 完成最小请求、长输入请求及目标并发请求。
- 每个计划 case 的失败请求数为 0；响应数量、终止原因和输出 token 数合理。
- “精度正常”有可执行判据：与已确认基线做相同输入的结果比对，或使用项目认可的任务/数值指标。能生成文本不等于精度通过。
- 若启用 prefix cache、fused MC2 或 multistream，分别有生效证据；无法取得指标时明确记为 `UNVERIFIED`，不从请求成功反推。

### 稳定性与性能

- warm-up、全量矩阵和测试后一段观察期内进程均存活。
- 所有 case 都有 TTFT、TPOT、E2E、吞吐和错误数，且满足用户阈值或与基线的比较目标。
- 测试后再次检查 Proxy、P/D 健康和注册数，确认没有 worker 在压测后退出。
- 本轮增量日志没有未解释的 OOM、device fault、通信失败、engine death 或 traceback。

### 可复现交付

- 最终脚本、脱敏配置、逐节点启动说明和测试命令与实际成功运行版本一致。
- 结果日志是本轮独立文件；若覆盖固定文件名，旧文件先备份。
- 记录成功配置的版本指纹、脚本校验和、测试时间和已知非致命告警。
- 失败 case 不被成功总数掩盖；自动汇总的计数与预期 case 数一致。

只有上述四类证据同时闭环，才报告“服务正常拉起且测试通过”。如果只是服务健康而尚未验证精度，明确报告“服务可用，精度未验证”；如果指标接口不可用但请求通过，明确报告“请求通过，缓存/优化指标未验证”。

## 11. 调参与寻优的停止条件

完整层级和 quick/exhaustive 语义见 [配置驱动工作流](service-lifecycle.md#优化层级analyze--verify--quick--exhaustive)。寻优先固定一个已通过正确性验收的基线；quick 优先一次改变一个维度，exhaustive 可在显式兼容范围内比较特性交互或拓扑组合。每个可比较候选至少使用相同预热和固定测试口径，并在以下任一条件满足时停止该方向：

- 正确性、稳定性或目标长度不再满足；
- 出现可复现 OOM、device fault 或通信异常；
- 性能改善小于用户设定阈值；
- 已达到用户允许的时间、资源或试验轮数；
- 下一步需要升级软件、改变拓扑或影响其他服务，超出当前授权。

推荐将候选配置、版本、指标、失败原因和日志位置结构化保存，确保“最好结果”来自同一验收口径。不要以单次吞吐峰值覆盖正确性和稳定性门槛，也不要在生产节点上无界循环重启或压测。

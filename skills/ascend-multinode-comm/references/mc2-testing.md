# MC2 算子级预检与排障

MC2 单独验收，不能由 AllGather、AllReduce、AllToAll 或 `hccl_test -a aiv` 推导通过。普通 collective 验证的是对应集合通信路径；MC2 还可能涉及算子专用通信资源、数据重排、计算与通信流水、同步状态以及图捕获。

本指南同时用于启动前预检和既有故障定位，由当前 agent 连接用户指定的服务器、容器、工作目录和远端脚本执行，不限定 agent 品牌、机器数量或地址。不是要求用户先上传脚本、编写适配器，再开始排查。

## 1. 从实际部署识别要测的算子

先读取各节点远端入口及其依赖、匹配版本的 vLLM-Ascend 配置消费代码和可用日志，记录“启用条件 → 实际分支 → 算子/API → TP/EP 等通信组”。预检没有 worker 时记录计划分支；排障时核对实际执行分支，不能把一个开关的存在当作已执行证据。

| 算子族 | 需要区别的语义/资源 | 本库当前执行入口 |
|---|---|---|
| Matmul-AllReduce | 局部矩阵乘结果跨 rank 求和 | builtin：`matmul_all_reduce` → `torch_npu.npu_mm_all_reduce_base` |
| AllGather-Matmul | 按 rank 顺序收集输入，再与当前 rank 权重相乘 | builtin：`all_gather_matmul` → `torch_npu.npu_all_gather_base_mm`，同时校验 gather 输出 |
| Matmul-ReduceScatter | 局部矩阵乘、跨 rank 归约、按约定轴切片 | builtin：`matmul_reduce_scatter` → `torch_npu.npu_mm_reduce_scatter_base` |
| AllToAll-Matmul / Matmul-AllToAll | 通信先于计算与计算先于通信不是同一算子；排列轴、切分轴也不同 | 分别建立 adapter case，调用对应版本的真实融合 API/官方测试 |
| 分组/量化 Matmul 与 AllToAllV、AllReduce 融合 | 分组规模、不等长计数、量化 scale/offset、输出类型 | 版本化 adapter；非量化 dense case 不能替代 |
| Matmul-AllReduce-Add-RMSNorm 等后融合 | 除通信结果外，还校验残差、归一化及多个输出 | 独立 adapter case；普通 Matmul-AllReduce 不能替代 |
| MoE Dispatch/Combine | EP/TP 资源、token 路由、计数、索引、状态区与回收；并非普通 AllToAll 的换名 | 成对 adapter case，调用服务所用的真实 dispatch/combine 版本 |
| Fused MoE / MegaMoE | dispatch→专家计算→combine 的融合路径及中间状态协议 | 独立 adapter case；分开 dispatch/combine 的成功不代表整融合路径通过 |

三类 builtin 的 API 与语义依据是 Ascend 官方 op-plugin 文档：[Matmul-AllReduce](https://github.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_mm_all_reduce_base.md)、[AllGather-Matmul](https://github.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_all_gather_base_mm.md)、[Matmul-ReduceScatter](https://github.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_mm_reduce_scatter_base.md)。这些页面的产品、rank 数和组网约束各异；不能把页面中的 A2/A3 支持外推为任意 A5 跨机支持。当前镜像有同名函数，也不能证明底层内核支持该组合。

其他融合族按 [官方 MC2 算子目录](https://gitcode.com/cann/ops-transformer/tree/master/mc2) 查当前版本；例如 [Matmul-AllToAll](https://gitcode.com/cann/ops-transformer/blob/master/mc2/matmul_allto_all/docs/aclnnMatmulAlltoAll.md) 明确包含计算后的重排与通信。本文是检测分类，不是所有版本的可用性清单。

vLLM-Ascend 的 MC2/FusedMC2、prefill、通信算法与容量开关也随版本变化。可从 [v0.23.0 附加配置](https://docs.vllm.ai/projects/ascend/zh-cn/v0.23.0/user_guide/configuration/additional_config.html) 和 [同版本 token dispatcher](https://github.com/vllm-project/vllm-ascend/blob/v0.23.0/vllm_ascend/ops/fused_moe/token_dispatcher.py) 起查，再对照用户现场版本；不要照抄别的版本开关，或把 `HCCL_ALGO=level0:fullmesh` 当成这些 MC2 分支都已启用。

## 2. 测试前的版本与资源核对

agent 负责从现场收集并填写，不强迫用户先整理完整算子清单：

- 芯片/设备映射、驱动固件、CANN、torch/torch_npu、op-plugin 或自定义算子包版本、镜像标识；确认每个参与节点一致性。
- 算子 API/schema、支持的芯片/组网/rank 数、通信模式、dtype/shape、对齐/整除及图模式约束，记录来源路径或对应版本文档到 `support_ref`。
- 实际 TP/EP 组与 rank 顺序；P、D 按各自算子和组分别测，不拼成一个虚构组。多通信域融合适配器还要记录内部 EP/TP 子组，不能只写总卡数。
- 通信 handle、workspace/window 的申请方式与容量；普通 HCCL group 成功不是 MC2 专用资源已成功。文件 `/etc/hccl_rootinfo.json` 与 API rootInfo/通信上下文不等价，沿用 [拓扑文件规则](topology-and-files.md)。
- 目标容器/用户/cwd、必要环境脚本、空闲卡、测试端口与时间窗；测试不接入生产通信组，不为了拿到 handle 而复用运行中服务的资源。

缺少支持证据时保持未验证，先查目标安装包/官方匹配版本，不能随机尝试调用签名或自动切换通信算法。MC2 内核的 SHMEM/APACE/MTE 路径不是可互换实现；测试现有框架算子不需要新写一个“看起来等价”的内核。

### A3 与 A5 的检查不能合并成一张通用支持表

先读 [平台识别与分支检查](platform-a3-a5.md)。A3 检查具体型号、HCCS/实际网络与当前 API 支持的 rank 数；某个 API 的 2/4/8/16/32 等限制不能套给所有 MC2。A5 按具体 DT/PR 型号和版本查证，不由 A3 的成功案例外推。每个算子的 dtype/shape、量化、eager/图模式、通信模式与资源要求分别记录。

`HCCL_BUFFSIZE`、`HCCL_OP_EXPANSION_MODE` 仅按当前部署和匹配版本继承；不自动填值、不用增大 buffer 或切 AIV 掩盖首因。`comm_mode` 是具体 API 参数，不能当成同名或相似环境变量的等价替换。P/D 有不同环境或算子配置时分别验证。

`checks.platform` 只核实实时型号和通信组平台隔离。未知身份不执行 MC2；同组混合平台默认暂停主动测试。即使身份 PASS，仍必须审阅 `support_ref` 的真实来源，工具不会在线判断该字符串是否充分证明版本兼容。

## 3. 内置三类真实融合探针

`preflight.py` 的 `mc2_cases` 为显式测试清单。可将 [算子清单模板](../examples/mc2-cases.json) 合入本次已核实的 cluster 配置；模板不是完整集群配置，`support_ref` 未替换会在连接前拒绝。节点/容器/卡数仍来自本次 nodes/groups，模板没有固定服务器。

每个 builtin case 明确：`name`、`group`、`operator`、`runner: builtin`、`support_ref`、`execution_mode: eager`、`shape: [m,k,n]`、`dtype`、`repeats`、`rtol/atol`。设置 `require_mc2: true` 才会执行。

`m` 是每个 rank 的输入行数：AllGather-Matmul 输出行数为 `world_size*m`；Matmul-ReduceScatter 输出行数为 `m/world_size`，要求 m 整除 world_size。当前小探针限定 FP16/BF16、k≥256、k/n 为 32 倍数，另限制 CPU golden 工作量。这是工具的窄测试范围，不是算子的完整支持域。`comm_mode` 仅能显式用于已核实支持该参数的 AllGather-Matmul/Matmul-ReduceScatter；不填沿用该安装版本默认值，不自动改成 AIV。

在明确授权的控制端运行；脚本会通过现有 SSH/容器入口并发启动所有选定 rank：

```bash
python scripts/preflight.py inspect --config examples/cluster.local.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.local.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope mc2
```

`inspect` 不调用 MC2。`check` 会先执行配置中的基础阶段，平台身份核实后再顺序执行 MC2 cases；没有“普通 collective 失败后仍盲目占卡执行 MC2”的分支。case 失败后停止后续测试，未测 case 留为 UNVERIFIED。`gate --scope mc2` 要求平台身份、基础通信及全部显式 case 通过。需要缩小故障复现范围时制作最小测试配置，不删除实际服务必需项来伪造完整验收。历史 `A5_MC2_REQUEST` / `A5_ADAPTER` 协议名保持兼容，A3 也使用它们，并非 A5 专用实现。

builtin 每个 rank 的执行过程：

1. 检查真实融合 API 和可见逻辑卡 → 独立 TCPStore → HCCL 组初始化 → 获取当前组通信 handle。
2. 用固定种子生成各 rank、各轮不同的数据；先转目标 dtype，同一输入用于设备和独立 CPU FP32 golden。
3. 直接调用对应融合 API，不以 `matmul + dist.all_reduce/all_gather` 代替被测算子。
4. 设备同步后校验输出 shape/dtype、NaN/Inf、逐元素误差；AllGather 的中间输出另做精确比较。
5. 连续至少两轮，每轮更换数据，记录误差和阶段；完成组同步后才给该 rank PASS。

这不是性能 benchmark，CPU golden 也不属于算子执行语义；误差阈值需匹配服务精度要求。当前不测试权重转置/NZ、量化、bias/residual、多 tile 参数、图捕获/回放。需要这些路径时建立额外 adapter case，不把 eager 成功外推过去。

内置探针交换并记录 kernel boot_id 的摘要，拒绝所有 rank 实际来自同一宿主的情况。若容器平台虚拟化/隐藏该标识，应按宿主清单与平台证据另做适配，不伪造 host_id；该检查也不是物理线缆拓扑证明。

## 4. AllToAll、MoE 与其他 MC2 的版本化适配器

优先由 agent 在目标版本源码/安装目录中找到已有的官方或项目测试，再检查它是否真的执行目标融合算子、跨节点、逐 rank 验证结果。有测试就包装结果；没有时，agent 可在用户认可的测试目录编写与已确认 API 匹配的最小测试，并说明新增文件和占卡范围。不要停在“请用户自己提供 MC2 适配器”，也不要在无环境时声称已实现跨版本通用 MoE runner。

配置示意（字段均按本次现场替换，argv 是已审阅、已存在的远端测试入口）：

```json
{
  "name": "decode-moe-eager",
  "group": "decode-ep",
  "operator": "moe_dispatch_combine",
  "runner": "adapter",
  "node": "selected-coordinator",
  "argv": ["python3", "/approved/tests/moe_mc2_test.py"],
  "execution_mode": "eager",
  "repeats": 3,
  "support_ref": "本次确认的芯片/CANN/torch_npu/API版本及约束来源"
}
```

每个 case 只在选定发起节点启动一次协调器。它必须自己并发调度整个目标组，并实现所有远端自有 worker 的超时/回收；不能把单 rank 脚本当协调器，不能自行逃离 watchdog。配置中的 `node` 必须属于 `group`。当前工具不会自动把任意外部单 rank 适配器展开成多 rank。

协调器从环境变量 `A5_MC2_REQUEST` 读取 JSON：case、operator、group、execution_mode、repeats、ranks；ranks 包含完整的 rank/node/device 映射。该请求由控制器生成，不含密码。具体 shape/dtype/路由/子组等参数来自已审阅的适配器 argv 或其测试配置，最终必须写回 evidence。

成功时输出一行 `A5_ADAPTER ` + JSON，通用 checks 见 [适配器契约](simulation-and-gates.md)。此外 `evidence` 必须包含：

- 与请求完全匹配的 case/operator/group/execution_mode。
- 完整且不重复的 ranks；每项含请求中的 rank/node/device，并有 `status: PASS`、`synchronized: true`、`numerical_correctness: true`。
- 不少于请求次数的 repeats，以及非空 versions、parameters（含 shape/dtype、必要的 token/expert/top-k、量化和通信组信息）。

每个布尔值都来自真实测试。框架只校验结构与覆盖映射，不能证明第三方程序没有谎报，agent 仍需审阅算子调用与原始误差/运行日志。适配器 rc 非零、身份错配、少 rank、未同步或证据不足一律不能 PASS。旧式 `adapters[].stage=mc2` 保留兼容，但不能与 `mc2_cases` 混用；新任务用逐 case 方式，避免最后一个适配器覆盖前面的结果。

### AllToAll 与分组/量化融合用例

冻结每卡输入/输出切分轴、permute 顺序、发送/接收计数及计算位置。输入携带 rank/块位置差异，用独立参考实现按真实排列和通信语义计算输出；同时验证分组偏移与 count 守恒。量化路径必须包含实际 scale/offset、累加 dtype、舍入/截断和输出 dtype，不能只用 FP16 dense 输出做替代。官方支持哪些组合就测哪些，不自行编造参数签名。

### MoE Dispatch/Combine 与 Fused MoE 用例

先冻结 tokens/hidden/expert 数、top-k、EP/TP、冗余/共享专家、容量及量化模式。构造确定性 token/routing，让选定 rank 确实向远端专家发送数据，不全是本地路由。基础用例之后，按实际支持域追加不均匀路由、空专家或小 token 数、容量边界和连续重复调用。

dispatch 后核对 token 内容与归属、接收/发送计数、索引及必要状态；combine 必须使用配套 dispatch 产出的真实索引/计数，验证回到原 token 顺序及权重聚合后的输出。使用恒等专家作为最小路由测试时，明确它未覆盖真实专家计算；Fused MoE/MegaMoE 必须实际调用融合路径并校验专家计算和最终输出，不以恒等专家或独立 dispatch/combine 冒充。预填充/解码选择不同路径时分别列 case；图模式另列 capture/replay case，重复回放与清理必须有证据。

## 5. 阶段化诊断与放行

失败至少记录节点、容器、case/operator、group、rank/device、shape/dtype/mode、版本、最后成功阶段、异常码/堆栈与各对端日志：

| 失败阶段 | 优先核查 | 不能直接归因 |
|---|---|---|
| import / capability | 库/插件版本、API 存在性、芯片和签名支持 | 不直接说网络断了 |
| tcpstore / hccl_init | rank 注册、Host IP/网卡、DNS、设备通路、通信域 | SSH 成功不排除这里失败 |
| comm_handle / 首次资源申请 | 组句柄、版本 ABI、workspace/window、算子特定资源 | 不因普通 HCCL 已通过就跳过 |
| fused_launch / device_synchronize | 真正执行分支、懒建链、形状/布局、通算同步、设备日志 | 不是所有超时都等于线缆故障 |
| numerical_check / 后续轮次 | 切分/路由、量化、重复状态清理、数据一致性 | 不能把算子返回 rc=0 当正确性通过 |
| capture / replay | 生产编译/图模式、资源生命周期、输入变化 | eager 通过不证明图模式可用 |

报告 `mc2/<case-name>` 分项保留；`mc2` 只汇总显式清单，失败不被其他 case 成功覆盖。`gate --scope mc2` 要求基础通信和整个 MC2 清单都通过；`service` 还需模型请求与当前 PD/KV 路径。没有测试的算子、形状、模式或版本均不在通过范围内。

本次内置实现只完成主机侧回归，没有真实 A5/torch_npu 上板结果。模型服务是否采用了已测的那个分支，仍需现场核对。

# vLLM-Ascend 通信发生在什么时候

这里把“协议/后端”和“承载网络”分开。TCPStore 是 rendezvous/键值交换服务；Gloo 是 CPU 分布式后端；HCCL 是昇腾集合通信库；TCP、RoCE、UB/UBoE 属于不同层面的传输。某后端可以同时需要 Host 控制面与 Device 数据面，不是四选一。

## 三种部署的最小区别

| 形态 | 模型通信域 | 额外跨实例通信 | 必须追加的验收 |
|---|---|---|---|
| PD 混部，同一 engine 完成 P 和 D | TP/PP/DP/EP 按真实 rank 组 | 普通混部未必有远程 KV；开启 Mooncake 多实例复用则有 | 普通混部真实请求；远程 KV 混部还需跨实例 cache 命中/读回 |
| PD 分离 | P 域、D 域分别初始化；二者 TP/DP 可不同 | 路由/调度元数据、KV 地址/句柄交换、KV 数据传输、完成/释放通知 | 每个 P/D 路由、真实远端 KV、长短 prompt、异常回收 |
| KV 池化 | 各模型域仍需要自己的集合通信 | 存储注册/定位、块 put/get、远端内存/传输资源、命中/淘汰/释放 | 跨节点写读校验，cache hit/miss、超时回收，真实请求 |

混部不意味着“肯定没有网络 KV”。官方给出了 PD-colocated + Mooncake 多实例缓存复用场景，不能把它归成普通本地 KV。[官方混部说明](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/features/pd_colocated_mooncake_multi_instance.html)

## 按时间展开

| 阶段 | 谁与谁 | 通信/资源 | 常见失败 | 本库验证与剩余边界 |
|---|---|---|---|---|
| S0 环境准备 | 控制端→主机→容器 | SSH、docker exec；驱动设备与库、环境脚本、挂载 | SSH 通但容器地址不通；libhccl.so 找不到；陈旧拓扑；NPU 被旧进程占用 | inspect 采集网卡、路由、设备、版本、挂载；人工确认卡空闲与版本兼容 |
| S1 地址与会合 | worker→master/store/coordinator | DNS/NSS、TCPStore；Host TCP | 地址选错、反向 DNS 卡死、端口占用、world_size/rank 不匹配 | DNS 双向解析 + 有限时真实 TCPStore；输出每 rank 进入/完成点 |
| S2 CPU 组/控制面 | CPU ranks、DP 协调进程 | Gloo collective；实现中的 TCP/IPC/RPC 等 | master 可达但 peer 回连不通；GLOO_SOCKET_IFNAME 错 | 真实 Gloo AllReduce/AllGather/AllToAll；不能替代 vLLM 全部调度消息 |
| S3 NPU 通信域 | 指定 TP/EP/PP/DP 子域 | HCCL Host 建链与 Device 通道/资源申请 | 控制面 IP 错；设备拓扑/链路异常；版本/卡映射错 | HCCL init + 首次 collective + 重复校验；具体算法/尺寸需扩展 |
| S4 权重后首次执行/图捕获 | 模型各 rank | TP 聚合、PP P2P、EP 分发合并、可选 MC2 融合 | 懒建链、图模式约束、显存/通信 buffer 不足、MC2 特殊路径 | 小 collective + 官方打流；MC2 另做逐算子测试，内置三类 eager 小探针；MoE/量化/图模式及 PP P2P 仍需对应测试 |
| S5 PD/KV 资源初始化 | P、D 或计算/存储节点 | connector 元数据、端点注册、内存注册、会话建链 | DP/TP 描述不符、endpoint 对错、端口动态分配、内存注册失败 | TCP 只验证基础可达；必须执行对应 connector 初始化适配器 |
| S6 首请求与 KV 搬运 | 请求入口→P→D；或计算→存储 | HTTP/路由控制面 + KV 数据面 + 完成/释放 | health=200 但实际400/500；EAGAIN、部分 KV 读失败、长 prompt 超限 | 真实请求/远程 KV 校验；小 TCP 或 HCCL 成功不替代这一层 |
| S7 稳态、重试与回收 | 多实例/多客户端并发 | 心跳、复用、超时、重连、池化淘汰 | 仅单请求成功；多卡并发泄漏；异常后资源未释放 | 版本化适配器/压测；本库默认不执行破坏性故障注入 |

这是用于排障的逻辑顺序，不声称每个版本的源码严格串行执行。HCCL/connector 可能首次使用才创建部分链路；因此日志中的 init 成功不是最后一关。分离模式的元数据和数据搬运机制取决于 connector。[官方分离设计](https://docs.vllm.ai/projects/ascend/en/v0.23.0/developer_guide/Design_Documents/disaggregated_prefill.html)

池化必须额外观察存储访问，不能拿 P→D allreduce 当作 KV 读回证据。[官方池化设计](https://docs.vllm.ai/projects/ascend/en/v0.23.0/developer_guide/Design_Documents/KV_Cache_Pool_Guide.html)

MC2 的资源初始化可能出现在 S3，实际融合算子、懒建链及图捕获多在 S4 或后续请求首次触发。应按真实执行分支区分 Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter、AllToAll 融合、MoE Dispatch/Combine 与 Fused MoE，不能用一个 AllReduce 结果覆盖它们。[逐算子检测与诊断](mc2-testing.md)

## 端口和环境变量必须从部署清单取得

逐项记录：请求入口 HTTP，实例 API，DP 地址/RPC，store/master，HCCL Host 建链范围，connector 元数据/监听端口，池化主控/存储服务，动态会话端口。每项都写明监听方、发起方、IP、进程/容器、配置来源，不提供“放开一个固定区间就够了”的通用答案。

HCCL_IF_IP 指向 Host 建链选择，不是 NPU RoCE IP。其优先级高于 HCCL_SOCKET_IFNAME，遗留值可使只修改网卡名无效。[HCCL_IF_IP](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850/commlib/hcclug/hcclug_000089.html)

HCCL_SOCKET_IFNAME 有前缀匹配和精确匹配语义；工具按实际网卡使用精确选择，Gloo 则使用网卡名。[HCCL_SOCKET_IFNAME](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850alpha002/hccl/hcclug/hcclug_000093.html)

HCCL_HOST_SOCKET_PORT_RANGE 可覆盖 HCCL_IF_BASE_PORT 的选择；端口数量、MC2 支持约束需对照现场 CANN 版本，不把老版本的 16/32 端口经验硬编码。[端口范围说明](https://www.hiascend.com/document/detail/en/canncommercial/850/commlib/hcclug/hcclug_000091.html)

## 分离/池化的配置落点

把 mode 改为 disaggregated，groups 分别列 P 和 D；每个组按照实际成员选定节点/逻辑卡。当前工具的 devices 是节点级列表，若同一节点不同通信域使用不同卡，分别做配置文件运行，避免错误复用。分离需要 kv_transfer 适配器；池化 mode=pooled 需要 kv_pool；混部启用远程 KV 时设置 require_kv_transfer=true。需要融合通算则设置 require_mc2=true，并通过 mc2_cases 列出各算子/通信组/模式，避免一个泛化 MC2 PASS 掩盖未覆盖路径。

P/D 的 dp_size、tp_size、world_size、data-parallel-start-rank、engine_id、模型名、KV layout/block size/dtype、PYTHONHASHSEED 等必须在真实部署清单校验。工具尚未自动解析任意 launch shell 或 vLLM CLI，不能把手写 groups 当成已经核对生产 rank 计划。

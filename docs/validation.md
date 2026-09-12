# 验证记录与已知限制

初始记录日期：2026-09-10。开发环境：Windows，Python 标准库；初始开发未执行真实服务器远端通信。后续真实 SSH/宿主机检查的有限验证范围见文末 2026-09-11 修订，不能外推为 NPU/服务通过。

初版本地 26 项 unittest 全部通过：自动地址选择及歧义、卡/端口/SSH 配置校验、CPU-only 存储节点分组、RoCE 能力证据、fullmesh 不误判为物理拓扑、未验证/过期证据拒绝放行、端口失败不发探针、真实回环 TCP 8/4096/65536 字节摘要、真实 DNS 调用、连接拒绝、命令与 worker 超时、缺失适配器证据拒绝、stdin 远端探针/子进程加载、两种 MPI hostfile、跨平台 Linux 命令生成。

skill-creator 的 quick_validate.py 校验通过，SKILL.md 及相对资源结构有效。

开发中修复了 Windows stdin 编码使中文探针无法加载的问题；DNS 用例保留系统真实结果，不假定任意机器的 localhost 正反解析一定对称。

尚未实测：Linux docker 全链路、CPU TCPStore/Gloo、A5 HCCL 的完整 collective/64 卡对矩阵、hccl_test/CANN 9.2.0、HCCL-VM、MC2、真实 PD/KV 池化适配器及通用光模块/UB/RoCE 物理拓扑。2026-09-12 已完成的有限双节点 A5 HCCL Broadcast 见文末专项记录，不能外推到这些缺口。

GitHub Actions 的 Linux/Windows × Python 3.10/3.12 标准库测试已通过，[运行记录](https://github.com/Tflowers-0129/ascend-multinode-comm-skills/actions/runs/34459676895) 对应实现提交 8d63150。CI 不包含 NPU 测试。

## 初版覆盖缺口

- 仅 IPv4；物理/逻辑卡映射人工确认。没有自动从任意服务 launch shell 解析所有 TP/EP/PP/DP 域。
- 拓扑自动发现到设备/配置证据；物理 fullmesh 的还原需要平台邻接表与版本适配。
- 默认小 collective，未覆盖 PP P2P、全部 dtype/消息尺寸和图捕获；后续 MC2 算子扩展见下文，仍未上板验收。
- KV 与复杂 MC2 路径提供验收接口与中文实施步骤，尚无跨现场版本的通用内置实现；缺失时严格未验证。
- hccl_test 退出零只表示程序完成，校验表需要匹配版本解释，包装器不会伪报正确性 PASS。
- 工具不自动消除用户服务占卡、版本冲突、代理和拓扑挂载警告；这些要在现场验收流程逐项确认。

## 新增：部署脚本静态审计

2026-09-10 新增 28 项回归用例，总计 54 项本地 unittest 通过。覆盖 source 与物理行号、变量引号/导出/生效顺序、命令替换不执行、source 循环/越界、条件候选降级、exit 控制流、容器环境边界、PD 独立通信域、DP 区间重叠、节点/namespace 端口对照、KV 布局及异常类型、torchrun 启动器与业务参数边界、敏感字段脱敏、报告转义，以及 CLI 退出码和不覆盖旧文件。

自带 audit-demo 是故意构造的静态故障数据，不是部署模板：检出 1 个确定错误和 4 个条件风险，CLI 返回 1。没有确定错误时仍返回 2 / UNVERIFIED，不能用于证明真实服务可以建链。此扩展没有执行任何上传脚本或远端 A5 操作。

自动解析仅覆盖文档列出的顺序 shell 子集。Python/Compose/K8s、动态 Bash/SSH/容器包装及具体版本的参数消费方，必须由技能继续做只读语义分析；没有完整 Bash AST 或任意部署文件的全自动正确性证明。GitHub Actions 会对本次提交运行相同跨平台测试矩阵，状态以对应提交的运行记录为准。

## 新增：服务器直连审计指引

技能增加了独立的服务器直连只读入口、连接信息表和填写模板，覆盖 IP/端口/用户名、密码或密钥认证、跳板机、脚本路径、容器和读取范围；密码通过实际可用的安全渠道输入，不加入配置。文档明确既有 SSH 工具的适用范围、认证受阻时的处理，以及现场证据到静态分析的来源映射。

本次是技能与文档更新，没有新增自动 SSH/密码采集程序，也没有连接真实服务器。技能格式、相对资源链接和既有 54 项 unittest 均通过本地检查。认证、远端读取及现场审计流程尚未做真实 SSH/容器端到端验收；单元测试通过不覆盖这些运行能力。

## 历史修订：现场故障入口（优先级已由下文更新）

此前将任意指定节点列表的“容器 + 工作目录 + 远端部署脚本 + 当前建链失败”设为默认入口，不要求上传/本地 manifest。该排障流程围绕现有 worker 和日志的失败阶段，交叉核对实际网络/设备状态，再选择最小受控验证；本地静态工具降为辅助。明确两节点卡对隔离不限制现场总节点数。当前入口以以下“预检与排障并列、agent 无关”的修订为准。

探针新增可选 workdir 和 container_user：源环境初始化和探针在指定目录/容器用户下执行；拒绝相对/非 Linux 目录、非法容器用户及 local 静默忽略容器的组合。新增 6 项命令生成与配置回归测试，总计 60 项本地 unittest 通过。这些测试验证参数生效顺序、转义及拒绝行为，未执行真实 SSH/Docker/NPU；现场故障闭环仍需真实环境验证。

## 修订：示例与打流目标参数化

移除当前文档、示例及测试数据中的现场主机身份、网段和环境脚本默认值。HCCL 指南按本次节点清单生成 hostfile，总 rank 来自 slots 总和；包装器要求显式提供测试目录及 source/继承环境选项，大流量档位使用 large-1g（保留旧名称兼容，不绑定主机）。集群示例改用 SSH 占位符，未替换时在任何远端调用前拒绝。

新增 5 项回归，总计 65 项本地 unittest 通过，验证任意命名的多个节点完整进入计划、rank 求和、环境/目录/目标必填、继承环境不 source、旧档位兼容及占位符阻止连接。仅生成计划和运行本地测试，没有进行真实 SSH/MPI/NPU 打流；文档参数化不代表远端版本已验收。

## 修订：预检与排障并列、agent 无关

README、技能描述、场景路由和远端连接模板均提供“启动前通信预检”与“当前建链失败排障”两个并列入口，不预设服务已有故障。补充预检请求示例，明确没有 worker/故障日志、业务端口尚未监听不是启动前的通信失败证据；计划配置、现场观察与探针实测分开记录，不为预检自动启动完整模型服务。

使用说明改为按所用 agent 的技能加载、文件读取、SSH/服务器连接和命令执行能力接入，Codex 安装路径仅保留为可选示例，CLI 可独立运行。没有远端访问能力时明确报告限制，不把本地分析冒充现场检查。

本次仅修改技能与文档，不改变探针代码。本地既有 65 项 unittest、技能格式和相对资源链接检查通过；人工核对两条入口的前提、授权范围与未测项处理。没有执行实际 agent 产品接入测试、SSH/容器端到端测试或 NPU 打流，不能据此宣称任意 agent 已兼容或现场已验收。

## 新增：MC2 逐算子探针与验收

不再只有泛化 MC2 适配器接口。preflight.py 新增三类真实 torch_npu 融合 API 调用入口：Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter，范围限定为版本支持的非量化 eager 小形状。MC2 case 独立生成通信组，记录 capability、TCPStore、HCCL 初始化、通信 handle、融合调用、设备同步、逐元素校验与重复执行阶段。普通 collective 不能代替被测融合调用。

mc2_cases 支持逐算子 builtin 或远端协调器 adapter；AllToAll 融合、MoE Dispatch/Combine、Fused MoE/MegaMoE、量化和图模式有明确的版本化测试指引，但本次没有伪造通用实现。每个 case 单独保留结果，新增 mc2 放行范围；case 失败后停止并保留后续未测项，不能被另一 case 的 PASS 覆盖。

新增 18 项主机侧回归，总计 83 项本地 unittest 通过。覆盖显式执行授权开关、支持来源占位符拒绝、形状/整除/工作量/容差校验、输入随 rank/轮次变化、CPU 数学替身验证 gather/归约/切分语义、真实融合 API 调用选择（mock）、NaN/Inf/输出错配拒绝、完整多节点 rank 编排、同宿主拒绝、适配器 request/逐 rank 证据与非零退出、阶段异常、逐 case 不误放行及 service 仍需模型请求。技能格式与相对链接亦检查通过。

没有安装或运行真实 torch/torch_npu，也没有连接真实 SSH/Docker/NPU。数学替身与 mock 仅验证主机侧逻辑，不等于 PyTorch CPU 集成测试或 A5 硬件通过。公开 API 文档的 A2/A3 支持不能外推为所有 A5/CANN 组合；必须由 agent 核实目标版本并现场验收，报告继续列明未测项。

## 新增：A3/A5 分支与成功案例提炼

新增平台指南和远端成功样本指引；A3 的 vNIC、superpod/SDID、HCCS 验证与 A5 的 UB/URMA/HiXLEP 路径分别核实。成功案例优先按平台、模式、组网、MC2/版本差异选择，不要求先攒够脚本数；原始脚本/日志不自动入库，进程存活不冒充真实请求或融合算子成功。

preflight.py 增加可选 platform 声明、实时型号保守匹配、节点/通信组身份报告、A3 物理设备的 vNIC/netdetect/gateway 只读采集，以及显式 HCCL_BUFFSIZE/HCCL_OP_EXPANSION_MODE 环境记录。身份冲突或同组混合平台暂停主动探针；未知身份不执行 MC2，不通过 mc2/service gate。不同 P/D 组可分别记录平台，但跨平台 connector 支持没有因此获得认证。历史 A5 协议前缀保持兼容。

新增 15 项合成证据/故障注入回归，总计 98 项本地 unittest 通过。覆盖实时型号、A2/A3 架构歧义、声明/错误输出不能当证据、身份矛盾、HCCS 候选不是传输/拓扑证明、平台查询分支、任意节点/卡列表和 CPU-only 排除、组内混合阻止、P/D 分组、pairs 防绕过、未知身份不执行 MC2/不放行服务，以及显式环境不成为平台默认值。技能格式和相对资源链接另做检查。

这是流程、工具主机侧逻辑和文档的扩展。未连接 A3/A5 服务器，未执行真实 npu-smi/hccn/HCCS、SSH/容器、torch_npu/HCCL/MC2 或 KV 验收；未知型号格式、厂商完整邻接表解析和实际平台/版本支持仍需现场验证。不能把这 98 项标准库测试称为硬件通过。

## 新增：官方部署配方基线

2026-09-10 查询官方最新正式版 v0.23.0，解析 tag 到源码提交 `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`，读取官方仓库文件与相应部署片段。新增 7 类主要配方索引，覆盖 A3 多节点、950DT 混部、A3/A5 PD 分离、Mooncake/Memcache 池化；另标注 A2 混部和旧 EP 参考。区分嵌入文档的脚本与真实独立文件，保留相互配套的 launcher、模板、proxy 和存储配置。

来源审阅发现并记录：通用 DP 模板不含 PD connector；旧 EP 模板参数顺序/分支不同；KV Pool 正文仍有 main 依赖、固定 Memcache 模板只列 A2/A3；Layerwise proxy 需要回连；部分配方有日志删除、hugepages 修改或通信检查开关。以上作为有适用条件的审计提示，不自动执行或修复上游脚本。

本次仅更新技能/文档，未改探针实现；既有 98 项 unittest、技能格式、相对链接和固定源码路径/行号检查通过。没有下载后执行部署脚本、安装依赖、连接现场或运行 NPU/模型；官方参考与现场成功证据严格区分。

## 新增：单机/双机/多机场景示例

增加六类中文示例，包含节点与 DP/TP 布局、固定官方脚本定位、原生/外部 DP 参数区别、角色模板和代理的配套关系，以及可复用的现场预检/排障请求。新增单机混部、单机 PD 和 A3 双机 PD 的官方配方索引；四机多 DP 是扩容算例，未冒充官方已验证模型配置。

技能入口支持选择单机场景，但没有修改运行时代码：preflight.py 配置仍要求 2～64 节点，内置 MC2 仍要求真实跨宿主证据，groups 也没有自动展开一机多个 TP 子域。单机现场检查、算子探针缺口和未验证状态已显式说明，不通过虚构节点绕过限制。

本次既有 98 项 unittest、技能格式与 86 个相对资源链接检查通过；另核对新增固定源码路径及章节行号。未连接服务器、执行部署片段或启动模型，没有新增单机 CLI 一键检测、SSH/容器或 NPU 的实测结果。

## 修订：普通密码登录与按需配置免密

2026-09-11，根据实际使用反馈简化认证流程：普通 SSH 密码登录不再要求预先准备密钥或独立核对首次指纹，支持交互终端/PTY 密码提示及首次连接信任，仍拒绝已变化的主机密钥。用户明确要求免密时，登录后生成本地专用密钥，仅向指定账号追加公钥，保留原配置并逐节点验证。仅宿主机网络检查不要求提供容器/部署脚本。

在用户本次授权的三台 Linux 宿主上，实际验证了 Windows OpenSSH 密码无回显交互、首次主机记录、公钥追加，以及逐节点 BatchMode 密钥登录。随后在宿主间完成六个方向的低频 ICMP、1500 字节不分片 ICMP 和既有 SSH 端口 TCP 建连检查；没有进入容器、修改网络、创建额外监听器或运行 NPU 测试。它不证明任意 agent 的交互兼容性，也不证明 TCPStore/Gloo/HCCL/MC2/KV 或服务启动通过。

本次只修改技能与指引，未修改 preflight.py：其批量 SSH 仍使用 BatchMode，不接收密码字段。既有 98 项 unittest、技能格式与 87 个相对资源链接检查通过；现场地址、密钥、密码、采集结果及报告不进入仓库。

## 新增：宿主机 HCCS 组网与设备小包检测

2026-09-11，新增独立 `fabric_probe.py` 和中文检测指引。工具自动解析已知格式的 npu-smi 映射，按真实 card/chip/physical ID 查询 vNIC、Pod/SDID、HCCS 和 RoCE/UB 证据；支持显式 SSH 密钥路径。默认只采集或生成明确设备对的计划，`--execute` 才做有界 HCCS ping，不 source、不进入容器、不创建计算 rank。

加入对端地址归属规则：同 Pod 的唯一对端与跨域地址重叠分开；目标与本机重复时不发可能误归属的探测；跨 Pod 成功响应不自动确认目标身份。按真实收发统计、失败文本、L1 plane 结果和超时判断，避免将 rc=0 与成功页脚误报为通信通过；同编号覆盖不冒充全卡对，设备小包不替代 HCCL/MC2/KV 或服务。

本地共 129 项 unittest：Windows 上 128 项通过、1 项 Linux SIGALRM 总预算测试跳过；新增 31 项覆盖未知/非连续/重复映射、动态查询、地址解析与重叠、Pod 0、RoCE DOWN 不否定 HCCS、图例不作拓扑、命令超时、参数/凭据拒绝、只采集不发包、双向执行、目标身份变化、SSH banner 和输出不覆盖。技能格式、99 个相对链接检查及 diff 空白检查通过。

先前获准的现场手工流程已完成三台宿主的真实 npu-smi/hccn 查询与有限 HCCS ping，观察到同域双向可达、另一分域发送失败而机内对照可达，以及 rc=0 仍报告丢包的情况。本次把该方法实现为工具，并用本地保留的 48 设备/64 条主覆盖探测原文离线回放：48 项 PASS、16 项 FAIL，地址归属检查符合证据。**离线回放不是新工具重新连接硬件的端到端验收**；本次仅更新仓库，未重连历史服务器。

未覆盖任意驱动版本输出、跨机物理邻接自动还原、通用 UBoE 判型/打流、RoCE 带宽测试、NPU collective/MC2/KV/模型服务。真实地址、账号密码、密钥与原始现场证据留在本地忽略目录，发布用例只含合成节点与文档地址。

## 2026-09-12：MPICH/Hydra 官方 HCCL Test 双节点实测

在用户授权的两台 Ascend950DT 宿主上，以共享 MPICH/Hydra 4.1.3 从节点 A 启动节点 A/B 各一个 rank，运行 CANN 9.1.0 随附的官方 broadcast_test。两端 CANN 的 libhccl.so 与 libhcomm.so 哈希一致；由于本地安装的测试二进制构建哈希不同，本次将节点 A 的官方二进制复制到共享路径，两端运行时 SHA-256 完全一致。rank 包装器分别从同名数据网卡取得各自 HCCL_IF_IP，并用 HCCL_SOCKET_IFNAME 精确匹配；hostfile、账号、地址与原始日志未进入仓库。

健康基线选择两端空闲且目标物理边为 UP 的设备 0，设置 level0:fullmesh，Broadcast root=rank 0，覆盖 8 KiB 到 1 MiB、2 次 warmup、5 次计时并开启正确性校验。8 个尺寸全部返回 success；1 MiB 的 alg_bandwidth 为 31.89177 GB/s，进程退出码为 0。

故障隔离选择两端空闲但同一目标物理边已确认 DOWN 的设备 7，只运行 8 KiB、0 次 warmup、1 次迭代。两个 rank 都输出 HCCL_RANK_READY 和测试参数，90 秒内没有首条尺寸结果，外层 watchdog 返回 124；Hydra 随后清理所有本次 rank，设备利用率恢复为 0，目标链路仍为 DOWN。这证明 MPI 远端拉起和测试程序入口可达，阻塞发生在首个普通 HCCL Broadcast 执行阶段。结合相同设备边的 link/port_info 证据，它支持物理链路故障诊断，但不等于直接执行或验证了 MoeDistributeDispatch/MC2 kernel。

首次 MPI launcher 探测曾把 hostfile 发起节点写为 localhost 且未指定 Hydra 回连接口，远端 proxy 因尝试连接自身 localhost 而失败；这次失败发生在 HCCL/NPU 之前，不计入通信结论。正式流程改为真实主机地址加 -iface，并将“先做 mpirun hostname、逐 rank 设置本机 HCCL IP、健康/故障对照和外层超时”总结进技能。现场脚本、hostfile、地址和日志不进入仓库。

这是单一 CANN/驱动/硬件组合的有限现场证据。它没有验证其他 dtype、AIV/CCU 执行器、其他 collective、全部卡对、故障恢复、容器内路径或真实 vLLM MoE 请求；普通 HCCL PASS 也不能填写 MC2 PASS。

本次更新后本地 unittest 共发现 132 项：Windows 上 131 项通过、1 项 Linux SIGALRM 总预算测试跳过；新增用例覆盖 Broadcast root/每节点卡数参数、loopback/大小写重复主机拒绝，以及 root 只约束 Broadcast。上述现场过程已完成 8 KiB 健康卡正确性验证；skill-creator quick_validate 通过。

## 上板验收建议

在本次真实通信域中先选一个最小跨节点空闲卡子集，在目标容器中运行 inspect/check，核对平台、IP/卡映射/版本与日志；具体子集必须满足当前算子的 rank/组网约束，不机械要求所有 MC2 两张卡就能运行。随后按当前卡列表扩展卡对覆盖和真实模型域，再接入实际 connector 与各 MC2 测试。每一步保存报告，发现异常先缩小范围再加大负载，不固定节点身份或每机卡数。

## 新增：A5 FullMesh channel acquire 卡住证据链

2026-09-12，根据一次已授权的双节点 A5 MoE 现场排障补充脱敏方法。新流程区分 `Entry-HcclChannelAcquire channelNum[N]` 与版本对应的 acquire completion，要求按同一 run/communicator 收齐全部 rank，再把缺失 peer/EID 边反查到容器逻辑卡、物理 NPU/chip、UDie 和双端端口。文档同时说明静态 topology 生成 rootinfo 不验证实时物理链路、两个 DOWN endpoint 可能属于同一条链路，以及部分 rank idle/其余 rank launch-and-wait 只是旁证。

MC2 指南新增 V2/V4 分层说明，补充固定提交的 op-plugin/kernel/tiling 实现链接和 master 文档导航：vLLM-Ascend 调用 torch_npu V2 接口，op-plugin 可从 V2 适配入口选择 `aclnnMoeDistributeDispatchV4`，CANN 的 V2 算子目录包含 V4 host API 与 arch35 FullMesh kernel/tiling 实现。`fullmesh_v2` 是 `commAlg` 模板选项，不等于 aclnn V4。公开源码存在只证明对应版本的实现与支持声明，现场仍需核对配套 tag、安装二进制、参数约束和实时链路。

本次只更新技能文档，不重连历史 NPU 或重跑模型。Windows 使用随附 Python 运行 129 项 unittest，其中 128 项通过、1 项 Linux SIGALRM 测试按预期跳过；`git diff --check` 无空白错误。skill-creator 的 `quick_validate.py` 因本地运行时没有 PyYAML，改在已有 PyYAML 的授权 Linux 临时仓库副本上执行并通过；没有为校验安装系统依赖。

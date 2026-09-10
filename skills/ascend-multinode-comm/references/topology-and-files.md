# RoCE、UB/UBoE、fullmesh 与容器拓扑文件

## 不要做错误的三选一

| 维度 | 候选值 | 有效证据 | 不能作证的现象 |
|---|---|---|---|
| Host 控制面 | TCP/IP 网卡、源 IP、路由 | 当前 namespace 的 ip/route、源地址绑定握手 | SSH 管理地址可登录 |
| 设备传输能力 | RoCE、UB、UBoE，可能混合 | RDMA link layer、URMA 端口状态、hccn/HiXLEP、实际数据通道日志 | 仅安装某个工具/有一个设备文件 |
| 物理组网 | 交换网络、直连、层级网络、fullmesh 等 | 每卡/端口对端邻接表、交换机端口、链路状态与路由 | 一个 AllReduce 成功 |
| 通信算法 | level0:fullmesh 等 | 进程实际环境和 HCCL 算法选择日志 | 脚本有 export，但运行进程未继承 |

`export HCCL_ALGO=level0:fullmesh` 是现场配方的一部分；工具可以按同一配置运行，但不会用这行设置宣布“物理网络已经探测为 fullmesh”。

## 探测步骤与输出

以下是主动预检的步骤：先核实计划运行环境，审阅必要的环境初始化脚本，再执行 inspect；check/pairs 须确认空闲资源与对应测试授权。排查现有建链故障时，先按 [现场流程](remote-server-audit.md) 直接读取原 worker/容器与网络设备状态，保留原环境；不要为了取证先 source 环境或覆盖测试网卡，只有需要受控复现时才选用上述探针。启动前预检不要求存在故障 worker 或日志。

1. 宿主机和容器分别运行 inspect，比较地址、路由、设备可见性、拓扑文件 hash/挂载来源、CANN/torch 版本。容器 bridge/NAT 与 host 网络需区分。
2. 从 UP 网卡选业务 IP，记录网卡名和 MTU；有多块可行网卡则要求 CIDR 或明确 IP。到每个 peer 的实际源路由与反向路径仍需核对，不能只验证 master。
3. 查询 npu-smi mapping。用户提供 physical_devices 时采集每张物理卡的 hccn IP、link、net_health、LLDP；没有映射不盲目对编号 0~7 做配置操作。
4. 采集 rdma link、sysfs RDMA 端口 link_layer/state/gid type、urma_admin show、UB 设备线索。工具分别列 capability candidates 和 active transport；后者没有实际通道证据保持 UNVERIFIED。
5. 由 LLDP/HiXLEP/平台管理输出构造卡/端口邻接表，区分 PEER2PEER 和 PEER2NET；检查每端口有且仅有正确对端、两侧对称、状态 UP。需要证明物理 fullmesh 时，验证所声明层级的所有必要边，不能只看跨服务器两张卡。
6. 跑两机逐卡矩阵，再跑多机 collective 大小梯度。逻辑通路可能经过交换机或中间节点；这两个测试不能替代物理线缆拓扑证据。

目前工具自动收集证据并给出 RoCE/UB 候选，不对厂商/版本各异的 LLDP、URMA 输出强行套一个未知 schema。缺少完整邻接表时物理拓扑明确标为 UNVERIFIED，交给技能按原始证据判断。这比误识别成 fullmesh 更可靠，但也意味着此初版尚未实现所有 A5 版本的全自动物理拓扑还原。

设备 IP 检测是另一层：在版本和 RoCE 场景支持时，可用 `hccn_tool -i 0 -ping -g address 对端设备IP`；Host ping 和 Device ping 不互相替代。UB-only 没有可用的 hccn IP 时不能一律报 RoCE 故障。[官方互联预检](https://docs.vllm.ai/projects/ascend/en/v0.23.0/installation.html)

## 三个容易混淆的路径

| 路径/名词 | 处理原则 |
|---|---|
| `/etc/hccl_rootinfo.json` | 是否需要由具体 CANN/HCCL/HiXLEP/池化运行路径决定，不是所有容器通用必需项 |
| HcclRootInfo API 对象 | 用于 communicator 初始化的 API 数据对象，不等价于这个固定路径的 JSON 文件 |
| `/etc/hixlep.json` | 用户现场可能存在的单文件；本库检查存在性、JSON 类型、顶层键和摘要，不假定它是官方通用入口 |
| `/etc/hixlep/` | 当前部分官方 950DT 样例使用的目录入口，应与 connector 的 ascend_local_comm_res_path 对应 |
| `/lib/route.conf` | 与某些 UB/HiXLEP 部署一起生成/消费的路由配置；不要只复制 rootinfo 而漏掉配套资源 |

### 对 rootinfo 的默认建议

本库不在容器模板中主动注入 `/etc/hccl_rootinfo.json`，也不因它缺失就把普通通信测试判失败。对用户当前无依赖的 fullmesh 路径，建议移除多余 bind mount，备份后清理陈旧文件。

但不能写成“一切 A5 容器都应该删除它”：官方安装预检和部分 950DT 模型/池化部署存在需要该文件的路径。这是版本/场景差异，须先确认真正消费者。[950DT 模型部署例](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/GLM5.html)

执行移除前的核对清单：

- 记录镜像 digest、CANN/HDK、vLLM-Ascend、connector 类型与对应文档版本。
- 在启动脚本/指定源码目录搜索 `hccl_rootinfo`、`hixlep`、`ascend_local_comm_res_path`，确认读取链路；不要搜索整个服务器敏感目录。
- 通过 Docker inspect 的 Mounts 与容器 mountinfo 判断是否是 bind mount、来源是否跨机器误拷贝。已挂载文件在容器内直接删可能失败，或误改宿主数据；先修改启动配置再重建目标容器。
- 需要消除旧配置时先备份到明确的本地目录，记录摘要；仅在维护窗口和用户授权后移除挂载。工具本身不执行删除/重建。
- 在无 rootinfo 的隔离测试容器里用相同镜像重复 HCCL、KV/池化验收；如版本明确需要，则按官方生成方式恢复匹配本机拓扑的文件，不复用别机文件。

官方 HiXLEP 生成指南由安装文档链接到 [A5 LocalCommRes 配置指南](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md)。本次该页面抓取超时，因此没有编造文件格式或生成参数；现场应读取和镜像匹配的版本。池化还区分设备间与主机/设备资源路径，不要用一个命名相似文件替代整套拓扑。

## 不自动“修复”的项目

不关闭 TLS、不关闭自定义算子安全校验、不改路由/防火墙、不改系统 DNS、不设置特权容器、不自动删除 rootinfo/hixlep。即使某官方排障页给出此类操作，也必须在明确故障原因、变更窗口和授权下单独处理。

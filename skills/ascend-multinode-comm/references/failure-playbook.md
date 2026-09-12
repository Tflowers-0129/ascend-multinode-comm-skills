# 从历史记录提炼的坑位

本表来自用户授权读取的相关任务中可见的排障摘要；不是全部 ChatGPT 项目都已遍历，也不是当前服务器状态。这里只保留工程结论，不复制密码、原始日志或完整对话。

| 现象/证据 | 应检查什么 | 预检如何前移 |
|---|---|---|
| 参与节点的多条 TCP 连接均 ESTABLISHED，rank0 栈停在 getnameinfo / NSS DNS，其他 rank 在 TCPStore 等待 | 容器内反向 DNS、hosts、NSS；不能直接判成端口没通 | getnameinfo 独立子进程硬超时，正反解析一致性，真实 TCPStore 注册/读写 |
| 修复容器 hosts 后 store 继续初始化 | 检查业务地址与别名是否精确对应，必要时用容器 --add-host | 不自动修改系统 DNS，不拿 ping 通当解析正常 |
| 管理 IP 与业务 IP 来自不同网段，地址尾号并不对应 | 管理网与业务网分别核查，不按节点标签拼业务地址 | 自动发现 UP 网卡 + 现场 CIDR，拒绝多解 |
| 请求全失败但服务端没对应记录 | 代理环境、no_proxy、入口 URL、请求是否到了正确机器 | 仅记录代理是否存在而不泄露代理凭据；HTTP 验收显式绕过代理并核对路由 |
| Mooncake 部分会话成功，另一些 EAGAIN/recv 超时 | 对照 session、rank、两端日志、元数据内容和动态端口，不直接下结论“整个网不通” | 双向数据校验 + 真实 connector/大小梯度；静态端口探针不冒充动态会话 |
| 宿主机有 launcher，容器里找不到 | 挂载路径、入口 cwd、source 环境、文件权限 | 在目标 namespace 执行预检；env_scripts 路径必须是容器内实际路径 |
| 8000 与 8001 对错，或 8000 已被其他管理进程占用 | 绑定地址、端口归属、配置来源 | 先 bind 成功再发 ready；失败时不向已有服务发送自定义探针 |
| 老容器/孤儿 worker 或 DP coordinator 仍在 | 卡占用、进程所属容器和作业；不得直接 killall | npu-smi/ss 只读采集，用户确认空闲后再占卡测试 |
| 非交互 docker exec 找不到 libhccl.so | .bashrc 未生效、CANN 环境/挂载/版本 | 显式 source 用户指定环境文件；不依赖交互终端的隐式状态 |
| health=200 但真实模型请求返回400/代理吞错，KV 长时间占用 | 模型名、max-model-len、TP/DP 元数据、请求路由、错误回传和释放路径 | 长短请求、各角色路由、KV 释放的真实验收，不只 curl /health |
| A5 FullMesh/MoE 中部分 rank 只有 `Entry-HcclChannelAcquire channelNum[N]`，其余 rank 已完成 acquire 并在 kernel 中等待 | 先排除日志截断，再按同一 run/communicator 建立 rank→peer/EID→acquire 完成状态矩阵；将缺失边反查到双端物理设备、UDie 和端口 | `Entry` 和 `channelNum[N]` 不等于 N 条通道成功；静态 rootinfo 可生成也不证明端口实时 UP |
| 一个节点时钟异常 | 证书、超时、日志对齐的影响；不要没有证据就当根因 | 记录节点时钟/偏移线索，不擅自改系统时间 |

## 最小排障决策

现有服务故障从 [服务器现场流程](remote-server-audit.md) 进入，根据真实失败阶段选择以下分支；诊断不等于已获准修改配置或占卡测试。

1. SSH/容器/已有日志中的 source 失败：先定位环境边界并给出修复建议，不继续占 NPU，也不为读取配置而执行 source。
2. 没有唯一业务 IP：提供业务 CIDR 或实际 IP，不选“第一张网卡”。
3. TCP bind 失败：端口已占用或地址不存在，保留占用者，换维护窗口或空闲端口。
4. TCP connect 成功、数据校验失败：看源地址、回程、连接被谁接受、报文完整性；不能直接算建链成功。
5. DNS/TCPStore 失败：看每 rank 的 tcpstore_enter/ready 与解析耗时；已有连接均 ESTABLISHED 不是排除 DNS 的证据。
6. Gloo 失败、store 成功：核对 peer 互联、动态回连、CPU 网卡和分组，不只检查 master。
7. HCCL 初始化/首算子失败：拆 Host 控制面、Device 链路/拓扑、卡映射、版本、资源与算法；FullMesh acquire 卡住时按下节先找缺失 peer/EID 边，再用 pairs 或版本匹配的最小复现缩小到卡对。
8. 小 HCCL 成功、大流量失败：加大小/次数，核对内存、链路错误与拥塞；小 TCP 不提供 RDMA MTU 或带宽保证。
9. 所有 primitive 成功、PD/池化失败：进入 connector 元数据、KV layout、内存注册、实际读写与释放层，不重复无意义 ping。
10. 服务失败：保留首个失败阶段和各 rank 时间线，不用最后的泛化汇总错误替代首因。

## A5 FullMesh 通道申请卡住的定位流程

适用特征：一个跨卡通信域已进入 HCCL/MC2 通道申请或首次融合算子，进程不退出；部分 rank 的设备利用率高、部分 rank 空闲，日志中出现 FullMesh channel acquire。该特征只能确定优先调查方向，不能单独证明算子不支持、光纤损坏或某个固定端口故障。

### 1. 先区分“发起申请”和“申请完成”

以下是现场常见文案，具体函数名和 success marker 随 CANN/HCCL 版本变化：

- `Entry-HcclChannelAcquire channelNum[N]`：进入或发起申请；`N` 是本次请求的通道数量字段，不是成功计数，也不能单独推出 peer 数、EID 数或物理端口数。
- `[HcclChannelAcquire] acquire channel success ... channelNum[N]`：该版本的一种完成证据；必须结合同一 run、communicator/session、rank 和后续时间线解释。
- 在 acquire 之后出现、且能归属于同一 communicator/call path 的 `operator() success`、kernel launch 或 device synchronize：表示该 rank 已继续前进；不代表其他 rank 也完成建链。

先确认日志时间窗、级别、轮转、截断和 flush 状态。缺少 success 也可能是采集不完整、线程日志交错或进程提前退出；不能把“没有看到”直接当成链路失败。

对同一 run 和通信域收齐所有 rank，至少建立下表；不要只比较两个文件的最后一行：

| rank | 节点/容器 | logical device | communicator/session | acquire entry | acquire completion | 首个 operator/kernel | 最后状态 | local/peer rank 与 EID |
|---|---|---:|---|---|---|---|---|---|
| … | … | … | … | 时间/通道数 | 成功/缺失/失败 | 时间/未进入 | 等待/退出/运行 | 仅填日志或拓扑能证明的值 |

状态可保守归为 `REQUESTED`、`ACQUIRED`、`LAUNCHED`、`WAITING`、`EXITED`。分布式作业可能同时跨在 S3 和 S4：部分 rank 仍申请设备通道，另一些 rank 已启动首算子并等待对端；不能强迫整个作业只贴一个阶段标签。

### 2. 从缺失 rank 反查缺失通信边

1. 用同一 communicator/session 的建链日志、connection tag 或版本对应拓扑输出提取 local rank、peer rank、local EID 和 peer EID。不得从 `channelNum[N]` 猜 peer。
2. 构造有向边 `local rank/EID → peer rank/EID`，再检查反向边。FullMesh 需要本次通信域声明的所有必要边；某个 EID 与一个 peer 成功，不证明同 rank 的其他 EID/peer 都成功。
3. 按现场 `npu-smi info -m/-l`、容器可见设备映射、HiXLEP/route/rootinfo 的实际字段，将每个 rank 映射为“容器逻辑卡 → 物理 NPU/chip → UDie → 本地 EID → 端口”。rank 不等于 physical device，容器可见卡可能重排。
4. 在双端使用当前版本帮助确认过的 `hccn_tool`、HiXLEP/URMA 或板卡管理接口，读取目标端口的 link、enable、PHY、health、down history、错误计数和邻接。若该版本支持，可使用下面的只读命令形态；参数含义以本机 help 为准：

   ```bash
   hccn_tool -g -link -i "$PHYSICAL_DEVICE_ID" -u "$UDIE_ID" -p "$PORT_ID"
   ```

5. 扫描本次通信域应使用的完整端口集合，而不只看故障候选。只有“其余必要端口均 UP，而缺失 EID 边的双端恰好非 UP”才形成较强的定向证据。两个非 UP endpoint 可能是同一物理链路的两端，不能重复计为两个独立故障。
6. 将设备利用率作为旁证：缺 acquire completion 的 rank 空闲，而已 launch 的 rank 高利用率等待，符合“部分参与者没进入 kernel、其余参与者等不到对端”的模式；它仍不能替代 peer/EID/端口映射。

### 3. 区分算子支持问题与环境链路问题

优先支持“环境/设备 fabric 链路异常”的组合证据是：同版本节点上多数 rank 已完成同一算子通道申请并启动，只有与特定 peer/EID 边相关的 rank 缺完成标记；对应双端端口在同一故障窗口非 UP，并有 link down/PHY/训练或计数器证据。此时最终的 `561xxx`、timeout 或 synchronize 卡住通常是后果，不应替代首个坏边。

优先支持“API/算子/版本不支持”的证据是：目标组合在匹配版本支持表之外，或所有相关 rank 在 capability/schema、tiling、kernel lookup/load、参数校验等同一阶段一致失败。不能因为栈中同时出现 V2/V4 名称就判不支持；命名分层见 [MC2 调用栈说明](mc2-testing.md#同一调用栈为什么会同时出现-v2-和-v4)。

`/etc/hccl_rootinfo.json` 生成成功、JSON 中存在 EID、`route.conf` 可解析或 URMA local endpoint 为 ACTIVE，只能证明相应静态资源/本地对象存在。它们不能单独证明远端邻接、物理端口和实际 HCCL channel 都可达。rootinfo 的具体边界见 [拓扑文件说明](topology-and-files.md#a5-fullmesh从-rootinfo-到实时物理边)。

光模块在位、温度/收发光功率和 DDM flag 正常，也不能覆盖 logical/PHY link DOWN。修复路径应根据 enable、训练、固件、交换侧、接线和模块/线缆证据逐项确定；没有证据时不要直接要求换线。

### 4. 脱敏证据模式与闭环标准

一次 A5 MoE FullMesh 调查中，全部参与 rank 都记录了 channel acquire entry；一部分 rank 有 acquire completion 并继续启动，另一子集缺少 completion。缺失子集可映射到若干跨节点对称 peer/EID 边，完整端口扫描又发现只有对应 endpoint 非 UP；节点版本、rank/EID 静态映射和生成的 rootinfo 一致。该组合证据将首要故障域高度收敛到设备 fabric 链路，并降低“该 FullMesh 算子整体不支持”的可能性；在物理修复和同一通信域复跑前，仍应标为高置信判断而不是已确认根因。这是一个有条件的脱敏证据模式，不是所有相似卡住的默认答案。

修复后按以下顺序闭环：

1. 先确认缺失边的双端 endpoint 都恢复 UP，采集时刻、邻接和错误计数一致；未恢复前不反复启动完整模型制造更多等待进程。
2. 在获准的空闲资源上先跑故障相关的最小通信域/算子复现，再扩到原通信域。
3. 要求所有参与 rank 都有版本对应的 acquire completion，首次 collective 或真实 MoE dispatch/combine 完成，并做同步、数值与重复调用校验。
4. 最后重跑原模型请求；只有原失败阶段和真实请求都通过，才称生产路径恢复。端口 UP 快照或单次 acquire 成功本身不够。

## 建议的异常记录字段

run_id/配置指纹、时间、namespace、节点、管理入口与业务 IP、网卡、logical/physical device、group/world/rank、最后成功阶段、失败 API、errno/异常栈、版本、挂载摘要。避免默认采集全进程 argv、完整环境变量、访问令牌和业务 prompt；向外分享前再次脱敏。

# 从历史记录提炼的坑位

本表来自用户授权读取的相关任务中可见的排障摘要；不是全部 ChatGPT 项目都已遍历，也不是当前服务器状态。这里只保留工程结论，不复制密码、原始日志或完整对话。

| 现象/证据 | 应检查什么 | 预检如何前移 |
|---|---|---|
| 19/21 的 16 个 TCP 连接均 ESTABLISHED，rank0 栈停在 getnameinfo / NSS DNS，另一侧 TCPStore 等待 | 容器内反向 DNS、hosts、NSS；不能直接判成端口没通 | getnameinfo 独立子进程硬超时，正反解析一致性，真实 TCPStore 注册/读写 |
| 修复容器 hosts 后 store 继续初始化 | 检查业务地址与别名是否精确对应，必要时用容器 --add-host | 不自动修改系统 DNS，不拿 ping 通当解析正常 |
| 管理 IP 为 141.*，19 的业务 IP 实际是 172.27.8.193 | 管理网与业务网不同，尾号不对应 | 自动发现 UP 网卡 + CIDR，拒绝多解，不拼接 .19 |
| 请求全失败但服务端没对应记录 | 代理环境、no_proxy、入口 URL、请求是否到了正确机器 | 仅记录代理是否存在而不泄露代理凭据；HTTP 验收显式绕过代理并核对路由 |
| Mooncake 部分会话成功，另一些 EAGAIN/recv 超时 | 对照 session、rank、两端日志、元数据内容和动态端口，不直接下结论“整个网不通” | 双向数据校验 + 真实 connector/大小梯度；静态端口探针不冒充动态会话 |
| 宿主机有 launcher，容器里找不到 | 挂载路径、入口 cwd、source 环境、文件权限 | 在目标 namespace 执行预检；env_scripts 路径必须是容器内实际路径 |
| 8000 与 8001 对错，或 8000 已被其他管理进程占用 | 绑定地址、端口归属、配置来源 | 先 bind 成功再发 ready；失败时不向已有服务发送自定义探针 |
| 老容器/孤儿 worker 或 DP coordinator 仍在 | 卡占用、进程所属容器和作业；不得直接 killall | npu-smi/ss 只读采集，用户确认空闲后再占卡测试 |
| 非交互 docker exec 找不到 libhccl.so | .bashrc 未生效、CANN 环境/挂载/版本 | 显式 source 用户指定环境文件；不依赖交互终端的隐式状态 |
| health=200 但真实模型请求返回400/代理吞错，KV 长时间占用 | 模型名、max-model-len、TP/DP 元数据、请求路由、错误回传和释放路径 | 长短请求、各角色路由、KV 释放的真实验收，不只 curl /health |
| 一个节点时钟异常 | 证书、超时、日志对齐的影响；不要没有证据就当根因 | 记录节点时钟/偏移线索，不擅自改系统时间 |

## 最小排障决策

1. SSH/容器/source 失败：先修环境边界，不继续占 NPU。
2. 没有唯一业务 IP：提供业务 CIDR 或实际 IP，不选“第一张网卡”。
3. TCP bind 失败：端口已占用或地址不存在，保留占用者，换维护窗口或空闲端口。
4. TCP connect 成功、数据校验失败：看源地址、回程、连接被谁接受、报文完整性；不能直接算建链成功。
5. DNS/TCPStore 失败：看每 rank 的 tcpstore_enter/ready 与解析耗时；16 个 ESTABLISHED 不是排除 DNS 的证据。
6. Gloo 失败、store 成功：核对 peer 互联、动态回连、CPU 网卡和分组，不只检查 master。
7. HCCL 初始化/首算子失败：拆 Host 控制面、Device 链路/拓扑、卡映射、版本、资源与算法；用 pairs 缩小到卡对。
8. 小 HCCL 成功、大流量失败：加大小/次数，核对内存、链路错误与拥塞；小 TCP 不提供 RDMA MTU 或带宽保证。
9. 所有 primitive 成功、PD/池化失败：进入 connector 元数据、KV layout、内存注册、实际读写与释放层，不重复无意义 ping。
10. 服务失败：保留首个失败阶段和各 rank 时间线，不用最后的泛化汇总错误替代首因。

## 建议的异常记录字段

run_id/配置指纹、时间、namespace、节点、管理入口与业务 IP、网卡、logical/physical device、group/world/rank、最后成功阶段、失败 API、errno/异常栈、版本、挂载摘要。避免默认采集全进程 argv、完整环境变量、访问令牌和业务 prompt；向外分享前再次脱敏。

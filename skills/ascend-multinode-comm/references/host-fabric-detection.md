# 宿主机组网识别与设备连通检测

适用：“给你这些服务器，判断 RoCE/UBoE/FullMesh 组网，以及能不能通”。可用于启动前预检，也可用于当前建链失败排查。用户只查宿主机时不索要容器/部署脚本，不进入容器；仅更新技能时不重连历史服务器。

## 先分清三个问题

1. **当前设备通路**：HCCS、RoCE、UB/UBoE，可能分层共存。不能强行在用户给出的三个名词中选一个而漏掉 HCCS。
2. **物理拓扑**：直连、交换、层级、物理 fullmesh，需要指定范围内的端口邻接和路由证据。`HCCS_SW` 表示查询范围内经交换机的 HCCS；不是跨服务器全直连证明。不要把 topo 输出图例中的名字当成实际矩阵项。
3. **算法**：`HCCL_ALGO=level0:fullmesh` 是配置，是否运行还需实际进程/日志确认。不能靠设置它修复底层不可达，也不因设备 ping 通过自动设置。

报告同时回答“观察到什么网络”和“哪些节点/设备/方向实际通了”。Host ping/SSH、设备 ping、HCCL collective、MC2/KV 与真实服务分别记录；未执行的层不可填 PASS。

## 从 SSH 到逐设备证据

先按 [连接指引](remote-server-audit.md) 使用本次账号和认证方式。密码可正常交互登录；用户要求免密时追加本地专用公钥后使用批量 CLI。新工具支持 `identity_file`，无需为它额外修改全局 SSH config；不接受密码字段或私钥正文。

- 获取 `npu-smi info -m`、`-l` 和命令帮助。按映射中的 NPU ID / chip ID / logical ID / physical ID 查设备，不解析主状态表猜卡数、不使用 `card*2`、不默认编号连续。Mcu 行不是计算设备。
- 读取 board/驱动固件信息。通用 `Ascend910` 或陌生板卡编号不能强行归类 A3/A5；但型号未知不妨碍在工具帮助和实际设备证据支持时查询 HCCS。
- 通过帮助确认后，对每个映射设备读 hccn IP/link/net_health/LLDP/vNIC，以及 npu-smi spod-info/hccs；记录原始输出和失败，缺工具/权限不能伪造成链路 DOWN。
- 记录每设备的 vNIC IP、link、SDID、Super Pod ID、Super Pod Size 原值、Server Index。Pod ID 或 SDID 为 0 不等于缺失。Size 未确认单位前不解释为服务器台数。
- 获取 Host 网卡/路由、RDMA/URMA 和 UB 设备线索。Host USB 管理口通不代表数据平面；Host RDMA 列表为空也不能否认 NPU 自有网络。
- 判断 RoCE 未配置需设备端证据：完整已枚举范围内 link DOWN 且 IP 查询明确表示未配置。RoCE DOWN 与 HCCS UP 可以同时成立，不把可选 RoCE 平面未配置当成 HCCS 故障。
- UB/UBoE 需要版本匹配的设备/端口状态、邻接/路由及实际通道证据。缺少 `urma_admin`、`/dev/uburma` 等只记“未发现证据”；发现工具也不直接标记 UBoE 已启用。继续按 [拓扑指南](topology-and-files.md) 核实，不伪造通用 URMA schema。
- 配置路径先区分文件/目录/不存在，尤其 `/etc/hccl_rootinfo.json` 可能实际是目录。没有消费方或挂载证据，不断言是网络失败原因，不自动删除。

## 地址归属：避免测到本机或另一台同地址设备

同一个 vNIC 地址或 SDID 可能出现在不同 Pod 中。跨隔离域重叠不必然是 IP 冲突，但仅凭 ping 无法跨域唯一定位对端。

| 发现 | 处理 |
|---|---|
| 目标地址/SDID 与发起节点本机设备重复 | 不将响应认作远端成功；跳过歧义边并说明可能命中本机 |
| 目标所在相关通信域中有其他同地址/SDID 设备 | 不发自动 ping，先解决对端身份歧义 |
| 相同地址只出现在另一个独立 Pod | 可对已核实同 Pod 的唯一对端探测，报告仍列出跨域重叠 |
| 跨 Pod 地址唯一、发送失败 | 保留设备层失败证据；结合本机跨卡对照，核实超节点归属/交换分区/路由/接线 |
| 跨 Pod 收到回复、但缺少跨域对端身份依据 | 收包事实保留，目标归属仍 UNVERIFIED，不把成功重复归属给两台服务器 |
| 同 Pod 不同 SSH 节点的 Server Index 重复 | 核实是否同一宿主的不同入口，或配置冲突，不直接作跨主机验收 |

跨机失败而机内对照通过，支持继续排查跨机域/路径，不证明一定是某根线或某个配置错误。**不要直接把所有 Pod ID 改成一样**；还需核实预期分组、Server Index、SDID、地址唯一性和交换侧配置。

## HCCS 小包执行与判定

用户已明确要求设备连通性测试时，在该范围内选择有限的小包探测；仅只读则不发流量。它不是 collective/带宽打流授权，不启动模型或 NPU 计算 rank。

先最小设备对，按需要扩展同编号设备或显式设备对，并做双向验证。故障相关时加本机跨板卡对照；不对全部节点盲目做全矩阵。`同编号 N 对 × 双向` 不等于 `N×N 全卡对`，逻辑设备数也不等于物理板卡数。

本次工具适配的帮助格式支持 `address`、`cnt`、`timeout` 后，使用最多 3 包、每包等待 500 ms、单条命令外层 10 秒的探测，包长保持该版本默认并保留帮助。版本不支持这些参数时拒绝自动执行，由 agent 选择该版本可安全限时的方法。官方 HCCS 入口见 [vLLM-Ascend 多机检查](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/large_scale_ep.html#verify-multi-node-communication-environment)。

**退出码 0 和 `Cmd executed successfully!` 不代表 ping 成功。** 解析实际 transmitted/received/loss、失败文本、L1 plane 检查和超时：

- 本次要求 3 包，必须 3 发 3 收、0% 丢包且无失败文本/非零 L1 plane 结果，才可通过该方向的小包检测。
- `send fail`、`not start to send`、非零退出、超时、部分丢包或实际未完成预定包数判 FAIL。
- 统计缺失、多个冲突统计、输出截断、未知版本格式，不能仅凭成功字样通过，记 UNVERIFIED（已有明确失败证据时仍保留 FAIL）。
- 每次发送前复核源/目标身份与 vNIC。拓扑/配置/占用变化时重新采集；快照不是永久身份保证。

## 可复用工具：fabric_probe.py

此脚本独立于 `preflight.py`，不 source 环境、不执行容器操作、不改远端安装和配置，代码通过 SSH stdin 传输。控制端 Python 3.10+ / OpenSSH，远端 Linux / Python 3.10+；先建立可用的密钥/agent 认证。默认 accept-new 保留主机密钥变化检查；严格环境可使用符合其策略的连接方式执行等价指引。

配置使用独立的 `examples/fabric-nodes.json`，不要直接传带有容器/环境字段的 cluster 配置。按本次数量增删 nodes、替换 SSH 占位符；可选 `port`、`identity_file`（本地路径）、`python`（远端可执行路径）。**配置不保存密码。** 1～64 个节点是本工具单次采集边界，不是场景限制；IPv6 通过预先配置的 SSH 别名访问，设备 ping 适配目前仅 IPv4。

在技能目录执行：

```bash
# agent 根据本次信息生成/填写 fabric-nodes.local.json，不连接示例占位符。
python scripts/fabric_probe.py inspect --config examples/fabric-nodes.local.json --out reports/fabric-inventory.json

# 先生成计划，只重新采集，不主动 ping。变量须来自本次节点名与 -m 映射。
python scripts/fabric_probe.py ping --config examples/fabric-nodes.local.json \
  --source "$SOURCE_NODE" --target "$TARGET_NODE" \
  --pair "$SOURCE_DEVICE_ID:$TARGET_DEVICE_ID" --bidirectional --out reports/fabric-plan.json

# 本次允许小包检测后执行；不用旧计划里的地址，重新采集并校验。
python scripts/fabric_probe.py ping --config examples/fabric-nodes.local.json \
  --source "$SOURCE_NODE" --target "$TARGET_NODE" \
  --pair "$SOURCE_DEVICE_ID:$TARGET_DEVICE_ID" --bidirectional --execute --out reports/fabric-ping.json
```

`--pair` 可重复以选择不同编号设备；需要同编号覆盖时改用 `--same-index`，按实际枚举交集匹配。两端设备集合不同会记录未匹配范围，不能静默声称全部设备通过。同宿主对照用同一 source/target 节点和不同设备 ID 的显式 pair；自 ping 拒绝。一次最多 512 条有向边，逐条执行，不默认全卡对矩阵或自动选全部节点对。

每次生成新 JSON 和同名中文 Markdown，不覆盖已有结果。`inspect`/仅计划通常返回 2；执行后 0=仅声明的小包方向通过、1=存在实测失败、2=未做/缺证据/归属歧义。远端命令逐条限时，采集总预算 600 秒；SSH 失败保留可访问节点的证据，报告不将管理入口失败当成 NPU 网络失败。

自动化边界：已适配现场观察到的 `npu-smi -m` 表头、vNIC/spod 和 HCCS ping 统计格式；未知格式不猜。工具采集 RoCE/URMA 原始证据，但不提供通用 UBoE 自动判型/打流器、不自动还原跨机物理邻接、不执行 RoCE bandwidth test。这些路径由 agent 按匹配版本与授权继续检查；不能将 NO_EVIDENCE 写成不存在该网络。

JSON 包含逐设备映射/状态、Pod 分组、地址/SDID 重叠、所选有向边、原始命令/结果及未测项。报告含现场拓扑，保存在忽略目录，不推送仓库。

设备小包通过后，按实际目标继续 [HCCL 打流](hccl-testing.md)、[MC2](mc2-testing.md) 与 KV/服务验收；它们需要独立资源和授权。不要用小包 PASS 取代这些阶段。

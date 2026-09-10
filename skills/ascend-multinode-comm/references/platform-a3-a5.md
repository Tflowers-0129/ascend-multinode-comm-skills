# A3 / A5 平台识别与分支检查

本库覆盖两种平台上的预检与排障流程，不承诺任意芯片、CANN、镜像、connector 和 MC2 组合已经兼容。先确认平台身份，再确认版本与通信路径，最后提供实测证据。平台身份 PASS 不等于通信 PASS。

## 1. 现场识别，不按脚本名字判断

在用户指定宿主和目标容器分别采集 `npu-smi info`、`npu-smi info -l`、`npu-smi info -m`，关联物理设备、chip ID、容器可见卡与 torch 逻辑编号；记录驱动/固件、CANN、torch/torch_npu、vLLM/vLLM-Ascend、镜像 digest 和自定义算子包版本。CLI 缺失/权限不足与硬件故障分开记录。

- A3 型号线索：`Ascend910_93` 及相应子型号。A2/A3 都可能出现 DAV_2201 / ASCEND910B 这一架构或软件标识，不能仅凭它们区分平台。
- A5 型号线索：`950DT` / `950PR` 及相应子型号。记录具体型号，不能只留一个 A5 标签；不从 DT/PR 名称强行规定节点只能担任 P 或 D。
- `A3` 字样、模型名中的 A3B、镜像 tag、主机别名、已安装的 platform_config 文件，都不是当前物理设备的证明。
- 工具只保守匹配成功的实时 `npu-smi info` 型号输出；新型号/未知格式保持 UNVERIFIED，不猜测。agent 应查匹配版本的 npu-smi 帮助与板卡信息，记录原始来源，再补充可回归的解析规则；不要手改报告或用配置声明把未知变成 PASS。

需要细化板卡信息时，按已发现的 NPU/chip ID 查询，不假定每卡有两个 chip；A3 的 board 查询方式亦见 [Ascend 官方型号获取说明](https://github.com/Ascend/IndexSDK/blob/master/docs/en/user_guide.md)：

```bash
npu-smi info -t board -i "${NPU_ID:?填写已核实的设备ID}" -c "${CHIP_ID:?填写已核实的chip ID}"
```

模型名称与镜像必须分别核实。官方安装文档区分 A3/A5 发行物；使用与现场硬件、驱动/CANN 和所选 release 配套的镜像/包，不因镜像可以启动就认为通信内核匹配。[vLLM-Ascend 安装说明](https://docs.vllm.ai/projects/ascend/en/v0.23.0/installation.html)

## 2. 分开检查的内容

| 检查项 | A3 路径 | A5 路径 | 共同边界 |
|---|---|---|---|
| Host 控制面 | 业务网卡/IP、DNS、TCPStore、Gloo、HCCL Host 建链 | 同左，不用设备 UB 地址替代 Host IP | 管理口 SSH 成功不代表业务面成功 |
| 设备网络 | 对 HCCS 部署核对 vNIC、superpod/SDID、链路与跨节点设备可达；实际 RoCE 路径另查端口/GID/路由 | 按实际系统查 UB/URMA、UBoE、RoCE、HiXLEP 及端口邻接关系 | 能力、实际传输、物理拓扑和 HCCL 算法分栏报告 |
| 容器资源 | 设备映射、库、必要网络配置；不能强加 A5 UB/HiXLEP 挂载 | 按版本的设备与 LocalCommRes 消费链检查 | rootinfo/hixlep 都不默认注入，也不自动删除 |
| MC2 | 按具体 API 的 A3 芯片、rank 数、HCCS/组网、模式限制核实 | 按具体 A5 型号和版本支持核实，不能套用 A3 支持表 | AIV 打流、普通 collective、融合算子三类证据独立 |
| PD / 池化 | 使用该平台受支持的 connector 与 KV 测试 | 同左；额外资源文件按实际消费者检查 | 控制面、内存注册、真实数据读回与释放都要验收 |

这些是检查路由，不是硬件固定配置表。不能因为 A3 没有 `/dev/ub*` 或 `urma_admin` 就判为建链失败，也不能因为 A5 没有某个 RoCE IP 就判为 RoCE 故障。

### A3 HCCS 分支

官方大规模 EP 示例的 A3 互联检查使用 vNIC、superpod/SDID 与 HCCS ping。使用前仍应核对现场是否采用该网络范围；该例的服务器数、卡数、chip ID、IP、并行规模都不是通用默认值。[A3 互联检查来源](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/large_scale_ep.html)

普通安装互联预检还展示了 A3 的 `-ip -g` 查询；不要把 HCCS 分支解释为所有场景都必须只用 vNIC。工具保留 IP 与 vNIC 两类原始输出，由 agent 按实际版本、组网和日志判断用途。[安装互联预检](https://docs.vllm.ai/projects/ascend/en/v0.23.0/installation.html)

先按显式物理设备 ID 做有界只读查询：

```bash
hccn_tool -i "${PHYSICAL_DEVICE_ID:?填写本次物理设备ID}" -vnic -g
npu-smi info -t spod-info -i "${NPU_ID:?填写本次设备ID}" -c "${CHIP_ID:?填写本次chip ID}"
```

结合该版本 `-link -g`、`-net_health -g`、`-lldp -g`、`-netdetect -g`、`-gateway -g` 输出以及授权范围内的 `/etc/hccn.conf`，建立“节点→物理设备/chip→vNIC→superpod/SDID→预期对端”映射。不能要求所有设备 SDID 一样，也不能无依据修改 superpod；比较唯一性/归属与实际组网约束。

跨设备探测会产生流量，须在确定对端设备地址和获得测试授权后执行；设置总超时，逐项保存方向、返回码和错误：

```bash
timeout "${PROBE_TIMEOUT_S:?填写授权的秒数}" \
  hccn_tool -i "${PHYSICAL_DEVICE_ID:?填写本次物理设备ID}" \
  -hccs_ping -g address "${PEER_DEVICE_IP:?填写对端vNIC设备地址}"
```

不把 Host IP 填进上述设备地址，也不拿 Host ping 替代。对本次必要设备边双向检查，先最小范围，后按卡对/组扩展。工具 `inspect` 已在识别到 A3 且提供 `physical_devices` 时追加 vNIC/netdetect/gateway 采集；spod-info 与主动 hccs_ping 由 agent 按已核对的 chip 映射和版本执行，当前没有自动枚举所有 chip、解析全部厂商邻接表或自动发起设备 ping。

之后仍须运行 [HCCL 测试](hccl-testing.md) 和实际 [MC2 测试](mc2-testing.md)。设备 ping 成功不证明 HCCL communicator、专用资源、数值结果或图捕获成功。

### A5 分支

沿用 [拓扑与文件规则](topology-and-files.md)，记录 URMA/UB/RDMA 能力、实际链路与算法选择日志；有 fullmesh 配方时仅在对应测试环境继承 `HCCL_ALGO=level0:fullmesh`，不把它当作物理直连证明。rootinfo、hixlep.json、hixlep 目录和 route.conf 按实际消费者与生成版本分别核实。A3 成功配方不能成为删除 A5 必需资源的依据，反向套用也不成立。

## 3. 配置、通信分组与放行

`nodes[].platform` 可省略或为 `auto`，也可声明 `A3` / `A5`。声明用于比对，不改变环境，不选择默认网卡/卡数/算法/镜像。

- `checks.platform` 分节点给出声明、观察型号、来源及身份状态；明确矛盾为 FAIL，暂停后续主动探针。识别不全为 UNVERIFIED，仍可运行已授权的通用基础探针，但不执行 MC2，不通过 mc2/service gate。
- 同一测试通信组发现多种已识别平台时，标记 UNVERIFIED 并暂停主动测试；这是工具的保守支持边界，不是声称混合平台通信必然不可能。需要匹配版本的专门支持证明和隔离适配器，不自动拼组尝试。`pairs` 也不能绕过此限制。
- 多个独立 P/D 通信组可以分别属于 A3/A5；这只说明没有把它们误放进同一集合通信组。跨平台 P→D 的 connector、KV layout、dtype、设备注册与真实读回仍需独立证明，不自动授予支持。
- `platform` 是 mc2/service 的必要身份检查，不加入 primitives/pairs 的通用基础范围；后两者即便通过也不得写成“A3/A5 全栈认证”。原有基础测试范围保持兼容。
- `HCCL_BUFFSIZE`、`HCCL_OP_EXPANSION_MODE` 可显式采集/继承，不按 A3/A5 自动填写值；P/D 不同环境需分配置运行，当前全局 environment 不能表达逐组差异。

现有 `A5COMM`、`A5_ADAPTER`、`A5_MC2_REQUEST` 是保留的历史协议标识，A3 也使用同一协议。不要擅自改名使旧适配器失效，也不能据此判断平台。

## 4. 成功案例怎么补

平台扩展不以提供大量脚本为前提。优先收集不同平台/模式/网络/MC2 分支的代表案例，见 [成功部署样本指引](known-good-deployments.md)。仅在该案例真实覆盖的组合内提炼经验，不把历史成功推广成全部配置已验证。

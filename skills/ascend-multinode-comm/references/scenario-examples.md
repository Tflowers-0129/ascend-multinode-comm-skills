# 单机与多机部署：六类预检/排障示例

用户只需提供本次服务器连接信息、现有容器、工作目录和远端部署脚本。下面的拓扑用于说明如何检查，不要求用户先算好 DP/TP；已有部署以远端脚本、实际进程和设备映射为准。预检与排障都可使用，不绑定特定 agent。

## 先看范围

E01、E02、E03、E05、E06 对照 vLLM-Ascend v0.23.0 官方配置，源码固定为 `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`；E04 是本库编写的扩容算例，不是上游已验证配方。所有例子均未在本次现场运行：`RUNTIME_UNVERIFIED`。完整镜像、权重、环境变量、模型参数和配套代理见各节官方来源，不能只用这里的并行参数片段启动模型。

| 示例 | 物理部署 | 并行配置摘要 | 来源性质 |
|---|---|---|---|
| E01 单机混部 | 一台 A3 或一台 950DT | A3：DP4/TP4；950DT：DP4/TP1 | DeepSeek-V4-Flash 的不同平台配方 |
| E02 单机 PD 分离 | 一台 950DT，P/D 使用不重叠设备 | P：DP4/TP1；D：DP4/TP1 | DeepSeek-V4-Flash 单机 1P1D |
| E03 双机混部多 DP | 两台同平台服务器 | A3：全局 DP2/TP16；950DT：全局 DP2/TP8 | 分别对应 DeepSeek-V3.2 / V4-Pro |
| E04 多机混部多 DP | 演示四台，也可按实际 N 台展开 | 演示全局 DP8/TP4，每台两个 DP rank | 资源/rank 算例，模型支持待核实 |
| E05 双机 PD 分离 | 一台 A3 跑 P，另一台 A3 跑 D | P：DP4/TP4；D：DP16/TP1 | DeepSeek-V4-Flash A3 配方 |
| E06 多机 PD 分离 | 官方例子 P 四台、D 四台 950DT | P：DP32/TP1；D：DP32/TP1 | DeepSeek-V4-Pro 逻辑 1P1D |

这里的设备数量按运行时可见的计算设备/rank 核算，不把“8 张物理卡”直接替换成“8 个 torch 逻辑设备”。先用现场型号、设备查询和容器掩码确认映射；A3 示例里的 TP16 尤其不能误改成 TP8。950DT 配方也不代表所有 A5 型号通用。

在没有 PP/CP 等额外布局因素的这些算例中，一个角色组的工作进程数按 `DP × TP` 核算；全局 DP 为各节点本地 DP 数之和，rank 起点为之前节点本地 DP 数的累加。EP 组另从实际模型实现还原，不能再盲乘一个 EP 因子，也不能断言 DP rank 之间没有 MoE 通信。

## 通用的现场请求

复制以下模板，再补充对应示例的小段要求。节点清单可以是一台、两台或更多台，不自动连接历史示例 IP。

```text
请使用 ascend-multinode-comm 技能，直接连接本次服务器检查。
任务：启动前通信预检 / 当前建链失败排障（选择其一）。

逐项连接信息：节点名、IP 或 SSH 别名、SSH 端口、用户名、认证方式；如需跳板机一并说明。
逐项运行位置：现有容器名、容器内服务用户、容器内工作目录、远端部署脚本路径。
可说明共用设置，再列出各节点差异。入口/source 依赖、平台、版本、角色和并行参数请从现场发现。
如有 proxy 或存储服务，补充其部署位置和远端脚本；不要求它独占一台服务器。
排障时：错误摘要、发生时间、日志路径已知则提供；未知请在上述范围定位。

先只读检查；不要重新执行部署脚本，不修改、重启、清卡或删除日志/配置。
需要主动探针时，请列出节点、设备、临时监听端口、超时与影响范围，再确认可用窗口。
请区分已确认错误、条件风险、已实测阶段和未验证项，并给出远端文件行号或运行时证据。
```

密码不填写在该模板或聊天中；通过当前环境实际支持的安全凭据输入渠道，或用户自己的 SSH 终端处理。详见 [服务器连接指引](remote-server-audit.md)。没有访问能力时如实说明，不能把本地推演当成已连接。

## E01：单机混部

**官方对应**：[DeepSeek-V4-Flash §5.1](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Flash.html)；固定源码 [A3 tab](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Flash.md#L233)、[950DT tab](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Flash.md#L280)。A3 使用对应 Ascend 量化权重，950DT 使用原始权重；两套模型/量化配置不能混抄。

```text
同一台服务器
  混部角色组：每个 DP rank 都处理 Prefill 和 Decode
  A3 配方：4 个 DP rank × 每 rank TP4 = 16 个计算设备
  或 950DT 配方：4 个 DP rank × 每 rank TP1 = 4 个计算设备
```

追加给 agent：

```text
这是单机混部。请检查远端入口与全部 worker 的设备映射、TP/DP/EP 分组、
本机 rendezvous/Gloo/HCCL 初始化及实际启用的融合算子路径。
不要因为没有第二台服务器就停止，也不要凭单机就跳过通信检查。
若脚本没有远程 KV/池化配置，不要求额外建立 P→D 跨实例 KV 通道。
```

重点：本机进程间仍有控制面通信；TP/EP 实际使用哪些 HCCL/MC2 路径取决于配置。TP1 不是“无需通信”的证明。没有异机对端时，跨机网卡/交换链路不在本次验收范围；单机成功不能为多机放行。

## E02：单机 PD 分离

**官方对应**：[DeepSeek-V4-Flash §5.2.3](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Flash.html) / [固定源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Flash.md#L931)。虽然上级标题写 Multi-Node，该 950DT 小节明确是同一服务器的 P/D 分离，不能当作跨机成功证据。

| 同一宿主上的角色 | 示例可见设备掩码 | DP / TP | 示例 API 端口 | 示例 DP RPC 端口 |
|---|---|---|---|---|
| P | `0,1,2,3` | 4 / 1 | 8000 | 12321 |
| D | `4,5,6,7` | 4 / 1 | 8001 | 12325 |

上表沿用该配方的演示值，不是执行默认值。两个角色组各有自己的 DP rank 空间，不合并成 DP8 的生产组。代理转发到 P/D 各自 API；另按 `MooncakeHybridConnector` 验证 KV 传输。工作目录内应配套检查 `run_prefill.sh`、`run_decode.sh` 和实际 proxy 配置。

追加给 agent：

```text
这是一台服务器上的 PD 分离。请分别检查 P、D 的远端脚本和实际容器/进程。
重点检查两侧设备是否重叠、同一网络 namespace 的端口是否冲突、
独立 DP 控制域和 connector 声明是否正确，以及代理到 P/D 和实际 KV 通路。
两侧可在同一容器也可在不同容器，请以现场为准，不能把容器名当成物理节点数。
```

两个容易误判的点：

- 该版本外部 DP launcher 默认从设备编号 0 连续切分。如果将两套 launcher 直接搬到同一可见设备空间，可能同时占用前几张卡；不能用不同目录/不同 API 端口代替设备隔离。该单机官方例子使用各自明确掩码的 P/D 脚本。
- 固定源码 P/D 的 `kv_port` 和 `engine_id` 存在相同字面值。应核对实际版本如何派生端口/标识、设备资源与监听 namespace，再判是否冲突；既不把官方文字直接判成故障，也不默认照抄安全。保留现存错误和监听归属作为证据。

950DT 配方包含 fullmesh 与 HiXLEP 资源路径；沿用 [拓扑文件审计](topology-and-files.md)，不要自动补挂或删除 rootinfo，更不能把 `/etc/hixlep/` 目录写成 `/etc/hixlep.json`。

## E03：双机混部、多 DP

**官方对应**：[DeepSeek-V3.2 A3 §5.2 源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V3.2.md#L178) 与 [DeepSeek-V4-Pro 950DT §5.1 源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Pro.md#L464)。选一个匹配平台/模型的完整配方，不混搭两侧。

| 参数 | 节点 A | 节点 B |
|---|---|---|
| 全局 DP | 2 | 2 |
| 本地 DP | 1 | 1 |
| 本地 DP 起始 rank | 0 | 1 |
| TP | A3 配方 16；950DT 配方 8 | 与节点 A 所选配方相同 |
| DP master / RPC 端口 | A 的可达业务地址 / 本组端口 | 与 A 一致 |
| `HCCL_IF_IP` | A 自己的业务 IP | B 自己的业务 IP |
| API 角色 | 对外入口 | 该配方使用 `--headless` |

对应原生 vLLM DP 参数片段（不是完整启动命令）：

```text
A：--data-parallel-size 2 --data-parallel-size-local 1 --data-parallel-start-rank 0
B：--data-parallel-size 2 --data-parallel-size-local 1 --data-parallel-start-rank 1 --headless
两侧共同：--data-parallel-address <A业务IP> --data-parallel-rpc-port <组内同一端口>
```

追加给 agent：

```text
这是双机混部多 DP。请检查 master 地址一致但本机 HCCL_IF_IP 各自正确，
DP rank 区间不重叠；在两端实际 namespace 检查 TCPStore/Gloo 建链。
请还原 TP 和可能跨 DP 的 EP 组，分别验证 HCCL、实际 MC2/专家通信路径，
不要把两台机器所有设备跑一次 AllReduce 就称为生产通信组全部通过。
```

950DT 的该模型 TP 配方还配有 FlashComm 和 DSA-CP，不能只把单机模板的 TP 改成 8。其 `HCCL_ALGO=level0:fullmesh` 是算法设置，不证明底层为物理直连；RoCE/UB/UBoE 路径仍要现场探测。

## E04：多机混部、多 DP（扩容算例）

下面演示“每机多个 DP rank”，不是推荐任何模型都采用 DP8/TP4。参数含义参考 E03 的原生 DP 启动方式；需另核实模型、专家数整除约束、平台/版本支持、显存与网络后才能生成可部署副本。

| 演示节点 | 可用逻辑设备数 | 本地 DP | TP | DP rank 区间 | 起始 rank |
|---|---|---|---|---|---|
| A | 8 | 2 | 4 | 0～1 | 0 |
| B | 8 | 2 | 4 | 2～3 | 2 |
| C | 8 | 2 | 4 | 4～5 | 4 |
| D | 8 | 2 | 4 | 6～7 | 6 |

四台都声明全局 DP8，每台 `--data-parallel-size-local 2`，各自 `--data-parallel-start-rank` 为上表起点。总计 32 个计算设备；不是 DP4，也不是每台再启动 8 个 DP rank。以 A 为入口的该原生 headless 布局中，其他节点沿用配套的 headless 方式；不能另把外部 DP proxy 的端点规则强加进来。

任意 N 台时，按各节点实际本地 DP 数累加，不能始终按“节点序号”当 rank 起点。例：本地 DP 为 1、2、1 时，起点是 0、1、3，全局 DP4。设备不足或模型不支持不等于网络故障。

追加给 agent：

```text
这是多机混部，节点数以本次清单为准。请从各远端脚本计算全局/本地 DP、
rank 区间和 TP/EP 分组，检查漏 rank、重复 rank、各节点实际空闲设备数，
再生成“通信域 → 节点/设备 → 控制面和数据面”的测试清单。
同一台机器上不同 TP 子组请分别记录，不得通过重命名节点规避工具限制。
```

检查范围包括所有参与节点的控制面可达性和所需 HCCL/MC2 域；不只测 A↔B。需要逐卡覆盖时，按真实节点组合使用 [参数化 HCCL 指南](hccl-testing.md)，并先估算卡对数量和占用窗口。卡对通过仍不等于大通信域/图模式通过。

## E05：双机 PD 分离

**官方对应**：[DeepSeek-V4-Flash A3 §5.2.1](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Flash.html) / [固定源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Flash.md#L371)。该例的 P/D 并行配置不同，不可推广为任意 connector 都支持任意非对称布局。

| 角色 | 所在服务器 | 全局 DP / TP | 本地 DP / 起点 | 对应计算设备数 |
|---|---|---|---|---|
| P | P-A | 4 / 4 | 4 / 0 | 16 |
| D | D-A | 16 / 1 | 16 / 0 | 16 |

P/D 分别使用自己的 master 地址和角色模板。以下是官方 launcher 的参数化调用示例，只有在对应远端工作目录准备并审阅完整 `launch_online_dp.py` + 角色专用 `run_dp_template.sh`，且用户授权部署后才执行；预检/排障只读取它们。

```bash
# P-A 上，P 专用工作目录；变量由本次现场填写，不能照搬管理 IP。
python launch_online_dp.py --dp-size 4 --tp-size 4 \
  --dp-size-local 4 --dp-rank-start 0 \
  --dp-address "${P_MASTER_IP:?填写P组业务IP}" \
  --dp-rpc-port "${P_RPC_PORT:?填写P组RPC端口}" \
  --vllm-start-port "${P_API_START_PORT:?填写P侧API起始端口}"

# D-A 上，D 专用工作目录。
python launch_online_dp.py --dp-size 16 --tp-size 1 \
  --dp-size-local 16 --dp-rank-start 0 \
  --dp-address "${D_MASTER_IP:?填写D组业务IP}" \
  --dp-rpc-port "${D_RPC_PORT:?填写D组RPC端口}" \
  --vllm-start-port "${D_API_START_PORT:?填写D侧API起始端口}"
```

两侧 connector 对并行布局的描述都应是 P=DP4/TP4、D=DP16/TP1，而不是各写各的本地值。外部 DP launcher 为本地每个 DP rank 启动 API 实例；该例分别产生 4 和 16 个端点，代理按配套协议登记。不要把 E03 的一个 API + headless 规则套过来。

追加给 agent：

```text
这是两台服务器上的 PD 分离。请分别还原 P、D 的独立模型通信组，
再检查 proxy、双方 connector/并行描述、KV 控制端口与实际 KV 数据路径。
请区分“P 内部失败”“D 内部失败”“跨 P/D KV 失败”，不要把 P+D 合并成一个 HCCL 组。
需要验证 MC2 时按各侧实际算子与组分别安排；两台机器之间 AllReduce 通过不等于 KV 通过。
```

## E06：多机 PD 分离

**官方对应**：[DeepSeek-V4-Pro §5.2.3](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Pro.html) / [固定源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Pro.md#L1333)。这里“1P1D”表示一个逻辑 P 组和一个逻辑 D 组，并不是两台物理机器。

| 角色组 | 演示服务器 | 每机本地 DP / TP | 各机 DP 起点 | 组内全局 DP |
|---|---|---|---|---|
| P | P-A、P-B、P-C、P-D | 8 / 1 | 0、8、16、24 | 32 |
| D | D-A、D-B、D-C、D-D | 8 / 1 | 0、8、16、24 | 32 |

P 组共同连接 P-A 的 master；D 组共同连接 D-A 的 master。D 的 rank 从 0 独立开始，不从 P 的 32 接着算。两个组各自保留 P/D 专用的 `run_dp_template.sh`，均配套同一版本 launcher。

逐节点调用形式如下；每次 `cd` 到该节点的对应角色目录，变量由上表及现场填写。它是部署参数示例，不属于默认预检动作。

```bash
python launch_online_dp.py \
  --dp-size "${GROUP_DP:?填写角色组全局DP}" \
  --tp-size "${GROUP_TP:?填写角色组TP}" \
  --dp-size-local "${LOCAL_DP:?填写本机本地DP}" \
  --dp-rank-start "${DP_START:?填写本机起始rank}" \
  --dp-address "${GROUP_MASTER_IP:?填写本角色master业务IP}" \
  --dp-rpc-port "${GROUP_RPC_PORT:?填写本角色RPC端口}" \
  --vllm-start-port "${LOCAL_API_START_PORT:?填写本机API起始端口}"
```

该官方布局中，每台有 8 个外部 DP API 端点，P/D 分别共 32 个；proxy 不能只登记每台第一个端口。双方 KV 并行描述对应各自全局 DP32/TP1。扩缩容时，launcher、rank 区间、KV 并行描述和 proxy 端点必须一起核对；四台只是本配方规模，不能当成技能固定节点数。

追加给 agent：

```text
这是多机 PD 分离。请从本次清单还原 P 组、D 组各自覆盖哪些服务器及 DP/TP/EP 域。
分别定位 P 内部、D 内部以及跨 P/D connector 的失败阶段，检查所有实际端点的覆盖，
不能只拿两台 master 打流，也不能以“1P1D”假定只有两台服务器。
请特别核对组内 rank/版本、各机本地IP/网卡、实际资源文件消费者与 MC2 执行路径。
```

## 从示例到真实检测：不要跨级放行

| 阶段 | 在这些示例中怎么检查 | 不能据此下的结论 |
|---|---|---|
| S0 配置/环境 | 远端入口、source 链、版本、平台、掩码、设备映射、挂载、现有进程/端口 | 脚本无语法错不等于可建链 |
| S1/S2 控制面 | 在目标 namespace 核对地址/DNS，授权后做真实 TCP 握手、TCPStore 和 Gloo；先还原谁连接谁 | 未启动的生产端口不监听不是网络故障；TCP 通过不是 HCCL 通过 |
| S3/S4 组内数据面 | 按真实 TP/EP 等域执行 HCCL collective、逐卡打流；MC2 使用实际融合路径、同步与数值证据 | 一次 AllReduce/alltoall/aiv 不能替代 MC2；小组通过不代表全域通过 |
| S5/S6 跨实例 KV | E02/E05/E06 核实 proxy/connector；混部仅在实际启用远程 KV 时加入；授权后做真实传输与读回 | API health=200 不等于 KV 到达/可消费，单机 KV 通过不证明跨机链路 |
| 服务请求 | 已有服务或另获启动授权时，通过正确入口发送真实请求并关联 worker/KV 证据 | 预检不授权自动部署，未执行模型请求不写服务已验收 |

通信阶段定义见 [通信矩阵](communication-stages.md)，MC2 的具体算子覆盖见 [算子级检测](mc2-testing.md)。以下工具边界必须一起保留：

1. **单机**：技能可以指导 agent 现场读取和定向验证；当前 `preflight.py inspect/check` 配置仍要求 2～64 个节点，不能直接接收 E01/E02 的单节点配置。单机需使用现场版本支持的进程间/算子测试；缺少可用探针时写明未验证，不伪造第二个节点绕过限制。
2. **单机 MC2 与跨机 MC2 不混淆**：现有内置 MC2 用例要求跨节点，且验收真实不同宿主身份；同宿主 P/D、多个容器或 SSH 别名不能满足此条件。单机算子结果另记录组/设备、调用模式、数值与重复执行证据，不填跨节点 PASS。
3. **多机子域**：`groups` 引用节点，并使用节点条目内全部 `devices`；不会自动从 DP/TP 参数展开生产子域。一机多 TP 子组、同宿主多个容器或 P/D 环境不同，需要独立检测计划/配置或现场专用探针；不能用一个“全卡大组”取代所有子组验证。跨节点 MC2 也不能代替真实的单节点 TP 算子测试。
4. **探针配置不等于部署配置**：`examples/cluster.json` 是预检工具结构示例，不是上述 vLLM 启动配置。网卡、卡映射、节点和获准的测试端口由现场生成；`pairs` 只做所选两节点的卡对隔离，不执行生产 P→D KV 协议。
5. **两种 launcher 参数名不同**：E03/E04 原生参数是 `--data-parallel-start-rank`；E05/E06 外部 launcher 参数是 `--dp-rank-start`，模板收到当前实例 rank 后用 `--data-parallel-rank`。按完整调用链分析，不因名称相似就互换。见 [官方组件契约](official-deployment-recipes.md#可直接定位的独立脚本)。

最终报告应逐域列出“节点/设备 → 计划或运行时配置 → 首个异常阶段 → 证据 → 未覆盖范围”。所有本页示例只用于指导本次现场分析，不能承诺真实服务必定拉起。

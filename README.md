# Ascend 多机通信预检与现场排障 Skills

面向 Ascend A3 / A5 与 vLLM-Ascend，提供两个并列场景：服务启动前的通信预检，以及建链失败后的现场排障。用户提供一组服务器连接信息、各节点容器、工作目录和远端部署脚本，由具备相应访问能力的 agent 直接连接现场。这里“提供脚本”指提供服务器上的路径，不要求上传到本地，也不限定服务器数量。

预检从计划部署配置与现场环境出发，按通信阶段验证；排障从现有日志、实际 worker、容器和网络/设备状态定位失败阶段，再定向验证。两者共用 DNS/TCPStore/Gloo/HCCL、逐卡检测、官方 hccl_test 配方、MC2 算子级测试、KV 验收接口和辅助脚本分析。技能指引不绑定某个 agent 产品，命令行工具也可独立使用。

这是可运行的初版工具与中文技能库，不是“检测通过就保证模型必定启动”的承诺。本次开发机为 Windows、无 NPU；本机测试结果见 [验证记录](docs/validation.md)。A3/A5 上板、真实 Gloo/HCCL、MC2、KV 数据通路仍需现场验收。

## 导航

| 要解决的问题 | 入口 |
|---|---|
| 让 agent 进行通信预检或现场排障 | [技能入口与场景选择](skills/ascend-multinode-comm/SKILL.md) |
| 服务启动前直接连接服务器预检 | [预检流程](skills/ascend-multinode-comm/SKILL.md#启动前通信预检) |
| 给出服务器、容器、工作目录、远端脚本 | [共用连接信息与现场排障指引](skills/ascend-multinode-comm/references/remote-server-audit.md) |
| 辅助核对部署脚本里的配置错误 | [脚本分析规则](skills/ascend-multinode-comm/references/deployment-script-audit.md) |
| 混部、分离、池化分别在何时通信 | [分阶段通信矩阵](skills/ascend-multinode-comm/references/communication-stages.md) |
| A3/A5 平台、镜像、HCCS/vNIC 与分组边界 | [平台识别与分支检查](skills/ascend-multinode-comm/references/platform-a3-a5.md) |
| HCCS / RoCE / UBoE / fullmesh 与拓扑文件 | [拓扑和文件审计](skills/ascend-multinode-comm/references/topology-and-files.md) |
| 对本次选定节点做官方打流、逐卡检测、MC2 验证 | [参数化 HCCL 检测指南](skills/ascend-multinode-comm/references/hccl-testing.md) |
| 检测真正的 MC2 融合算子、MoE Dispatch/Combine | [MC2 算子级流程、内置探针与扩展契约](skills/ascend-multinode-comm/references/mc2-testing.md) |
| 仿真能验证什么，什么不能放行 | [仿真与分级验收](skills/ascend-multinode-comm/references/simulation-and-gates.md) |
| 历史故障复盘 | [现场坑位与定位](skills/ascend-multinode-comm/references/failure-playbook.md) |
| 先从 vLLM-Ascend 官网查 A3/A5 部署脚本 | [官方配方、固定源码与审计重点](skills/ascend-multinode-comm/references/official-deployment-recipes.md) |
| 提供远端成功部署脚本，提炼平台/版本经验 | [成功样本的选择、读取和归档](skills/ascend-multinode-comm/references/known-good-deployments.md) |

## 快速使用

两个场景都直接使用远端部署脚本。节点按实际数量列出，共用设置和节点差异分别说明；角色、TP/DP/EP 分组和版本等优先由 agent 从远端发现，不要求用户先制作完整配置清单。认证和更多字段见 [连接信息模板](skills/ascend-multinode-comm/references/remote-server-audit.md)。

密码认证时，登录密码通过当前环境支持的安全凭据输入渠道提供，或由用户在自己的 SSH 终端输入；不要把密码、私钥正文贴进聊天或写进配置/报告。没有安全交互渠道时，先由用户准备 agent 实际可用的 SSH 登录，不宣称已有工具可以自动接收密码。

### 场景一：服务启动前通信预检

```text
准备在这些服务器上部署服务，请直接连接现场做启动前通信预检。
服务器：逐项提供 IP/SSH 别名、端口、用户名、认证方式。
各节点容器：xxx；工作目录：xxx；服务部署脚本：xxx。
工作目录和脚本都在服务器的指定容器内；相对脚本路径相对于该工作目录。
服务尚未启动；请从远端脚本分析混部/PD 分离/池化及实际通信组。
先检查脚本与现场网卡、IP、路由、设备和容器配置，指出可能影响建链的问题。
主动探针、临时监听器及占卡打流前，请确认测试范围、空闲卡、端口和时间窗。
不要执行服务部署脚本；请报告已验证阶段、失败原因和仍需启动后验收的项目。
```

预检顺序：核实目标运行上下文与计划部署 → 还原通信关系、检查脚本 → 获取网卡/IP 和设备/拓扑证据 → 在授权范围运行分阶段探针 → 报告覆盖范围、异常与剩余验收项。服务未启动、没有 worker 或故障日志属于正常前提；生产端口尚未监听不能直接判为网络故障。预检不授权启动完整模型服务，实际 KV/MC2 或模型请求未执行时继续标为未验证。

### 场景二：服务当前建链失败，现场排障

```text
这些服务器上的服务目前建链失败，请直接连接现场排查。
服务器：逐项提供 IP/SSH 别名、端口、用户名、认证方式。
各节点容器：xxx；工作目录：xxx；服务部署脚本：xxx。
工作目录和脚本都在服务器的指定容器内；相对脚本路径相对于该工作目录。
错误/日志位置：已知则提供，未知请从当前容器和远端脚本查找。
请先保留现有故障现场，修改、重启或新增占卡测试前说明影响并确认。
```

排障顺序：连接并核实各节点执行上下文 → 关联现有日志的最后成功/首个失败点 → 对照脚本与实际 worker、网络/设备状态 → 选择最小现场验证 → 给出原因、证据、最小修复和复测建议。不会拿到远端路径后要求先下载上传，也不会以静态扫描或一份通用检查清单代替实际排查。

普通取证不修改配置、重启或重跑部署脚本。新增监听器、通信测试进程和 NPU 打流需明确空闲资源与授权；用户明确只读时保持只读。整个流程使用可用的 SSH/服务器连接能力，现有 `preflight.py` 则服务于后续受控测试，其 `BatchMode=yes` 不支持直接弹出密码输入。

### 辅助场景：明确只分析本地附件或已采集文本

只有用户选择本地分析，或远端取证已生成必要本地文本时，才使用这个入口。它不是远端预检或排障的前置条件。助手核对调用链、变量生效顺序与通信域，指出文件行号、影响阶段、成立条件和修复建议；工具自动解析静态 shell 子集，复杂 shell/Python/Compose/K8s 由助手继续分析。

```bash
cd skills/ascend-multinode-comm
python scripts/audit_deployment.py --root examples/audit-demo \
  --manifest examples/audit-demo/manifest.json --out reports/audit-demo.json
```

此样例故意包含错误，预期退出 1，并报告重复 HCCL_IP、DP 区间重叠、非法端口。工具生成 JSON + 中文 Markdown。审计真实上传目录时替换 root；manifest 可省略，分组关系明确后再做跨节点比较。工具不执行上传脚本，静态无报错也不会给出建链 PASS。报告和真实上传内容不要提交仓库。

### 共用工具：预检与经授权的定向验证

控制端：Python 3.10+、OpenSSH。被测环境：Linux、bash、Python 3.10+、iproute2；collective 需要当前服务使用的 torch/torch_npu/CANN。无需在控制端安装 torch。

```bash
cd skills/ascend-multinode-comm
cp examples/cluster.json examples/cluster.local.json
# 编辑真实容器名、workdir、可选 container_user、卡列表、环境脚本、业务 CIDR 和空闲测试端口。
# env_scripts 在目标容器内 source；宿主机上的路径不自动出现在容器里。
python scripts/preflight.py inspect --config examples/cluster.local.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.local.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope primitives
```

`inspect` 只有发现阶段，退出码通常为 2（未完整验证），不是执行错误。`check` 默认也保留 `model_e2e=UNVERIFIED`，基础通过后用 `gate --scope primitives` 查看基础范围；不能把它叫作服务完整验收。服务起来后需同镜像、同配置的真实请求以及相应 KV/MC2 适配器。

`examples/cluster.json` 仅展示结构：按现场增删 nodes/groups，填写真实 SSH 目标、容器、工作目录和选定空闲卡；`[0]` 只是最小卡列表示例。SSH 占位符未替换会在连接前报错。示例不设置现场 CIDR、环境脚本路径或 fullmesh；根据当前证据填写 CIDR/地址、必要 env_scripts 和 environment，不能沿用文档中的节点身份。`platform: auto` 保守识别实时型号；未知不会自动认作 A3 或 A5，声明平台也不能代替硬件证据。

两机所有选定卡对：

```bash
python scripts/preflight.py pairs --config examples/cluster.local.json --out reports/pairs.json
python scripts/preflight.py gate --report reports/pairs.json --scope pairs
```

8×8 会依次运行 64 个两 rank 通信域；耗时显著，先单卡验证，再选空闲窗口全量运行。每个卡对校验 AllReduce、AllGather、AllToAll 的数值。它证明的是逻辑卡对可通信，不证明两卡之间有一根直连光纤。

### MC2 通算融合算子单独检测

不仅测试普通 AllGather/AllToAll，还按算子列出 MC2 cases：

- 已接入三类真实 API 调用：Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter。内置探针只覆盖非量化 eager 小形状，必须先核对当前芯片/版本/组网支持，不宣称所有 A3/A5 组合都可运行。
- AllToAll-Matmul、Matmul-AllToAll、分组/量化融合、MoE Dispatch/Combine、Fused MoE/MegaMoE、图模式使用各自的版本化测试适配器；由 agent 在现场查找已有测试或按确认的 API 补齐，不以普通 collective 替代。
- 每个算子记录通信域、rank/卡映射、执行阶段、重复次数、数值校验与异常；一项通过不会覆盖另一项失败或未测。

将 [MC2 清单模板](skills/ascend-multinode-comm/examples/mc2-cases.json) 按 [MC2 指南](skills/ascend-multinode-comm/references/mc2-testing.md) 合入本次配置并核实资源后，运行 `check`，再用 `gate --scope mc2` 查看显式算子清单是否完整通过。`primitives` 通过不代表 MC2 通过；MC2 通过也不替代真实模型请求和 PD/KV 验收。

## 配置要点

- `nodes[].platform` 可为 `auto`（默认）、`A3`、`A5`，用于与实时型号比对；不指定镜像、卡数或算法默认值。身份矛盾或同组混合平台会暂停主动测试；识别不全可以执行通用基础探针，但不会执行 MC2 或通过 mc2/service gate。独立 P/D 组的平台可不同，跨平台 KV 支持仍需另证。详见平台指南。
- `nodes[].ssh` 只用于管理入口，可用已有 SSH config 别名。没有密码字段，不放宽 host key 校验；首次主机认证由用户完成。
- `nodes[].container` 不填即宿主机；工具不新建容器。`env_scripts` 在该运行环境内生效。
- `nodes[].workdir` 是该运行环境内的 Linux 绝对目录，在 source 和探针启动前生效。`container_user` 可指定已核实的容器服务用户名/UID（可带组），仅用于有 container 的节点；不自动切到 root。未设置时保留原来的执行上下文，不能声称已与 worker 对齐。
- `nodes[].ssh: local` 只支持当前 Linux 宿主执行，不进入容器；需要容器就使用明确 SSH 目标。现场诊断节点数不固定；现有主动探针单次 2～64 节点，`pairs` 一次两个节点仅为卡对隔离工具的限制。
- CPU-only 存储/入口节点可设 `role: store` 或 `role: router`，`devices: []`，不加入模型 groups。
- `devices` 是 torch 在当前可见设备掩码下的逻辑编号；可选 `physical_devices` 才是 hccn 查询编号。通过 npu-smi 映射核对，不能默认二者相同。
- `data_ip: auto` 配合现场确认的 `fabric_cidr` 主动找唯一 UP 网卡；多解/无解报错。不要由管理地址尾号猜业务地址。
- `groups` 是待验收通信域列表。PD 分离中 P 与 D 分开定义，必要时为 TP/EP/PP 子域分别建配置；工具不会从模型参数自动推断所有子域。
- `environment` 可设置 `HCCL_ALGO=level0:fullmesh`、`HCCL_BUFFSIZE`、`HCCL_OP_EXPANSION_MODE` 等；不按平台写入默认值，也不会默认写 HCCL 端口范围。网卡/IP 自动按节点设置；P/D 环境不同需分配置测试，不能用全局 environment 覆盖组间差异。
- `tcp_ports` 是明确允许临时监听的测试端口，默认两个探针端口不覆盖所有生产端口。扩展为真实服务端口前确保没有在线服务占用；动态分配端口仍需真实 connector 验证。
- `mode` 可选 `colocated` / `disaggregated` / `pooled`。混部也可能启用远程 KV，见通信指南，不能只凭模式名跳过 KV。
- `adapters` 是真实版本的自定义可执行程序；具体契约见验收指南。没有适配器不会自动填 PASS。
- `require_mc2: true` 配合 `mc2_cases` 列出每个需要验证的算子/通信组/模式。builtin 和逐 case adapter 可混合，不能与旧式 `adapters[].stage=mc2` 混用；缺失 case、资源或版本证据不应放行。

## 结果与安全边界

退出码：0=所声明完整范围通过；1=检测失败；2=缺少证据/未做。`gate` 默认拒绝超过一小时的报告；配置、镜像、网卡、拓扑、卡分配或占用变化后必须重测。配置指纹只是追溯信息，不是防篡改签名。

工具会创建短时监听器、NPU collective 和自有子进程，需要用户授权的节点、空闲卡与端口。它不改防火墙、路由、时钟、拓扑文件，不杀用户服务。SSH 中断后远端 watchdog 在超时上限内回收自有 worker；内核不可中断任务、MPI 外部远端 daemon 等不能承诺即时清理，需现场核验。

报告可能包含 IP、文件摘要、进程错误和拓扑信息，`reports/`、`*.local.json`、日志不入库。发布示例仅用待填写占位符、通用节点名或测试用文档地址，不是预设待连接目标；执行目标必须来自用户本次清单。

## 需要提供很多成功脚本吗

不需要。先使用 [官方配方索引](skills/ascend-multinode-comm/references/official-deployment-recipes.md)：首批整理 A3/A5 多节点混部、Mooncake/GLM-5/DeepSeek-V4 PD 分离、Mooncake 与 Memcache 池化的 7 类主要配方，附官网、固定源码及实际脚本定位。基线是 v0.23.0 官方提交，具体平台和依赖边界逐项保留；A2 混部示例与旧 EP 教程单列参考，不冒充 A3/A5 现场成功。

官方基线不能覆盖的变体，再补充已有代表案例即可。给服务器连接信息、容器、工作目录和远端入口，最好附成功时间与真实请求日志路径，其余依赖/版本由 agent 在授权范围发现；不要求用户先整理或上传整套脚本。

样本用于提炼有条件的检查规则，不直接复制成默认模板。进程启动或 health=200 不代表 KV/MC2 已执行，历史成功也不保证当前环境可用。原始脚本/日志不自动推送仓库，密码不进聊天；具体字段与处理边界见 [成功部署样本指引](skills/ascend-multinode-comm/references/known-good-deployments.md)。

## 在不同 agent 中使用

保留 `skills/ascend-multinode-comm` 整个目录，包括 `SKILL.md`、`scripts/`、`references/` 和 `examples/`：

- agent 支持加载 `SKILL.md` 技能包时，按该产品的技能加载机制添加本目录；安装位置和触发方式以该产品为准，不要求统一的目录或调用语法。
- 没有技能加载器但支持读文件时，将本目录交给 agent，要求先读取 `SKILL.md`，按任务选读引用文档，再使用自身可用的 SSH/命令执行工具。无需把所有参考资料一次性粘贴进提示词。
- 只有对话、不能读取本地文件或连接服务器的 agent，不能独立完成现场预检/排障。应说明缺失能力，由用户选择提供可用访问方式，或改为分析脱敏采集结果；不得声称已连接、已实测。

可用的通用调用说明：

```text
请读取提供的 ascend-multinode-comm/SKILL.md，并按我的任务选择预检或排障流程。
使用你当前实际可用的服务器连接和命令执行能力，在指定远端容器/目录内检查。
节点、容器、脚本、认证方式和测试范围见本次信息；缺少能力或权限请明确指出。
```

Codex 只是可选宿主之一；该宿主的个人技能目录 `.codex/skills/` 是一种安装示例，不是本仓库通用路径。其他 agent 不需要此目录，也不依赖 Codex 专属 API。具体产品的自动发现、认证交互与远端执行兼容性仍需在所用环境核实，本库未逐一验收。

也可以不通过 agent，直接运行上面的 Python CLI；运行前仍需准备 SSH、目标环境和测试授权。

## 本地测试

```bash
# 在仓库根目录运行，无第三方依赖
python -m unittest discover -s tests -v
```

本仓库以中文为主，参数名/协议名保留英文。没有引入旧工作区的密码、聊天原文或现场日志，也没有修改原来的服务启动脚本。

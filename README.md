# Ascend 多机现场建链排障与预检 Skills

面向 A5 / vLLM-Ascend，主场景是：用户提供一组服务器连接信息、各节点容器、工作目录和远端部署脚本，服务当前建链失败，由技能直接连接现场排查。这里“提供脚本”指提供服务器上的路径，不要求上传到本地，也不限定服务器数量。

从现有日志、实际 worker、容器和网络/设备状态定位失败阶段，再按需要进行 DNS/TCPStore/Gloo/HCCL、逐卡或 PD/KV 定向验证。也保留服务启动前预检、官方 hccl_test 配方、MC2/KV 验收接口，以及辅助静态脚本分析。

这是可运行的初版工具与中文技能库，不是“检测通过就保证模型必定启动”的承诺。本次开发机为 Windows、无 NPU；本机测试结果见 [验证记录](docs/validation.md)。A5 上板、真实 Gloo/HCCL、MC2、KV 数据通路仍需现场验收。

## 导航

| 要解决的问题 | 入口 |
|---|---|
| 让 Codex 现场排查建链失败 | [SKILL.md](skills/ascend-multinode-comm/SKILL.md) |
| 给出服务器、容器、工作目录、远端脚本后开始排查 | [主流程与连接信息模板](skills/ascend-multinode-comm/references/remote-server-audit.md) |
| 辅助核对部署脚本里的配置错误 | [脚本分析规则](skills/ascend-multinode-comm/references/deployment-script-audit.md) |
| 混部、分离、池化分别在何时通信 | [分阶段通信矩阵](skills/ascend-multinode-comm/references/communication-stages.md) |
| RoCE / UBoE / fullmesh 与拓扑文件 | [拓扑和文件审计](skills/ascend-multinode-comm/references/topology-and-files.md) |
| 对本次选定节点做官方打流、逐卡检测、MC2 验证 | [参数化 HCCL 检测指南](skills/ascend-multinode-comm/references/hccl-testing.md) |
| 仿真能验证什么，什么不能放行 | [仿真与分级验收](skills/ascend-multinode-comm/references/simulation-and-gates.md) |
| 历史故障复盘 | [现场坑位与定位](skills/ascend-multinode-comm/references/failure-playbook.md) |

## 快速使用

### 默认场景：指定服务器上的服务正在建链失败

直接这样描述即可，节点按实际数量列出，共用设置和节点差异分别说明：

```text
这些服务器上的服务目前建链失败，请直接连接现场排查。
服务器：逐项提供 IP/SSH 别名、端口、用户名、认证方式。
各节点容器：xxx；工作目录：xxx；服务部署脚本：xxx。
工作目录和脚本都在服务器的指定容器内；相对脚本路径相对于该工作目录。
错误/日志位置：已知则提供，未知请从当前容器和远端脚本查找。
请先保留现有故障现场，修改、重启或新增占卡测试前说明影响并确认。
```

角色、TP/DP/EP 分组、日志重定向和版本等优先由技能从远端发现，不要求用户先制作完整配置清单。认证和更多字段见 [现场排障指引](skills/ascend-multinode-comm/references/remote-server-audit.md)。

密码认证时，登录密码通过当前环境支持的安全凭据输入渠道提供，或由用户在自己的 SSH 终端输入；不要把密码、私钥正文贴进聊天或写进配置/报告。没有安全交互渠道时，先由用户准备可用 SSH 登录，不宣称已有工具可以自动接收密码。

技能的工作顺序是：连接并核实各节点执行上下文 → 关联现有日志的最后成功/首个失败点 → 对照脚本与实际 worker、网络/设备状态 → 选择最小现场验证 → 给出原因、证据、最小修复和复测建议。不会拿到远端路径后要求先下载上传，也不会以静态扫描或一份通用检查清单代替实际排查。

普通取证不修改配置、重启或重跑部署脚本。新增监听器、通信测试进程和 NPU 打流需明确空闲资源与授权；用户明确只读时保持只读。整个流程使用可用的 SSH/服务器连接能力，现有 `preflight.py` 则服务于后续受控测试，其 `BatchMode=yes` 不支持直接弹出密码输入。

### 辅助场景：明确只分析本地附件或已采集文本

只有用户选择本地分析，或远端取证已生成必要本地文本时，才使用这个入口。它不是现场排障的前置条件。助手核对调用链、变量生效顺序与通信域，指出文件行号、影响阶段、成立条件和修复建议；工具自动解析静态 shell 子集，复杂 shell/Python/Compose/K8s 由助手继续分析。

```bash
cd skills/ascend-multinode-comm
python scripts/audit_deployment.py --root examples/audit-demo \
  --manifest examples/audit-demo/manifest.json --out reports/audit-demo.json
```

此样例故意包含错误，预期退出 1，并报告重复 HCCL_IP、DP 区间重叠、非法端口。工具生成 JSON + 中文 Markdown。审计真实上传目录时替换 root；manifest 可省略，分组关系明确后再做跨节点比较。工具不执行上传脚本，静态无报错也不会给出建链 PASS。报告和真实上传内容不要提交仓库。

### 启动前预检，或现场排障需要的受控验证

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

`examples/cluster.json` 仅展示结构：按现场增删 nodes/groups，填写真实 SSH 目标、容器、工作目录和选定空闲卡；`[0]` 只是最小卡列表示例。SSH 占位符未替换会在连接前报错。示例不设置现场 CIDR、环境脚本路径或 fullmesh；根据当前证据填写 CIDR/地址、必要 env_scripts 和 environment，不能沿用文档中的节点身份。

两机所有选定卡对：

```bash
python scripts/preflight.py pairs --config examples/cluster.local.json --out reports/pairs.json
python scripts/preflight.py gate --report reports/pairs.json --scope pairs
```

8×8 会依次运行 64 个两 rank 通信域；耗时显著，先单卡验证，再选空闲窗口全量运行。每个卡对校验 AllReduce、AllGather、AllToAll 的数值。它证明的是逻辑卡对可通信，不证明两卡之间有一根直连光纤。

## 配置要点

- `nodes[].ssh` 只用于管理入口，可用已有 SSH config 别名。没有密码字段，不放宽 host key 校验；首次主机认证由用户完成。
- `nodes[].container` 不填即宿主机；工具不新建容器。`env_scripts` 在该运行环境内生效。
- `nodes[].workdir` 是该运行环境内的 Linux 绝对目录，在 source 和探针启动前生效。`container_user` 可指定已核实的容器服务用户名/UID（可带组），仅用于有 container 的节点；不自动切到 root。未设置时保留原来的执行上下文，不能声称已与 worker 对齐。
- `nodes[].ssh: local` 只支持当前 Linux 宿主执行，不进入容器；需要容器就使用明确 SSH 目标。现场诊断节点数不固定；现有主动探针单次 2～64 节点，`pairs` 一次两个节点仅为卡对隔离工具的限制。
- CPU-only 存储/入口节点可设 `role: store` 或 `role: router`，`devices: []`，不加入模型 groups。
- `devices` 是 torch 在当前可见设备掩码下的逻辑编号；可选 `physical_devices` 才是 hccn 查询编号。通过 npu-smi 映射核对，不能默认二者相同。
- `data_ip: auto` 配合现场确认的 `fabric_cidr` 主动找唯一 UP 网卡；多解/无解报错。不要由管理地址尾号猜业务地址。
- `groups` 是待验收通信域列表。PD 分离中 P 与 D 分开定义，必要时为 TP/EP/PP 子域分别建配置；工具不会从模型参数自动推断所有子域。
- `environment` 可设置 `HCCL_ALGO=level0:fullmesh` 等；不会默认写 HCCL 端口范围，避免覆盖版本行为。网卡/IP 自动按节点设置。
- `tcp_ports` 是明确允许临时监听的测试端口，默认两个探针端口不覆盖所有生产端口。扩展为真实服务端口前确保没有在线服务占用；动态分配端口仍需真实 connector 验证。
- `mode` 可选 `colocated` / `disaggregated` / `pooled`。混部也可能启用远程 KV，见通信指南，不能只凭模式名跳过 KV。
- `adapters` 是真实版本的自定义可执行程序；具体契约见验收指南。没有适配器不会自动填 PASS。

## 结果与安全边界

退出码：0=所声明完整范围通过；1=检测失败；2=缺少证据/未做。`gate` 默认拒绝超过一小时的报告；配置、镜像、网卡、拓扑、卡分配或占用变化后必须重测。配置指纹只是追溯信息，不是防篡改签名。

工具会创建短时监听器、NPU collective 和自有子进程，需要用户授权的节点、空闲卡与端口。它不改防火墙、路由、时钟、拓扑文件，不杀用户服务。SSH 中断后远端 watchdog 在超时上限内回收自有 worker；内核不可中断任务、MPI 外部远端 daemon 等不能承诺即时清理，需现场核验。

报告可能包含 IP、文件摘要、进程错误和拓扑信息，`reports/`、`*.local.json`、日志不入库。发布示例仅用待填写占位符、通用节点名或测试用文档地址，不是预设待连接目标；执行目标必须来自用户本次清单。

## 安装技能与测试

复制 `skills/ascend-multinode-comm` 整个目录到个人 `.codex/skills/`；保留 scripts、references、examples。

```bash
# 在仓库根目录运行，无第三方依赖
python -m unittest discover -s tests -v
```

本仓库以中文为主，参数名/协议名保留英文。没有引入旧工作区的密码、聊天原文或现场日志，也没有修改原来的服务启动脚本。

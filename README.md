# Ascend 多机通信预检 Skills

面向 A5 / vLLM-Ascend，把多机服务启动失败前移到分层预检：发现网卡与 IP，区分 Host 控制面和 NPU 数据面，测 DNS、TCPStore、Gloo、HCCL 与逐卡通信，保留官方 hccl_test 配方，并为 PD/KV 池化和 MC2 留出真实版本验收入口。

这是可运行的初版工具与中文技能库，不是“检测通过就保证模型必定启动”的承诺。本次开发机为 Windows、无 NPU；本机测试结果见 [验证记录](docs/validation.md)。A5 上板、真实 Gloo/HCCL、MC2、KV 数据通路仍需现场验收。

## 导航

| 要解决的问题 | 入口 |
|---|---|
| 让 Codex 按步骤做预检 | [SKILL.md](skills/ascend-multinode-comm/SKILL.md) |
| 上传一套部署脚本，找影响建链的错误 | [脚本审计说明](skills/ascend-multinode-comm/references/deployment-script-audit.md) |
| 混部、分离、池化分别在何时通信 | [分阶段通信矩阵](skills/ascend-multinode-comm/references/communication-stages.md) |
| RoCE / UBoE / fullmesh 与拓扑文件 | [拓扑和文件审计](skills/ascend-multinode-comm/references/topology-and-files.md) |
| 18/19 官方打流、8×8 逐卡、MC2 | [HCCL 检测指南](skills/ascend-multinode-comm/references/hccl-testing.md) |
| 仿真能验证什么，什么不能放行 | [仿真与分级验收](skills/ascend-multinode-comm/references/simulation-and-gates.md) |
| 历史故障复盘 | [现场坑位与定位](skills/ascend-multinode-comm/references/failure-playbook.md) |

## 快速使用

### 上传脚本，只分析不执行

直接把多节点入口和配套配置交给技能，助手会先还原各节点/角色、调用链、变量生效顺序与通信域，指出具体文件行号、影响阶段、成立条件和修复建议。支持助手阅读 shell/Python/Compose/K8s 等；内置工具自动解析静态 shell 子集，复杂部分由助手继续分析。

```bash
cd skills/ascend-multinode-comm
python scripts/audit_deployment.py --root examples/audit-demo \
  --manifest examples/audit-demo/manifest.json --out reports/audit-demo.json
```

此样例故意包含错误，预期退出 1，并报告重复 HCCL_IP、DP 区间重叠、非法端口。工具生成 JSON + 中文 Markdown。审计真实上传目录时替换 root；manifest 可省略，分组关系明确后再做跨节点比较。工具不执行上传脚本，静态无报错也不会给出建链 PASS。报告和真实上传内容不要提交仓库。

### 在目标环境主动检测

控制端：Python 3.10+、OpenSSH。被测环境：Linux、bash、Python 3.10+、iproute2；collective 需要当前服务使用的 torch/torch_npu/CANN。无需在控制端安装 torch。

```bash
cd skills/ascend-multinode-comm
cp examples/cluster.json examples/cluster.local.json
# 编辑真实容器名、可见逻辑卡列表、环境脚本路径、业务 CIDR、空闲测试端口。
# env_scripts 在目标容器内 source；宿主机上的路径不自动出现在容器里。
python scripts/preflight.py inspect --config examples/cluster.local.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.local.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope primitives
```

`inspect` 只有发现阶段，退出码通常为 2（未完整验证），不是执行错误。`check` 默认也保留 `model_e2e=UNVERIFIED`，基础通过后用 `gate --scope primitives` 查看基础范围；不能把它叫作服务完整验收。服务起来后需同镜像、同配置的真实请求以及相应 KV/MC2 适配器。

两机所有选定卡对：

```bash
python scripts/preflight.py pairs --config examples/cluster.local.json --out reports/pairs.json
python scripts/preflight.py gate --report reports/pairs.json --scope pairs
```

8×8 会依次运行 64 个两 rank 通信域；耗时显著，先单卡验证，再选空闲窗口全量运行。每个卡对校验 AllReduce、AllGather、AllToAll 的数值。它证明的是逻辑卡对可通信，不证明两卡之间有一根直连光纤。

## 配置要点

- `nodes[].ssh` 只用于管理入口，可用已有 SSH config 别名。没有密码字段，不放宽 host key 校验；首次主机认证由用户完成。
- `nodes[].container` 不填即宿主机；工具不新建容器。`env_scripts` 在该运行环境内生效。
- CPU-only 存储/入口节点可设 `role: store` 或 `role: router`，`devices: []`，不加入模型 groups。
- `devices` 是 torch 在当前可见设备掩码下的逻辑编号；可选 `physical_devices` 才是 hccn 查询编号。通过 npu-smi 映射核对，不能默认二者相同。
- `data_ip: auto` 配合 `fabric_cidr` 主动找唯一 UP 网卡；多解/无解报错。不要由 141.* 地址尾号猜 172.* 地址。
- `groups` 是待验收通信域列表。PD 分离中 P 与 D 分开定义，必要时为 TP/EP/PP 子域分别建配置；工具不会从模型参数自动推断所有子域。
- `environment` 可设置 `HCCL_ALGO=level0:fullmesh` 等；不会默认写 HCCL 端口范围，避免覆盖版本行为。网卡/IP 自动按节点设置。
- `tcp_ports` 是明确允许临时监听的测试端口，默认两个探针端口不覆盖所有生产端口。扩展为真实服务端口前确保没有在线服务占用；动态分配端口仍需真实 connector 验证。
- `mode` 可选 `colocated` / `disaggregated` / `pooled`。混部也可能启用远程 KV，见通信指南，不能只凭模式名跳过 KV。
- `adapters` 是真实版本的自定义可执行程序；具体契约见验收指南。没有适配器不会自动填 PASS。

## 结果与安全边界

退出码：0=所声明完整范围通过；1=检测失败；2=缺少证据/未做。`gate` 默认拒绝超过一小时的报告；配置、镜像、网卡、拓扑、卡分配或占用变化后必须重测。配置指纹只是追溯信息，不是防篡改签名。

工具会创建短时监听器、NPU collective 和自有子进程，需要用户授权的节点、空闲卡与端口。它不改防火墙、路由、时钟、拓扑文件，不杀用户服务。SSH 中断后远端 watchdog 在超时上限内回收自有 worker；内核不可中断任务、MPI 外部远端 daemon 等不能承诺即时清理，需现场核验。

报告可能包含 IP、文件摘要、进程错误和拓扑信息，`reports/`、`*.local.json`、日志不入库。源码中的 18/19 是用户提供的历史示例，不是本次实测结果；若转公开仓库，应先将现场地址替换成文档地址。

## 安装技能与测试

复制 `skills/ascend-multinode-comm` 整个目录到个人 `.codex/skills/`；保留 scripts、references、examples。

```bash
# 在仓库根目录运行，无第三方依赖
python -m unittest discover -s tests -v
```

本仓库以中文为主，参数名/协议名保留英文。没有引入旧工作区的密码、聊天原文或现场日志，也没有修改原来的服务启动脚本。

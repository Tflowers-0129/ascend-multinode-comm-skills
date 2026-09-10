---
name: ascend-multinode-comm
description: 面向 Ascend/A5 与 vLLM-Ascend 的服务器现场建链排障。用户给出一组服务器连接信息、各节点容器、工作目录和远端部署脚本，服务当前建链失败时，直接 SSH 到现场，结合日志、实际进程配置、网络与设备状态分层定位 TCPStore/Gloo/HCCL、PD/KV 问题。也支持启动前预检与辅助脚本静态分析；提供脚本默认指服务器上的路径，不要求上传。
---

# 昇腾多机现场建链排障与预检

目标：解决用户指定服务器上正在发生的建链失败，定位到节点、容器、阶段、rank、链路或配置来源。默认先调查现有故障；只有尚未启动服务的预检请求，才从无模型探针开始。节点数量与通信分组来自现场，不限定为两台，也不默认所有节点配置相同。

## 先选择入口

| 用户意图 | 行为 |
|---|---|
| 这些服务器、容器、工作目录和部署脚本，当前建链失败，请排查 | 默认入口：读 [现场排障流程](references/remote-server-audit.md)，连接各目标，读取远端脚本、日志和实际状态，按失败阶段定位 |
| 服务启动前验证通信、打流、检测 HCCL | 按下方主动预检流程，明确节点、空闲卡、端口与时间窗 |
| 明确只分析本地附件、不连接服务器 | 辅助静态审计；不能据此诊断当前现场状态 |

用户在服务器场景中“提供脚本”即提供远端路径；不要要求先下载、上传、复制到本地，或先制作审计 manifest。若给出容器，工作目录默认按该容器内路径理解，脚本相对路径相对于该目录；核查实际存在性，必要时澄清宿主/容器边界，不擅自换一个同名文件。

如果用户仅要求完善技能/文档，不索要真实凭据、不连接历史服务器。实际排障时补齐缺失的 IP/SSH 别名、端口、用户名、认证方式、容器、工作目录和脚本路径。角色、并行参数、日志位置等优先从远端发现，只有会改变结论的歧义才问用户。密码经实际可用的安全输入渠道或用户自己的 SSH 终端提供，不写入聊天、配置或报告。

## 默认现场排障顺序

1. 连接目标服务器，确认各自现有容器、容器内用户、工作目录、远端入口及依赖；同名容器在不同宿主上不是同一实例。读取现有日志/退出状态，服务仍在时核对实际进程，不重新运行部署脚本来“看看报错”。
2. 对照 [通信阶段](references/communication-stages.md) 关联各节点/rank 的最后成功点、首个失败点及对端证据，区别 S0 环境、S1 store/DNS、S2 Gloo、S3/S4 HCCL/首算子、S5/S6 KV。读 [现场故障](references/failure-playbook.md) 辅助定位，不用最后一个 timeout 代替首因。
3. 将脚本声明与实际进程参数/环境、容器挂载、网卡/IP、路由、DNS、监听归属、设备/版本状态交叉验证。必须在故障服务的目标 namespace 中查；新 docker exec 的环境不自动等于运行中 worker 的环境。
4. 根据假设做最小验证，不机械跑完整预检。只读状态及有界解析查询按需执行；新增监听器、TCPStore/Gloo/HCCL 测试进程或占卡打流前，明确空闲资源和影响范围并取得对应授权。用 [拓扑与配置文件](references/topology-and-files.md) 和 [HCCL 检测](references/hccl-testing.md) 选择验证路径。未被验证的层继续标注未知。
5. 给出已确认原因/高概率假设、具体远端文件行号或日志/运行时证据、影响节点和最小修复建议；证据不足就继续范围内的检查，不以本地静态报告或通用检查清单结束。只有缺访问、缺关键事实或后续动作需新授权时停下并指出确切缺口。修改、重启与复测按用户后续授权进行。

## 辅助：脚本语义与静态检查

读 [脚本分析规则](references/deployment-script-audit.md) 还原 source/调用链、变量生效顺序和真实通信域。可直接分析远端带行号文本；`scripts/audit_deployment.py` 只在已有必要本地文本时提供线索，不是现场排障前置条件。复杂 Shell/Python/Compose/K8s 由助手继续语义分析。明确仅本地附件审计时不自动连接服务器。原文/现场报告不推送仓库。

```bash
python scripts/audit_deployment.py --root /path/to/uploaded-deployment \
  --manifest /path/to/audit-manifest.json --out reports/deployment-audit.json
```

manifest 可省略；没有明确分组时不跨文件断言 rank/端口冲突。助手可根据脚本证据生成本地清单并标出推断，不能把不同版本/备选方案都当成同时启动的节点。

## 启动前预检 / 经授权的定向复现

1. 先读 [通信阶段](references/communication-stages.md)，确认混部/分离/池化、P/D/存储角色、实际 TP/PP/DP/EP 分组、镜像与 CANN 版本。不同 P、D 实例不要硬塞进一个生产 HCCL 通信域。
2. 读 [拓扑与配置文件](references/topology-and-files.md)，区分管理 IP、Host 控制面 IP、NPU 数据面地址；RoCE/UB 是传输线索，fullmesh 是拓扑或算法线索，不能三选一。
3. 从 `examples/cluster.json` 制作本地配置，保留自动探测，指定业务 CIDR 或明确 data_ip 消除多网卡歧义。先 `inspect`，再经用户确认空闲卡、端口与时间窗后 `check`。工具不自动登录历史地址、不复用聊天里的密码。
4. 对每一个真实通信域运行 TCPStore、Gloo、HCCL；本次选定节点的逐卡或官方打流见 [HCCL 检测](references/hccl-testing.md)。目标来自当前清单，不复用任何历史地址。纯 TCP 不算 HCCL 通过，alltoall/aiv 不算 MC2 通过。
5. 按实际 connector 执行 P→D KV 或池化适配器；未提供真实适配器则保持 `UNVERIFIED`。读 [现场故障](references/failure-playbook.md) 对照证据，不从错误码直接猜防火墙。
6. 输出报告时必须列出实测/未测、测试时刻、版本、节点和卡映射、阶段失败与建议。需要仿真时读 [仿真与验收边界](references/simulation-and-gates.md)。

## 工具

`scripts/preflight.py`：Python 标准库控制器；通过 SSH 在宿主机或现有容器内运行同一版本的探针。探针代码通过 stdin 传输，不改远端安装、不写拓扑文件。主动检测使用短时监听器和进程，远端 watchdog 在对应命名空间内限制生命周期。

```bash
python scripts/preflight.py inspect --config examples/cluster.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope primitives
```

`scripts/hccl_bench.py`：从重复的 `--host` 参数生成 MPI hostfile 并计算总 rank 数；必须指定实际 `--directory`，以及 `--source` 或 `--inherit-env`。`--execute` 才运行，默认小流量，1G 必须显式选择。`scripts/preflight.py pairs` 每次隔离一对选定节点，按各自卡列表做笛卡尔积；节点身份、卡数都来自本次配置，不把多 rank collective 冒充独立卡对覆盖。

## 关键判断

- TCP 全为 ESTABLISHED 仍可能卡在 TCPStore 的反向 DNS；必须同时测 getnameinfo 和真正的 TCPStore/Gloo 初始化。
- 自动探测没有唯一业务地址时停止，不按管理 IP 尾号拼业务 IP，不默认第一块网卡。
- 优先在服务将要运行的容器、用户、环境脚本下检测。宿主机通过不能替代容器通过。
- `/etc/hccl_rootinfo.json` 默认不注入挂载；检测现有挂载并给出版本化判断。只有确认当前运行路径不消费该文件，才建议备份后移除挂载/旧文件。部分官方 950DT/HiXLEP 路径要求它，不能一概删除。
- `/etc/hixlep.json` 与 `/etc/hixlep/` 不能混为一谈；检查真实配置指向哪一个。不能捏造 JSON schema。
- `export HCCL_ALGO=level0:fullmesh` 只在用户指定或实际脚本有此设置时继承测试，不全局改写算法；配置信息不是物理 fullmesh 的证明。
- 不自动停止用户服务、清空 NPU、调整防火墙/路由、删除配置或增大安全权限。日志可能含私网拓扑，报告默认不进 Git。

## 安装与自检

把本技能目录复制到用户的 `.codex/skills/ascend-multinode-comm`；脚本和 examples 必须一起复制。仓库根目录执行 `python -m unittest discover -s tests -v`。硬件实测和 CPU/Linux 集成测试分别记录，不能把 mock 当作硬件验收。

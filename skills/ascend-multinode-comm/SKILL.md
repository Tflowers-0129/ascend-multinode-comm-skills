---
name: ascend-multinode-comm
description: 面向 Ascend A3/A5 与 vLLM-Ascend 的多机通信预检与现场排障。用户提供服务器连接信息、容器、工作目录和远端部署脚本后，按实时平台/版本及 HCCS/RoCE/UB 路径检查 TCPStore/Gloo/HCCL、MC2 与 PD/KV 通信；建链失败时结合日志、实际进程和网络设备状态定位原因。预检与排障并列，支持辅助脚本分析与成功案例提炼；脚本默认在服务器，不要求上传，不绑定特定 agent。
---

# 昇腾多机通信预检与现场排障

目标：在用户指定服务器上，启动前发现影响建链的配置与通信问题，或在建链失败后定位到节点、容器、阶段、rank、链路与配置来源。两者按用户意图选择，不默认已经发生故障。节点数量与通信分组来自现场，不限定为两台，也不默认所有节点配置相同。

本技能不依赖特定 agent 品牌或专属 API。使用当前 agent 实际具备的文件读取、SSH/服务器连接和命令执行能力；先核实能力与权限，缺少时明确报告，不能把文档步骤或本地分析冒充现场执行。

## 先选择入口

| 用户意图 | 行为 |
|---|---|
| 这些服务器、容器、工作目录和部署脚本，服务启动前验证通信 | 按下方“启动前通信预检”流程，从远端计划配置与现场环境出发，明确空闲卡、端口与时间窗；不要求故障日志或现有 worker |
| 这些服务器、容器、工作目录和部署脚本，当前建链失败，请排查 | 按下方“现场建链排障”流程及 [现场排障指引](references/remote-server-audit.md)，读取远端脚本、日志和实际状态，按失败阶段定位 |
| 明确只分析本地附件、不连接服务器 | 辅助静态审计；不能据此诊断当前现场状态 |
| 先找官网部署脚本、建立 A3/A5 参考基线 | 读 [官方配方索引](references/official-deployment-recipes.md)，按平台/模式/版本选择官网代码块和固定源码；无需先索要现场成功脚本，不执行部署 |
| 给出远端已成功部署的脚本，希望提炼检查规则 | 按 [成功样本指引](references/known-good-deployments.md) 只读提取有版本和验收证据的规则，不重跑服务，不把原始内容推送仓库 |

用户在服务器场景中“提供脚本”即提供远端路径；不要要求先下载、上传、复制到本地，或先制作审计 manifest。若给出容器，工作目录默认按该容器内路径理解，脚本相对路径相对于该目录；核查实际存在性，必要时澄清宿主/容器边界，不擅自换一个同名文件。

需要成功配置作参考而用户尚未提供时，先查 [官方配方](references/official-deployment-recipes.md)，再按缺口补充现场变体；官方示例和上游性能结果不等于用户现场验证。用户已给远端部署时仍优先核对现场，不以研究全部官方教程代替实际预检/排障。

如果用户仅要求完善技能/文档，不索要真实凭据、不连接历史服务器。实际预检或排障时补齐缺失的 IP/SSH 别名、端口、用户名、认证方式、容器、工作目录和脚本路径；连接信息、认证与定向读取边界见 [远端指引](references/remote-server-audit.md) 第 1、2、4 节。角色、并行参数、版本等优先从远端发现，只有会改变结论的歧义才问用户。密码经实际可用的安全输入渠道或用户自己的 SSH 终端提供，不写入聊天、配置或报告。意图不明确且会影响测试范围时确认是预检还是排障；不因为给了脚本就判定服务有故障。

## 启动前通信预检

1. 连接并核实每个目标的容器、服务用户、工作目录及远端脚本，读取入口/source 依赖而不执行部署脚本。按 [脚本分析规则](references/deployment-script-audit.md) 还原计划配置，并读 [通信阶段](references/communication-stages.md)，确认混部/分离/池化、P/D/存储角色、实际 TP/PP/DP/EP 分组、镜像与 CANN 版本。不同 P、D 实例不要硬塞进一个生产 HCCL 通信域。
2. 读 [A3/A5 平台分支](references/platform-a3-a5.md)，先核实实时型号、软件/镜像与卡映射，再核对计划网卡/IP、路由、端口占用、设备可见性及容器挂载。平台身份不等于版本/算子支持；未知不猜，同一通信组不混合平台试错。服务尚未启动时，没有 worker、故障日志或业务监听是正常前提，不判建链失败；计划参数与已实测状态分别记录。容器尚未运行或环境不齐则报告检查缺口，不擅自创建/启动容器或服务。
3. 读 [拓扑与配置文件](references/topology-and-files.md)，区分管理 IP、Host 控制面 IP、NPU 数据面地址；HCCS/RoCE/UB 与 fullmesh 属于不同维度，不互斥。A3 HCCS 按 vNIC/superpod/SDID 路径查证，不能套成 A5 UB 检查。从 `examples/cluster.json` 制作本地测试配置，以业务 CIDR 或明确 data_ip 消除多网卡歧义；目标与卡列表来自本次信息。先审阅必要 env_scripts，再运行 `inspect`；不得将部署脚本当成环境初始化脚本。
4. 确认空闲卡、允许临时监听的端口与时间窗后，以独立测试进程运行 `check`，按实际通信域验证 DNS/TCP、TCPStore、Gloo、HCCL。不能向未启动的生产端口建连失败就判网络不通，也不能占用线上通信组。逐卡或官方打流见 [HCCL 检测](references/hccl-testing.md)，纯 TCP 不算 HCCL 通过，alltoall/aiv 不算 MC2 通过。
5. 读 [MC2 算子级检测](references/mc2-testing.md)，从实际配置/代码识别 Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter、AllToAll 融合、MoE Dispatch/Combine 或 Fused MoE 等所用路径，分别建立 `mc2_cases`。内置三类非量化 eager 探针不能替代量化/图模式/MoE；其余由 agent 在现场定位或补齐版本化测试。P→D KV 与池化另用实际 connector 适配器，不与 MC2 合并为同一验收项。缺失时保持 `UNVERIFIED`；预检不自动授权拉起完整模型服务。
6. 输出已确认配置错误、条件风险、各阶段实测/未测、时刻、版本、节点/卡映射与后续动作。不把没有现存故障判成全部通过，不承诺预检通过就保证服务启动。需要仿真或分级放行时读 [仿真与验收边界](references/simulation-and-gates.md)。

## 现场建链排障

1. 连接目标服务器，确认各自现有容器、容器内用户、工作目录、远端入口及依赖；同名容器在不同宿主上不是同一实例。读取现有日志/退出状态，服务仍在时核对实际进程，不重新运行部署脚本来“看看报错”。
2. 对照 [通信阶段](references/communication-stages.md) 关联各节点/rank 的最后成功点、首个失败点及对端证据，区别 S0 环境、S1 store/DNS、S2 Gloo、S3/S4 HCCL/首算子、S5/S6 KV。读 [现场故障](references/failure-playbook.md) 辅助定位，不用最后一个 timeout 代替首因。
3. 按 [A3/A5 平台分支](references/platform-a3-a5.md) 核实型号与版本支持，将脚本声明与实际进程参数/环境、容器挂载、网卡/IP、路由、DNS、监听归属、设备/版本状态交叉验证。必须在故障服务的目标 namespace 中查；新 docker exec 的环境不自动等于运行中 worker 的环境。
4. 根据假设做最小验证，不机械跑完整预检。只读状态及有界解析查询按需执行；新增监听器、TCPStore/Gloo/HCCL/MC2 测试进程或占卡打流前，明确空闲资源和影响范围并取得对应授权。用 [拓扑与配置文件](references/topology-and-files.md)、[HCCL 检测](references/hccl-testing.md) 和 [MC2 算子检测](references/mc2-testing.md) 选择验证路径；保留测试环境与故障 worker 的差异。未被验证的层继续标注未知。
5. 给出已确认原因/高概率假设、具体远端文件行号或日志/运行时证据、影响节点和最小修复建议；证据不足就继续范围内的检查，不以本地静态报告或通用检查清单结束。只有缺访问、缺关键事实或后续动作需新授权时停下并指出确切缺口。修改、重启与复测按用户后续授权进行。

## 辅助：脚本语义与静态检查

预检与排障都可读 [脚本分析规则](references/deployment-script-audit.md) 还原 source/调用链、变量生效顺序和真实通信域。可直接分析远端带行号文本；`scripts/audit_deployment.py` 只在已有必要本地文本时提供线索，不是远端预检或排障前置条件。复杂 Shell/Python/Compose/K8s 由助手继续语义分析。明确仅本地附件审计时不自动连接服务器。原文/现场报告不推送仓库。

```bash
python scripts/audit_deployment.py --root /path/to/uploaded-deployment \
  --manifest /path/to/audit-manifest.json --out reports/deployment-audit.json
```

manifest 可省略；没有明确分组时不跨文件断言 rank/端口冲突。助手可根据脚本证据生成本地清单并标出推断，不能把不同版本/备选方案都当成同时启动的节点。

## 工具

`scripts/preflight.py`：Python 标准库控制器；通过 SSH 在宿主机或现有容器内运行同一版本的探针。探针代码通过 stdin 传输，不改远端安装、不写拓扑文件。主动检测使用短时监听器和进程，远端 watchdog 在对应命名空间内限制生命周期。

```bash
python scripts/preflight.py inspect --config examples/cluster.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope primitives
```

`scripts/hccl_bench.py`：从重复的 `--host` 参数生成 MPI hostfile 并计算总 rank 数；必须指定实际 `--directory`，以及 `--source` 或 `--inherit-env`。`--execute` 才运行，默认小流量，1G 必须显式选择。`scripts/preflight.py pairs` 每次隔离一对选定节点，按各自卡列表做笛卡尔积；节点身份、卡数都来自本次配置，不把多 rank collective 冒充独立卡对覆盖。

`preflight.py check` 在显式配置 `require_mc2: true` 和 `mc2_cases` 且平台身份核实时执行算子级测试，逐项写入 `mc2/<case-name>`；`gate --scope mc2` 要求平台身份、基础通信与全部显式 case 通过。配置模板见 `examples/mc2-cases.json`，须合入实际 cluster 并填写已核实的版本支持来源。

## 关键判断

- TCP 全为 ESTABLISHED 仍可能卡在 TCPStore 的反向 DNS；必须同时测 getnameinfo 和真正的 TCPStore/Gloo 初始化。
- 自动探测没有唯一业务地址时停止，不按管理 IP 尾号拼业务 IP，不默认第一块网卡。
- 优先在服务将要运行的容器、用户、环境脚本下检测。宿主机通过不能替代容器通过。
- A3/A5 先识别再路由，不从模型名、镜像 tag 或 DAV_2201 猜平台；成功样本是有条件的参考，不是前置门槛或通用默认配置。
- MC2 需要真实算子执行、同步、逐 rank 数值校验与重复调用证据；普通 collective 或单个融合算子通过不代表所有 MC2 路径通过。API 不存在/版本不支持与网络故障分开诊断，不静默回退到非融合实现。
- `/etc/hccl_rootinfo.json` 默认不注入挂载；检测现有挂载并给出版本化判断。只有确认当前运行路径不消费该文件，才建议备份后移除挂载/旧文件。部分官方 950DT/HiXLEP 路径要求它，不能一概删除。
- `/etc/hixlep.json` 与 `/etc/hixlep/` 不能混为一谈；检查真实配置指向哪一个。不能捏造 JSON schema。
- `export HCCL_ALGO=level0:fullmesh` 只在用户指定或实际脚本有此设置时继承测试，不全局改写算法；配置信息不是物理 fullmesh 的证明。
- 不自动停止用户服务、清空 NPU、调整防火墙/路由、删除配置或增大安全权限。日志可能含私网拓扑，报告默认不进 Git。

## 加载与自检

保留整个技能目录及 scripts、references、examples，按所用 agent 的技能加载机制添加；没有加载器但能读取文件的 agent 可直接读取本文件并按需打开引用资源，不假定统一安装路径或调用语法。工具也可作为 Python CLI 独立运行；需要控制端 Python 3.10+ 与 OpenSSH，目标环境和测试资源按相应参考文档准备。不同 agent 的认证与执行能力需现场核实，未提供相应能力时不能宣称完成远端预检或排障。

仓库根目录执行 `python -m unittest discover -s tests -v`。硬件实测和 CPU/Linux 集成测试分别记录，不能把 mock 当作硬件验收。

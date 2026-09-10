---
name: ascend-multinode-comm
description: 面向 Ascend/A5 与 vLLM-Ascend 的多机通信预检、部署脚本审计和分层排障。用户上传一套多节点部署脚本时，只读分析影响建链的错误并定位文件行号；需要主动检测时发现网卡/IP，检测 TCPStore/Gloo/HCCL、逐卡通信与拓扑。覆盖 PD 混部、分离、KV 池化，明确静态风险与实测边界。
---

# 昇腾多机通信预检

目标：在真实模型服务启动前，把失败尽量定位到具体节点、容器、网卡、阶段、rank 或卡对；基础通信成功不能替代真实服务验收。

## 上传部署脚本：先做只读审计

用户要求“分析这套部署脚本”“找影响建链的错误”时，读 [部署脚本审计](references/deployment-script-audit.md)，进入静态模式，不直接启动下述主动预检。

从上传目录/文件列表还原每个节点的入口、参数、source/调用链、namespace 与实际通信域。可用 `scripts/audit_deployment.py` 提取静态 shell 线索，再由助手读取工具未覆盖的 Python/Compose/K8s/复杂 shell 做语义分析；不要把未覆盖位置简单转交用户，也不要把扫描结果当最终结论。

输出：节点配置对照、通信依赖、按严重性排序的问题（文件行号、证据、影响阶段、成立条件、修复建议）、必须上机确认的项目。区分确定错误、条件风险、缺失信息；只分析不改脚本、不执行上传命令、不把上传原文/报告推送仓库。需要修改或主动测试时另按用户请求进行。

```bash
python scripts/audit_deployment.py --root /path/to/uploaded-deployment \
  --manifest /path/to/audit-manifest.json --out reports/deployment-audit.json
```

manifest 可省略；没有明确分组时不跨文件断言 rank/端口冲突。助手可根据脚本证据生成本地清单并标出推断，不能把不同版本/备选方案都当成同时启动的节点。

## 执行顺序

1. 先读 [通信阶段](references/communication-stages.md)，确认混部/分离/池化、P/D/存储角色、实际 TP/PP/DP/EP 分组、镜像与 CANN 版本。不同 P、D 实例不要硬塞进一个生产 HCCL 通信域。
2. 读 [拓扑与配置文件](references/topology-and-files.md)，区分管理 IP、Host 控制面 IP、NPU 数据面地址；RoCE/UB 是传输线索，fullmesh 是拓扑或算法线索，不能三选一。
3. 从 `examples/cluster.json` 制作本地配置，保留自动探测，指定业务 CIDR 或明确 data_ip 消除多网卡歧义。先 `inspect`，再经用户确认空闲卡、端口与时间窗后 `check`。工具不自动登录历史地址、不复用聊天里的密码。
4. 对每一个真实通信域运行 TCPStore、Gloo、HCCL；18/19 逐卡或官方打流见 [HCCL 检测](references/hccl-testing.md)。纯 TCP 不算 HCCL 通过，alltoall/aiv 不算 MC2 通过。
5. 按实际 connector 执行 P→D KV 或池化适配器；未提供真实适配器则保持 `UNVERIFIED`。读 [现场故障](references/failure-playbook.md) 对照证据，不从错误码直接猜防火墙。
6. 输出报告时必须列出实测/未测、测试时刻、版本、节点和卡映射、阶段失败与建议。需要仿真时读 [仿真与验收边界](references/simulation-and-gates.md)。

## 工具

`scripts/preflight.py`：Python 标准库控制器；通过 SSH 在宿主机或现有容器内运行同一版本的探针。探针代码通过 stdin 传输，不改远端安装、不写拓扑文件。主动检测使用短时监听器和进程，远端 watchdog 在对应命名空间内限制生命周期。

```bash
python scripts/preflight.py inspect --config examples/cluster.json --out reports/inventory.json
python scripts/preflight.py check --config examples/cluster.json --out reports/check.json
python scripts/preflight.py gate --report reports/check.json --scope primitives
```

`scripts/hccl_bench.py`：生成 MPI hostfile 与 hccl_test 计划；`--execute` 才运行。默认小流量，1G 历史配方必须显式选择。`scripts/preflight.py pairs`：对两台机器的显式可见卡列表做笛卡尔积，逐个两 rank HCCL collective，不把一个 16-rank collective 冒充 64 个独立卡对。

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

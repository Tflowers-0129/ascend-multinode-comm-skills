# 官方打流、逐卡矩阵与 MC2

## 从本次节点清单生成打流计划

节点、地址、卡数、CANN/hccl_test 路径都来自本次用户提供的信息和现场核查，不沿用历史服务器。先确定参与测试的通信域与发起节点，在该节点的目标容器/用户环境中准备 MPI；总 rank 数为各参与节点 slots 之和，不固定节点数量或每机卡数。

| 参数 | 如何确定 |
|---|---|
| 参与节点列表 | 每个节点的 MPI 目标主机名/IP 和本次选定的 slots；MPI 能否解析 SSH 别名需现场确认 |
| 发起位置 | 本次选定的参与节点、容器、用户和工作目录，不默认清单中的某个历史主机 |
| 环境初始化 | 当前版本实际可用的环境脚本，或明确沿用已准备好的环境 |
| 测试目录 | 当前安装的 hccl_test 目录；先核对二进制及 MPI/CANN 版本，不固定安装前缀或版本号 |
| rank/卡映射 | 本次可见逻辑卡、物理卡对应和空闲状态；slots 并不能证明用了预期物理卡 |
| 算法与尺寸 | 从现场进程/配置取得；默认小流量，fullmesh/AIV 和 1G 各自按需启用 |

以下是参数化的 MPICH 手工命令形态，不会替用户选择目标或生成生产配置。环境变量须先从现场填好，hostfile 用唯一的新文件绝对路径，总 rank 数与其 slots 总和一致。优先使用下方包装器自动计算，手工模式需自行核对。

```bash
: "${HCCL_ENV_SCRIPT:?请填写本次环境脚本路径}"
: "${HCCL_TEST_DIR:?请填写本次hccl_test目录}"
: "${HCCL_HOSTFILE:?请填写本次已核对的hostfile绝对路径}"
: "${HCCL_WORLD_SIZE:?请填写hostfile中slots总和}"
source "$HCCL_ENV_SCRIPT"
cd -- "$HCCL_TEST_DIR"
mpirun -f "$HCCL_HOSTFILE" -n "$HCCL_WORLD_SIZE" ./bin/alltoall_test -b 8 -e 1M -f 2
```

MPICH hostfile 中每个参与节点一行 `本次主机名或IP:本次slots`；Open MPI 格式为 `本次主机名或IP slots=本次slots`。这两种格式不能混用。`1M` 是小流量起点，不是固定生产参数。

只有现场本来使用 fullmesh 算法且当前版本适用时，才在测试进程环境中继承：

```bash
export HCCL_ALGO=level0:fullmesh
```

AIV 需要当前测试程序支持时添加 `-a aiv`。经确认有空闲资源和测试窗口后，才将 `-e 1M` 扩为 `-e 1G` 等大小；AllToAll 的内存/带宽开销还取决于 rank 数和实现，不能把 1G 当作总显存占用。

## 包装脚本：目标与路径必须显式提供

在本技能目录生成计划，不会连接服务器或打流。`HCCL_HOST_SPECS` 是由本次参与节点清单生成的 Bash 数组，每个元素为 `hostname:slots`；元素数量来自实际测试组，下面不填写任何固定地址：

```bash
: "${HCCL_HOST_SPECS[0]:?请先按本次节点清单填写HCCL_HOST_SPECS数组}"
HCCL_HOST_ARGS=()
for HCCL_HOST_SPEC in "${HCCL_HOST_SPECS[@]}"; do
  HCCL_HOST_ARGS+=(--host "$HCCL_HOST_SPEC")
done
python scripts/hccl_bench.py "${HCCL_HOST_ARGS[@]}" \
  --source "${HCCL_ENV_SCRIPT:?请填写本次环境脚本路径}" \
  --directory "${HCCL_TEST_DIR:?请填写本次hccl_test目录}" \
  --mpi "${HCCL_MPI:?请填写已核实的mpich或openmpi}" --op all2all --profile smoke
```

包装器按全部 `--host` 参数生成 hostfile，自动计算 `-n`。当前要求至少两个不同节点且 slots 一致；不支持的异构映射会拒绝，不会偷偷截取前两个节点。`--host` 是 MPI 目标，不支持 `user@host` 或 IPv6；登录用户由现场 MPI/SSH 配置处理，不能把管理地址自动当作 HCCL 业务地址。

`--source` 与 `--inherit-env` 必须明确选一个：若当前环境已准备好，用 `--inherit-env` 替换 `--source 路径`。`--directory` 必填；不再预置某台机器上的脚本、CANN 版本或安装目录。缺参数直接报错，不尝试历史路径。

确认 MPI/测试程序帮助信息、版本、卡空闲后，才添加 `--execute`。默认 smoke 只到 1M；大流量用 `--profile large-1g`，旧名称 `historical-1g` 仅作兼容别名，不绑定机器。`--op` 还支持 allgather、allreduce、reduce-scatter。版本支持 `-c 1` 时可加 `--check`；现场确需相应设置时才加 `--aiv` 或 `--fullmesh`。

`--mpi mpich` 使用 `-f`；`--mpi openmpi` 使用 `--hostfile` 和对应 slots 的 ppr 映射。执行前读取 mpirun --version，不匹配就拒绝。MPI 未必自动传播 source 后的环境，必须逐节点核对库路径、HCCL_ALGO 和本节点 Host IP，不能把发起节点的 HCCL_IF_IP 复制给其他节点。

包装器使用唯一临时 hostfile，不覆盖用户已有文件。保存命令、MPI 版本、耗时、rc 与日志。不同 hccl_test 版本表格格式不同，rc=0 仅标记执行成功，数值结果仍须核对；MPI 远端 daemon 的清理行为因实现不同，超时后须确认各节点仅本次测试 rank 已退出。

官方工具提供多类 collective；AIV 是该版本测试中的执行扩展/计算单元相关选项，不是网络传输类型，也不等于 MC2 融合算子。[hccl_test 官方说明](https://gitcode.com/cann/oam-tools/blob/master/src/hccl_test/README_en.md)

## 真正的“每卡连通性”

一个多 rank AllToAll 测的是本次选定的通信域，算法可能分层转发；不能由此声称每个物理卡对都做了独立建链。

`preflight.py pairs` 每次从现场选定一对节点，对各自 devices 列表做笛卡尔积；若分别有 M、N 张选定卡，则测试 M×N 个两 rank 域。8×8=64 只是算术示例，不固定节点身份或卡数。每个卡对校验多个尺寸、重复次数的 AllReduce、AllGather、AllToAll 和 barrier，并记录失败阶段；两 rank collective 同时含两个方向的数据交换。

按故障证据选择最小参与节点/卡 → 相关卡对 → 必要的卡对矩阵 → 真实多机通信组 → 消息尺寸梯度 → 真实模型通信形状。工具 pairs 当前一次仅接收两个节点并直接跑其选定卡的笛卡尔积，这是隔离工具的范围，不是服务器清单的限制；需要先测少量时缩小 devices。多节点场景由实际通信关系选择待测边，不固定主机顺序或只测清单前两个。

设备 ping 可以把失败进一步分成物理/设备网络与 HCCL 资源两层，仍不能取代 collective。不要解析任意报错里的 IP 数字当作成功读到的设备地址；必须确认 hccn 返回格式、字段名和命令成功。

## MC2 不是 AllReduce 的另一个名字

MC2 需要在目标版本中测试真实融合算子，不同平台、形状、图模式和通信资源 API 约束不同。现在提供 `require_mc2` + `mc2_cases` 的逐算子执行和验收：内置 Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter 三类真实 API 的非量化 eager 小探针；AllToAll 融合、MoE Dispatch/Combine、Fused MoE、量化与图模式走版本化适配器。没有声称跨所有 CANN/A5 版本通用。

完整的算子分类、现场识别、配置模板、MoE 配对校验和故障阶段见 [MC2 算子级检测指南](mc2-testing.md)。不要在完成本页普通 hccl_test 后就停止；应根据部署实际使用的 MC2 路径继续验证。`primitives` 与 `mc2` 是不同放行范围，未测项保持未验证。

验收必须包含：实际融合算子已执行、跨节点而非仅本地卡、逐元素与参考结果比较、对应 rank/device/shape/dtype、同步完成、同版本 eager/图模式必要覆盖。普通 allreduce/alltoall/aiv 成功不能把 mc2 填 PASS。适配器失败要输出算子名、rank、资源初始化阶段和异常码，外层 watchdog 限时回收。

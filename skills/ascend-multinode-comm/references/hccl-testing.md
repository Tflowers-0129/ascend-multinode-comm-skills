# 官方打流、逐卡矩阵与 MC2

## 从本次节点清单生成打流计划

节点、地址、卡数、CANN/hccl_test 路径都来自本次用户提供的信息和现场核查，不沿用历史服务器。先确定参与测试的通信域与发起节点，在该节点的目标容器/用户环境中准备 MPI；总 rank 数为各参与节点 slots 之和，不固定节点数量或每机卡数。

A3/A5 共用参数化入口，但先按 [平台分支](platform-a3-a5.md) 核实型号、版本与设备网络。A3 的 HCCS/vNIC/spod-info 与 A5 的 UB/URMA/HiXLEP 不能互相替代；`-a aiv` 和 fullmesh 不因平台标签自动启用。包装器仅生成/执行明确配方，不自动认证任意 hccl_test 二进制或混合平台组的支持性。

| 参数 | 如何确定 |
|---|---|
| 参与节点列表 | 每个节点的 MPI 目标主机名/IP 和本次选定的 slots；MPI 能否解析 SSH 别名需现场确认 |
| 发起位置 | 本次选定的参与节点、容器、用户和工作目录，不默认清单中的某个历史主机 |
| 环境初始化 | 当前版本实际可用的环境脚本，或明确沿用已准备好的环境 |
| 测试目录 | 当前安装的 hccl_test 目录；先核对二进制及 MPI/CANN 版本，不固定安装前缀或版本号 |
| rank/卡映射 | 本次可见逻辑卡、物理卡对应和空闲状态；slots 并不能证明用了预期物理卡 |
| 算法与尺寸 | 从现场进程/配置取得；默认小流量，fullmesh/AIV 和 1G 各自按需启用 |

## 推荐：MPICH/Hydra + 官方 HCCL Test

本节固化可迁移的打流过程，不收录某次现场的 hostfile、IP、容器名、安装路径或临时 rank wrapper。实际执行时可根据现场生成短期 wrapper，但它只负责逐 rank 加载本机环境和启动官方 HCCL Test，不能把一个节点的地址硬编码给其他节点。

先用实际可达地址创建 MPICH hostfile，首行会先分配 rank 0：

~~~text
node-a.example:1 user=test-user
node-b.example:1 user=test-user
~~~

多节点 hostfile 不要写 localhost 或 127.0.0.1。Hydra 的远端 proxy 必须回连发起节点；若发起节点被发布为 localhost，远端会错误地连接自身并报 unable to connect ... localhost。先执行只打印 hostname/rank 的 mpirun launcher smoke，并用 -iface 指定 Hydra 发布的回连网卡。进入 HCCL 前，每个 rank 再从同名数据网卡读取自己的 IPv4，分别设置 HCCL_IF_IP 和 HCCL_SOCKET_IFNAME，不能把发起节点地址复制到所有节点。

先选择已知健康且空闲的同编号设备建立基线。下列变量只是过程骨架，值必须由当前现场核实：

~~~bash
export MPI_HOME=/shared/software/mpich
export HCCL_ENV_SCRIPT=/usr/local/Ascend/cann/set_env.sh
export HCCL_HOSTFILE=/shared/hccl-test/hostfile
export HCCL_MPI_IFACE=data0.3001
export HCCL_DATA_IFACE=data0.3001
export HCCL_TEST_USE_DEVS=0
export HCCL_WORLD_SIZE=2
export HCCL_NPUS_PER_NODE=1
export HCCL_TEST_DIR=/usr/local/Ascend/cann/tools/hccl_test
export HCCL_TEST_OP=broadcast
export HCCL_TEST_ROOT=0
export HCCL_TEST_MINBYTES=8K
export HCCL_TEST_MAXBYTES=1M
export HCCL_TEST_WARMUP=2
export HCCL_TEST_ITERS=5
export HCCL_TEST_CHECK=1
export HCCL_TEST_TIMEOUT_S=120
export HCCL_TEST_LOG=/shared/hccl-test/broadcast-device0.log
set -o pipefail

# 1. launcher smoke：确认两个 rank 落到预期节点，且远端 proxy 能回连发起节点。
timeout 30s "$MPI_HOME/bin/mpirun" -launcher ssh -iface "$HCCL_MPI_IFACE" \
  -f "$HCCL_HOSTFILE" -n "$HCCL_WORLD_SIZE" -prepend-rank hostname

# 2. HCCL Test：让各 rank 的现场临时 wrapper 先 source CANN、设置本机
#    HCCL_IF_IP/HCCL_SOCKET_IFNAME/HCCL_TEST_USE_DEVS，再 exec 同一份官方二进制。
timeout --signal=INT --kill-after=10s "$HCCL_TEST_TIMEOUT_S"s \
  "$MPI_HOME/bin/mpirun" -launcher ssh -iface "$HCCL_MPI_IFACE" \
  -f "$HCCL_HOSTFILE" -n "$HCCL_WORLD_SIZE" -prepend-rank \
  -genv MPI_HOME "$MPI_HOME" -genv HCCL_ENV_SCRIPT "$HCCL_ENV_SCRIPT" \
  -genv HCCL_DATA_IFACE "$HCCL_DATA_IFACE" \
  -genv HCCL_TEST_USE_DEVS "$HCCL_TEST_USE_DEVS" \
  /shared/hccl-test/rank-env-wrapper.sh /shared/hccl-test/broadcast_test \
  -b "$HCCL_TEST_MINBYTES" -e "$HCCL_TEST_MAXBYTES" -f 2 \
  -d fp32 -p "$HCCL_NPUS_PER_NODE" -n "$HCCL_TEST_ITERS" \
  -w "$HCCL_TEST_WARMUP" -c "$HCCL_TEST_CHECK" -r "$HCCL_TEST_ROOT" \
  2>&1 | tee "$HCCL_TEST_LOG"
test_rc=${PIPESTATUS[0]}
printf 'HCCL test rc=%s\n' "$test_rc"
~~~

broadcast 且 HCCL_TEST_ROOT=0 时，hostfile 首行的 rank 0 是发送根，适合验证“节点 A → 节点 B”；allreduce 则是双向 collective。mpirun 前面的 -n 表示 MPI 总 rank 数，测试二进制后面的 -n 表示测试迭代次数，两者不能混淆。设备列表个数必须等于每节点 slots，例如 host:2 对应 HCCL_TEST_USE_DEVS=0,1；启动前必须显式核对。

若各节点本地 broadcast_test 哈希不同，可将一份已核实的官方二进制放到共享只读路径。每个 rank 应重新 source CANN 环境、补 MPI 库、执行 ldd 缺库检查并打印实际二进制 SHA-256。MPI 版本、CANN/HCCL 库、测试二进制及卡映射仍要先核对；共享二进制不等于允许混用不兼容运行库。

只有生产配置确实使用该算法且版本适用时才设置 HCCL_ALGO=level0:fullmesh。健康基线通过后，再把 HCCL_TEST_USE_DEVS 改为待隔离卡，并把尺寸缩到单一小值、迭代降为 1。不要用 -i 0 表示单尺寸：该工具把它解释为持续重复起始尺寸；应把 -b/-e 设为相同值。

外层 timeout 返回 124、两端已打印 HCCL_RANK_READY、测试参数也已输出，但始终没有首条尺寸结果，说明 MPI 拉起和进程入口已经通过，阻塞发生在 HCCL collective 执行阶段。它需要与同一设备边的 hccn_tool -g -link/-port_info/-down_data 证据关联；仍不能单凭普通 HCCL 测试断言具体 MC2 kernel 支持或失败。

以下是参数化的 MPICH 手工命令形态，不会替用户选择目标或生成生产配置。环境变量须先从现场填好，hostfile 用唯一的新文件绝对路径，总 rank 数与其 slots 总和一致。手工模式需自行核对。

```bash
: "${HCCL_ENV_SCRIPT:?请填写本次环境脚本路径}"
: "${HCCL_TEST_DIR:?请填写本次hccl_test目录}"
: "${HCCL_HOSTFILE:?请填写本次已核对的hostfile绝对路径}"
: "${HCCL_WORLD_SIZE:?请填写hostfile中slots总和}"
: "${HCCL_NPUS_PER_NODE:?请填写每节点参与卡数}"
source "$HCCL_ENV_SCRIPT"
cd -- "$HCCL_TEST_DIR"
mpirun -f "$HCCL_HOSTFILE" -n "$HCCL_WORLD_SIZE" ./bin/alltoall_test -b 8 -e 1M -f 2 -p "$HCCL_NPUS_PER_NODE"
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

确认 MPI/测试程序帮助信息、版本、卡空闲后，才添加 `--execute`。默认 smoke 只到 1M；大流量用 `--profile large-1g`，旧名称 `historical-1g` 仅作兼容别名，不绑定机器。`--op` 还支持 allgather、allreduce、reduce-scatter 和 broadcast；broadcast 用 `--root` 指定根 rank。版本支持 `-c 1` 时可加 `--check`；现场确需相应设置时才加 `--aiv` 或 `--fullmesh`。

`--mpi mpich` 使用 `-f`；`--mpi openmpi` 使用 `--hostfile` 和对应 slots 的 ppr 映射。执行前读取 mpirun --version，不匹配就拒绝。MPI 未必自动传播 source 后的环境，必须逐节点核对库路径、HCCL_ALGO 和本节点 Host IP，不能把发起节点的 HCCL_IF_IP 复制给其他节点。

包装器使用唯一临时 hostfile，不覆盖用户已有文件。保存命令、MPI 版本、耗时、rc 与日志。不同 hccl_test 版本表格格式不同，rc=0 仅标记执行成功，数值结果仍须核对；MPI 远端 daemon 的清理行为因实现不同，超时后须确认各节点仅本次测试 rank 已退出。

官方工具提供多类 collective；AIV 是该版本测试中的执行扩展/计算单元相关选项，不是网络传输类型，也不等于 MC2 融合算子。[hccl_test 官方说明](https://gitcode.com/cann/oam-tools/blob/master/src/hccl_test/README_en.md)

## 真正的“每卡连通性”

先分离物理/设备路径问题时，可用 [宿主机 HCCS 小包检测](host-fabric-detection.md) 及 `fabric_probe.py`：自动发现映射，检查 vNIC/Pod/SDID 重叠，逐方向核对收发统计。`--same-index` 只测同编号对，不是全卡对；退出码 0 也可能伴随 100% 丢包，不能当 PASS。这一层通过不代替下述两 rank collective 或 MC2。

一个多 rank AllToAll 测的是本次选定的通信域，算法可能分层转发；不能由此声称每个物理卡对都做了独立建链。

`preflight.py pairs` 每次从现场选定一对节点，对各自 devices 列表做笛卡尔积；若分别有 M、N 张选定卡，则测试 M×N 个两 rank 域。8×8=64 只是算术示例，不固定节点身份或卡数。每个卡对校验多个尺寸、重复次数的 AllReduce、AllGather、AllToAll 和 barrier，并记录失败阶段；两 rank collective 同时含两个方向的数据交换。

按故障证据选择最小参与节点/卡 → 相关卡对 → 必要的卡对矩阵 → 真实多机通信组 → 消息尺寸梯度 → 真实模型通信形状。工具 pairs 当前一次仅接收两个节点并直接跑其选定卡的笛卡尔积，这是隔离工具的范围，不是服务器清单的限制；需要先测少量时缩小 devices。多节点场景由实际通信关系选择待测边，不固定主机顺序或只测清单前两个。

设备 ping 可以把失败进一步分成物理/设备网络与 HCCL 资源两层，仍不能取代 collective。不要解析任意报错里的 IP 数字当作成功读到的设备地址；必须确认 hccn 返回格式、字段名和命令成功。

## MC2 不是 AllReduce 的另一个名字

MC2 需要在目标版本中测试真实融合算子，不同平台、形状、图模式和通信资源 API 约束不同。现在提供 `require_mc2` + `mc2_cases` 的逐算子执行和验收：内置 Matmul-AllReduce、AllGather-Matmul、Matmul-ReduceScatter 三类真实 API 的非量化 eager 小探针；AllToAll 融合、MoE Dispatch/Combine、Fused MoE、量化与图模式走版本化适配器。没有声称跨所有 CANN/A5 版本通用。

完整的算子分类、现场识别、配置模板、MoE 配对校验和故障阶段见 [MC2 算子级检测指南](mc2-testing.md)。不要在完成本页普通 hccl_test 后就停止；应根据部署实际使用的 MC2 路径继续验证。`primitives` 与 `mc2` 是不同放行范围，未测项保持未验证。

验收必须包含：实际融合算子已执行、跨节点而非仅本地卡、逐元素与参考结果比较、对应 rank/device/shape/dtype、同步完成、同版本 eager/图模式必要覆盖。普通 allreduce/alltoall/aiv 成功不能把 mc2 填 PASS。适配器失败要输出算子名、rank、资源初始化阶段和异常码，外层 watchdog 限时回收。

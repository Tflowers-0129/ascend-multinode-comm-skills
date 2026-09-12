# 用户手动通信测试

这个目录只放可以由用户在 Ascend 服务器上直接执行的最小测试。先根据目标选择入口：

| 目标 | 文件 | 能证明什么 |
|---|---|---|
| 用官方 HCCL Test 做双机打流、健康卡/故障卡对照 | [`run_hccl_test.sh`](run_hccl_test.sh) | MPI 能拉起全部 rank，且所选普通 HCCL collective 能完成 |
| 从 Python/torch 直接调用 HCCL collective | [`torch_collectives.py`](torch_collectives.py) | `torch.distributed` 的 HCCL 初始化、通信和数值正确性 |
| 验证 MC2 融合算子或 MoE Dispatch/Combine | [MC2 算子级指南](../references/mc2-testing.md) | 只有实际调用对应融合 API 才能覆盖该 MC2 路径 |

这些脚本不安装依赖、不修改网卡/路由/拓扑，也不写入现场 IP、密码和日志。普通 HCCL 通过不等于 MC2 通过。

## 1. MPICH + 官方 HCCL Test 双机打流

两台机器应使用相同版本的 MPI、CANN 和 `hccl_test` 二进制。执行节点应能免密 SSH 到 hostfile 中的两个名字，并确认测试卡空闲。

如果尚未编译 `hccl_test`，按当前安装路径执行一次：

```bash
export MPI_HOME=/usr/local/mpich-3.2.1
export CANN_HOME=/usr/local/Ascend/ascend-toolkit/latest
source "$CANN_HOME/set_env.sh"
cd "$CANN_HOME/tools/hccl_test"
make MPI_HOME="$MPI_HOME" ASCEND_DIR="$CANN_HOME"
```

全组 8 卡 × 2 机的 hostfile 示例：

```text
node-a:8
node-b:8
```

在第一台机器上设置四个现场值，然后直接执行 AllToAll：

```bash
cd /path/to/ascend-multinode-comm/manual-tests
export MPI_HOME=/usr/local/mpich-3.2.1
export CANN_HOME=/usr/local/Ascend/ascend-toolkit/latest
export HCCL_TEST_DIR="$CANN_HOME/tools/hccl_test"
export HCCL_SOCKET_IFNAME=<两端实际通信网卡名>

bash run_hccl_test.sh alltoall ./hostfile_8x2
```

脚本默认执行与常见官方教程一致的 `alltoall_test -b 8 -e 1G -f 2`。要明确测试 AIV 路径时再加 `USE_AIV=1`；要复现服务的 FullMesh 配置时，在运行前设置 `HCCL_ALGO=level0:fullmesh`。不要为了让测试通过而擅自改变服务所用算法。

逐卡隔离时，另建两节点、每节点一个 rank 的 hostfile：

```text
node-a:1
node-b:1
```

如果发起节点不能 SSH 回自己，可以只把第一行改成 `localhost:1`。先运行一次 MPI 基础拉起：

```bash
"$MPI_HOME/bin/mpirun" -launcher ssh -iface "$HCCL_SOCKET_IFNAME" \
  -f ./hostfile_pair -n 2 -prepend-rank hostname
```

再用完全相同的节点、网卡、数据量和命令对比健康卡与疑似故障卡，只改最后的设备号：

```bash
bash run_hccl_test.sh broadcast ./hostfile_pair 0
bash run_hccl_test.sh broadcast ./hostfile_pair 7
```

Broadcast 默认发送 8 KiB、运行 1 次且不 warmup，外层超时 90 秒。`exit code 0` 且各 rank 输出结果表示本次 collective 完成；`exit code 124` 表示超时。如果健康卡完成，而另一张卡稳定停在参数输出之后并超时，故障随该卡的跨机路径移动；再结合 `hccn_tool` 链路状态、收发计数和 plog 判断是端口/链路还是软件阶段，不能只凭“卡住”断言硬件损坏。

同一命令在健康卡通过，说明 MPI、CANN、HCCL 的公共环境总体可用；它不能证明融合 MC2/FM16 路径一定受支持。单机 EP/MC2 成功也不排除某张卡的跨机出口异常：单机通信可能只走机内 HCCS/UB，只有跨机测试实际经过外部链路。应以“健康卡/疑似故障卡只改设备号”的对照结果缩小范围。

容器里没有 `ip` 命令不代表网卡不存在，可用 `ifconfig` 或 `/sys/class/net` 查看。全组 AllToAll 也不等于每个物理卡对都被独立覆盖。更多判读见 [HCCL 检测指南](../references/hccl-testing.md)。

## 2. Python/torch 直接调用 HCCL collective

[`torch_collectives.py`](torch_collectives.py) 直接使用 `torch.distributed` 的 HCCL backend，依次测试 AllReduce、Broadcast、AllGather 和 AllToAll，并校验每个结果。两端先加载相同 CANN/PyTorch 环境并设置实际 HCCL 数据网卡。

双机各用 NPU 7、每机一个进程时，在节点 0 执行：

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
export HCCL_SOCKET_IFNAME=<实际通信网卡名>
torchrun --nnodes=2 --nproc-per-node=1 --node-rank=0 \
  --master-addr=<节点0可达IP> --master-port=29500 \
  ./torch_collectives.py --device-ids 7 --op all
```

在节点 1 执行相同命令，只把 `--node-rank=0` 改为 `--node-rank=1`。每机八卡时，两端都改为：

```text
--nproc-per-node=8 --device-ids 0,1,2,3,4,5,6,7
```

`MASTER_ADDR` 是 `torchrun` rendezvous/TCPStore 地址，不是 HCCL 数据面的设备地址；HCCL 使用的网卡由现场配置决定。`--device-ids` 按每个节点的 `LOCAL_RANK` 顺序映射当前进程可见设备；设置过设备可见性变量时要按重编号后的逻辑设备填写。

这个脚本验证普通 collective，不会调用 `torch_npu.npu_moe_distribute_dispatch_v2`，也不能替代 `npu_mm_all_reduce_base` 等融合 MC2 测试。需要 MC2 时按 [MC2 指南](../references/mc2-testing.md) 先确认现场版本的真实 API、产品和 shape 约束，再运行仓库现有的版本化融合探针；不要用普通 AllToAll 冒充 MoE Dispatch/Combine。

# 官方打流、逐卡矩阵与 MC2

## 18 → 19 的历史配方

以下命令由用户提供，表示当时验证可用的环境，不代表本次已重新实测。原节点还包括 20、21、23；不要默认它们当前空闲、镜像一致或都属于同一个通信域。

在 18 机器、原来运行成功的 namespace 中：

```bash
source /home/tools/source_128p.sh
cd /usr/local/Ascend/cann-9.2.0/tools/hccl_test/
vim hostfile_8x2
```

hostfile_8x2 内容：

```text
141.61.33.18:8
141.61.33.19:8
```

```bash
mpirun -f hostfile_8x2 -n 16 ./bin/alltoall_test -b 8 -e 1G -f 2 -a aiv
```

fullmesh 配方中保留：

```bash
export HCCL_ALGO=level0:fullmesh
```

先检查 source 文件可用、两机二进制/库版本一致、MPI 互信、每卡空闲、设备映射与通信网卡。1G 是大流量，AllToAll 的实际内存/带宽开销与 rank 数和实现有关，不能把 1G 简单当作总显存占用。

## 包装脚本

在本技能目录运行，只生成计划、不打流：

```bash
python scripts/hccl_bench.py --host 141.61.33.18:8 --host 141.61.33.19:8 \
  --mpi mpich --op all2all --aiv --fullmesh --profile historical-1g
```

确认 MPI/测试程序帮助信息与版本、卡空闲后添加 `--execute`。默认 smoke 只到 1M，`--op` 还支持 allgather、allreduce、reduce-scatter。支持正确性校验的版本可加 `--check`（传 `-c 1`）；先核对本机 `bin/对应程序 -h`，不要自动安装或混用版本。

`--mpi mpich` 生成 `host:slots` 配合 `-f`；`--mpi openmpi` 生成 `host slots=N` 配合 `--hostfile` 与显式 ppr 映射。执行前读取 mpirun --version，不匹配就拒绝。source 脚本中的环境未必会被 MPI 自动传播到远端，必须核对远端 rank 的库路径、HCCL_ALGO 和 Host IP，不能把 18 的 HCCL_IF_IP 复制给 19。

包装器用唯一临时 hostfile，不覆盖用户的 hostfile_8x2。保存命令、MPI 版本、耗时、rc 与日志。不同 hccl_test 版本表格格式不同，所以 rc=0 仅标记执行成功，数值结果仍须核对；报告默认 UNVERIFIED，不伪造正确性。MPI 远端 daemon 的清理行为因实现不同，超时后须确认各机仅本次测试 rank 已退出。

官方工具提供多类 collective；AIV 是该版本测试中的执行扩展/计算单元相关选项，不是网络传输类型，也不等于 MC2 融合算子。[hccl_test 官方说明](https://gitcode.com/cann/oam-tools/blob/master/src/hccl_test/README_en.md)

## 真正的“每卡连通性”

16-rank AllToAll 测的是一个完整通信域，算法可能分层转发；不能由此声称每个物理卡对都做了独立建链。

`preflight.py pairs` 对节点 A 的每个 devices 元素与节点 B 的每个 devices 元素逐一创建两 rank 域；8×8=64 个。每个卡对校验多个尺寸、重复次数的 AllReduce、AllGather、AllToAll 和 barrier，并记录失败阶段。两 rank collective 同时含两个方向的数据交换。运行示例在仓库 README。

优先流程：单机指定两卡 → 两机各单卡 → 对应卡号 → 全笛卡尔积 → 16-rank → 多机实际组 → 消息尺寸梯度 → 真实模型通信形状。工具 pairs 当前直接全笛卡尔积；需要先测少量时缩小 devices。每个测试不会并发占用多个卡对，但全量可能耗时很长。

设备 ping 可以把失败进一步分成物理/设备网络与 HCCL 资源两层，仍不能取代 collective。不要解析任意报错里的 IP 数字当作成功读到的设备地址；必须确认 hccn 返回格式、字段名和命令成功。

## MC2 不是 AllReduce 的另一个名字

MC2 需要在目标版本中测试真实融合算子，例如矩阵乘与通信融合、专家分发/合并等路径；不同平台、形状、图模式和通信资源 API 约束不同。当前库提供 `require_mc2` 和适配器契约，没有声称内置一个跨所有 CANN 版本通用的 MC2 runner。

验收必须包含：实际融合算子已执行、跨节点而非仅本地卡、逐元素与参考结果比较、对应 rank/device/shape/dtype、同步完成、同版本 eager/图模式必要覆盖。普通 allreduce/alltoall/aiv 成功不能把 mc2 填 PASS。适配器失败要输出算子名、rank、资源初始化阶段和异常码，外层 watchdog 限时回收。

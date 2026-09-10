# 仿真、真实探针、服务验收的边界

## 四种证据不能混用

| 层级 | 在哪里运行 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| L0 单元/故障注入 | 无卡开发机 | 多网卡决策、端口冲突、超时、协议校验、报告不误放行 | 真实远端网络、Gloo/HCCL 或模型 |
| L1 无模型真实探针 | 目标主机/容器 | DNS、源绑定 TCP、真实 TCPStore/Gloo；带卡运行真实 HCCL 与显式 MC2 算子 case | 未覆盖端口、拓扑/算法/尺寸、MC2 分支/图模式或 KV 通路 |
| L2 官方 HCCL-VM | 匹配的 x86 Linux 仿真环境 | 虚拟拓扑和通信算法/数据一致性检查，按官方能力使用 | 真实线缆、光模块、交换机、拥塞、RoCE/UB 硬件性能 |
| L3 真实模型/connector | 正式镜像、实际角色与网络 | 本次版本/负载下 PD、池化、MC2 和请求路径可用 | 所有未来变更/故障、所有模型形状都安全 |

本库的 Python socket 测试是真实 TCP 小型应用层往返，不是 TCP 内核握手仿真器；也没有把 Python 求和脚本称为 HCCL 仿真。

## HCCL-VM 官方路径

官方 hcomm 提供 HCCL-VM，可在其支持的 x86 Linux 和 Ascend950 虚拟环境中启动拓扑并配合 hccl_test、检查组件。需要匹配 CANN/hcomm/hccl 源码与构建依赖；不是 pip 安装本库就自动具备。当前官方说明限制 PyTorch 接入能力，不把本库 torch_npu worker 自动改接到 VM。[HCCL-VM README](https://gitcode.com/cann/hcomm/blob/master/test/hccl_vm/README.md)

操作顺序：固定官方源码 revision → 按该 revision README 构建 → 选择官方样例 topology → 启动 VM → 运行该版本支持的 hccl_test → 保存 checker 结果 → 退出 VM 清理自有资源。命令、插件和 YAML 名称从已固定版本读取，不自动下载/编译 master 或假装已经完成仿真。

VM 使用的 rootinfo/topo 文件可能是虚拟拓扑入口；这与用户现场“真实容器不应多挂一个陈旧 rootinfo”不是矛盾，消费方和用途不同。VM 产物不能复制到真实服务器 /etc 覆盖拓扑。

## 可复现的故障用例

已内置本地测试：端口被占用时不发 ready；任一监听失败时控制器不发客户端；连接拒绝为 FAIL；多业务网卡歧义拒绝选取；缺失/陈旧证据拒绝 gate；命令卡死超时；探针从 stdin 在独立进程加载；真实多尺寸 TCP 摘要校验。

现场隔离环境还应补充以下演练（不能直接在生产机执行）：

- 使用专门测试 namespace，制造慢/错误反向解析，验证 store 失败能定位到 DNS。
- 一个 rank 缺席或 world_size 不一致，验证 watchdog 与所有节点超时记录。
- 指定不存在的 Gloo 网卡、不可见逻辑卡，验证分阶段失败而非泛化挂死。
- 在可销毁容器中准备过期/错误拓扑文件，观察真实 HCCL/HiXLEP 错误。不要改宿主 /etc。
- 使用测试存储 key/测试实例模拟 KV 读失败，验证无错误命中、超时回收与错误透传。

## 适配器契约

MC2 新任务优先使用 [逐算子 mc2_cases](mc2-testing.md)，可混合 builtin 与各版本适配器。下面的 `adapters[].stage=mc2` 是兼容旧配置的汇总契约，不提供逐 case 覆盖，也不能与 mc2_cases 混用。`gate --scope mc2` 仅对显式 case 清单生成放行要求；旧式汇总 PASS 不能获得该范围通过。

只有用户显式配置的 argv 才能执行；例子（路径必须已经存在于该容器）：

```json
{
  "require_mc2": true,
  "adapters": [
    {"stage":"mc2", "node":"node-a", "argv":["python3","/workspace/tests/actual_mc2.py"]},
    {"stage":"model_e2e", "node":"node-a", "argv":["python3","/workspace/tests/actual_request.py"]}
  ]
}
```

上例 node-a 只是通用节点标识，使用时必须替换为本次 nodes 中实际选定的名称，不指定固定执行主机。

适配器需以非零 rc 表示失败；成功必须输出一行 `A5_ADAPTER ` 加 JSON。下面只展示数据格式，不能复制固定 true 作为测试实现：

```json
{"status":"PASS","checks":{"fused_operator":true,"numerical_correctness":true,"cross_node":true},"evidence":{"operator":"实际融合算子名","ranks":[0,1],"shape":[128,128],"max_error":0}}
```

| stage | 所有必需 checks |
|---|---|
| mc2 | fused_operator、numerical_correctness、cross_node |
| kv_transfer | metadata、remote_kv、checksum、release |
| kv_pool | register、put、remote_get、checksum、cache_hit、cleanup |
| model_e2e | request、nonempty_output、correct_route |

这些布尔值必须由真实测试产生，并附版本、节点/卡/shape、结果摘要。框架只能校验契约，不替人验证任意适配器是否在撒谎；审查适配器代码、原始证据是技能流程的一部分。远端适配器不应自行 daemonize/setsid 逃离 watchdog，若调度 MPI/其他机器，必须自己实现相应回收。

### PD 验收适配器如何写

初始化与生产相同的 connector、内存注册和 KV layout；构造小块含 rank/偏移的非重复数据；由 P 产生并完成真实传输，D 从自己的接收缓存读回并逐元素校验；校验通知/超时与释放。元数据必须与真实 P/D DP、TP 一致。不要通过 Host TCP 复制一份数组冒充 NPU KV 通路。

### 池化验收适配器如何写

使用目标版本的 AscendStore/Mooncake/UCM 等真实后端：注册资源，A 写测试 key，B 远端读取并校验；再请求验证 hit，删除测试 key 验证 miss/清理。区分控制服务可达、资源注册成功、设备数据传输成功和缓存语义正确。协议选择和 PYTHONHASHSEED 等约束按实际部署核对。[官方 KV Pool 指南](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/kv_pool.html)

## 放行不是百分之百保证

primitives 只要求 inventory/runtime_alignment/dns/tcp 和配置中每个 group 的 TCPStore/Gloo/HCCL；mc2 还要求 platform 身份检查、所有显式 MC2 case 及其汇总通过；service 还要求 platform、真实请求及当前模式 KV/MC2。任何未提供证据均 UNVERIFIED。平台型号通过不是版本/API 兼容性证明；生产通信域是否与配置一致、版本是否兼容、模型显存是否足够，仍要另外核对。[A3/A5 平台检查边界](platform-a3-a5.md)

报告记录配置 SHA256；gate 默认一小时有效。不能手工删 required 项获得通过，不把修改后的报告当作原始证据；此版本不提供签名/防篡改服务。变更镜像、拓扑、网络、卡分配后重新执行。不要只把 `check && 启动生产服务` 当作所有模式的安全发布流程。

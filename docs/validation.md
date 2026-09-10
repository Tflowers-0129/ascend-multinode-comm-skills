# 验证记录与已知限制

日期：2026-09-10。开发环境：Windows，Python 标准库；无 torch/torch_npu/NPU，没有执行 18/19/20/21/23 的远端通信操作。以下不是现场通过报告。

最终本地 26 项 unittest 全部通过：自动地址选择及歧义、卡/端口/SSH 配置校验、CPU-only 存储节点分组、RoCE 能力证据、fullmesh 不误判为物理拓扑、未验证/过期证据拒绝放行、端口失败不发探针、真实回环 TCP 8/4096/65536 字节摘要、真实 DNS 调用、连接拒绝、命令与 worker 超时、缺失适配器证据拒绝、stdin 远端探针/子进程加载、两种 MPI hostfile、跨平台 Linux 命令生成。

skill-creator 的 quick_validate.py 校验通过，SKILL.md 及相对资源结构有效。

开发中修复了 Windows stdin 编码使中文探针无法加载的问题；DNS 用例保留系统真实结果，不假定任意机器的 localhost 正反解析一定对称。

尚未实测：Linux SSH/docker 全链路、CPU TCPStore/Gloo、A5 HCCL 及 64 卡对矩阵、hccl_test/CANN 9.2.0、HCCL-VM、MC2、真实 PD/KV 池化适配器、光模块/UB/RoCE 物理拓扑。源码和说明都必须保持这一边界。

已配置 GitHub Actions 的 Linux/Windows Python 3.10/3.12 标准库测试；本地通过不等于远端 CI 已通过。CI 不包含 NPU 测试。

## 初版覆盖缺口

- 仅 IPv4；物理/逻辑卡映射人工确认。没有自动从任意服务 launch shell 解析所有 TP/EP/PP/DP 域。
- 拓扑自动发现到设备/配置证据；物理 fullmesh 的还原需要平台邻接表与版本适配。
- 默认小 collective，未覆盖 PP P2P、全部 dtype/消息尺寸、图捕获/MC2。
- KV/MC2 提供验收接口与中文实施步骤，尚无跨现场版本的内置实现；缺失时严格未验证。
- hccl_test 退出零只表示程序完成，校验表需要匹配版本解释，包装器不会伪报正确性 PASS。
- 工具不自动消除用户服务占卡、版本冲突、代理和拓扑挂载警告；这些要在现场验收流程逐项确认。

## 上板验收建议

先提供一个两机各一张空闲卡的配置，在同一目标容器中运行 inspect/check；核对 IP/卡映射/版本与运行日志。然后扩为两机8卡→64卡对→真实模型域。最后分别接入实际 connector 和 MC2 测试。每一步保存报告，发现异常先缩小范围再加大负载。

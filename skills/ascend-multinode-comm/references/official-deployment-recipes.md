# vLLM-Ascend 官方部署配方索引

先使用官方文档及其配置脚本建立参考基线，不要求用户先提供大量成功脚本。用户已有远端部署时，实际脚本/运行版本仍是现场事实源；官方配方用于对照，不替代连接现场。

## 来源与证据级别

检索日期：2026-09-10。查询时最新正式版为 [v0.23.0](https://github.com/vllm-project/vllm-ascend/releases/tag/v0.23.0)，本页固定其官方源码提交 `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`，不是要求用户升级到此版本。后续应优先选择与现场安装版本一致的文档、源码和依赖。

本次完成官方来源及配置内容审阅，记为 `OFFICIAL_REFERENCE / SOURCE_REVIEWED`；没有运行这些部署，硬件/服务状态一律 `RUNTIME_UNVERIFIED`。即使官方给出性能结果，也只属于该示例声明的环境，不写成用户现场成功。

源码固定不等于依赖全部固定：部分页面正文仍要求 main 分支或链接其他 revision；必须分别记录“源码提交”“正文依赖”“实际镜像/安装版本”。`latest`/`main` 可用于发现更新，不能混进固定基线而不标注。原始 Markdown 中的 `|vllm_ascend_version|`、模板变量和说明文字也不是可直接执行的值。

## 首批主要配方

下列脚本多数嵌在官网文档代码块中，不是仓库里另有一个同名 `.sh` 文件。打开“固定源码”按章节/tab 读取整套脚本；不要凭文档中写了文件名就拼出不存在的下载地址。

| 编号 / 平台与场景 | 官方入口与固定源码定位 | 需要一起读取的配置 |
|---|---|---|
| R01：A3，多节点非 PD 分离部署 | [DeepSeek-V3.2 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V3.2.html)；[源码 §5.2 A3](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V3.2.md#L169) | Node0/Node1 两段、镜像与权重要求；DP 起始 rank、headless、业务 IP/网卡、TP/EP、图模式与 FlashComm |
| R02：A5 / 950DT，多节点混部 | [DeepSeek-V4-Pro 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Pro.html)；[源码 §5.1 950DT](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Pro.md#L464) | Node0/Node1、原始权重、DP/TP/EP、fullmesh、FlashComm/DSA-CP、图模式；不能只抄 TP 数值 |
| R03：A3，Mooncake PD 分离 | [DeepSeek 分离教程](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/features/pd_disaggregation_mooncake_multi_node.html)；[源码部署入口](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/features/pd_disaggregation_mooncake_multi_node.md#L227) | `launch_online_dp.py`、各 P/D 的 `run_dp_template.sh`；Layerwise/Non-layerwise 二选一匹配对应 proxy，连同 connector/engine_id/kv_port 和 P/D 并行描述 |
| R04：A3 或 A5 / 950DT，GLM-5 系列 PD 分离 | [GLM-5 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/GLM5.html)；[源码 §5.3 A3](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/GLM5.md#L808) / [§5.4 950DT](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/GLM5.md#L1387) | 每组 launcher、各 P/D 模板、§5.5 转发脚本和 §4 容器；950DT 有 LocalCommRes/HiXLEP 路径，不与 A3 模板混搭 |
| R05：A5 / 950DT，DeepSeek-V4-Pro PD 分离 | [官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/DeepSeek-V4-Pro.html)；[源码 §5.2.3](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/models/DeepSeek-V4-Pro.md#L1333) | launcher、P/D 模板、MooncakeHybridConnector、HiXLEP 与转发配置；逻辑 P/D 实例数不等于物理服务器数 |
| R06：A3 / Ascend 950，Mooncake 池化，含混部和 PD 分离 | [KV Pool 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/kv_pool.html)；[固定源码 Mooncake 部分](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/user_guide/feature_guide/kv_pool.md#L53) | `mooncake.json`、master、`multi_producer.sh` / `multi_consumer.sh` 或 `pd_mix.sh`；平台/传输分支分别选，PD+池化使用 MultiConnector |
| R07：A3，Memcache 池化，含混部和 PD 分离 | [KV Pool 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/kv_pool.html)；[固定源码 Memcache 部分](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/user_guide/feature_guide/kv_pool.md#L479) | `mmc-meta.conf`、`mmc-local.conf`、MetaService、`run_prefill.sh/run_decode.sh` 或 `Run_pd_mix.sh`；独立 MemCache 进程部署另读对应小节 |

本表的 A5 模型配方明确是 950DT，不外推为 950PR；R06 的具体 950 型号/依赖仍需单独核对。R07 固定源码的启动分支只列 A2/A3，不能套用后来 main 页的 A5 分支后仍称“v0.23.0 原配方”。没有匹配的组合时记录缺口，继续查对应版本官方资料，再按需补充现场样本。

## 可直接定位的独立脚本

以下均在同一固定提交中实际存在。它们是部署组件，不是本库可自动运行的预检探针。

- [launch_online_dp.py](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/examples/external_online_dp/launch_online_dp.py)：创建各本地 DP 实例。
- [run_dp_template.sh](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/examples/external_online_dp/run_dp_template.sh)：通用实例模板；本身没有 PD KV connector，必须按 R03/R04 等对应完整配方核对，不能单独称为 PD 部署脚本。
- [非 Layerwise PD proxy](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py) 与 [Layerwise PD proxy](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/examples/disaggregated_prefill_v1/load_balance_proxy_layerwise_server_example.py)：按所用 connector/模式选择，不互相替换。

外部 DP launcher 与同目录模板的参数对应如下；同名的其他历史模板未必遵循此契约：

| 模板位置 | 含义 |
|---|---|
| `$1` | 本次实例可见设备列表 |
| `$2` | 本次实例 API 端口 |
| `$3` / `$4` | 组内 DP 总数 / 当前 DP rank |
| `$5` / `$6` | DP master 地址 / RPC 端口 |
| `$7` | TP 大小 |

该 launcher 默认按连续设备编号切分；非连续空闲卡或不同可见掩码需另核实映射。参数类型检查不等于 rank/卡数/端口正确；源码只 join 子进程，不能把父 launcher 退出 0 当成全部 worker 成功。以上是源码审阅结论，需按 [脚本分析规则](deployment-script-audit.md) 对照目标版本。

## 补充与历史参考，不混入主基线

- [Mooncake 多实例混部官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/features/pd_colocated_mooncake_multi_instance.html) / [固定源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/tutorials/features/pd_colocated_mooncake_multi_instance.md)：明确以 A2 为例，提供 `mooncake.json`、master、实例启动和跨节点缓存命中测试。可参考其验证方法，不标成 A3/A5 成功案例。
- [大规模 EP 官网](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/large_scale_ep.html) / [固定源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/user_guide/feature_guide/large_scale_ep.md)：A3 HCCS/组划分参考，但正文仍推荐 v0.9.1，proxy 也链接旧分支。其模板位置参数与上表不同，必须保留同一教程的 launcher/template 配对，再对照现场版本迁移，不能直接拼接。

## 从这些配方提炼的预检重点

1. **版本与容器先于网络归因（S0）。** 镜像后缀、平台、驱动/CANN/connector 及源码必须匹配；官方页面是多 tab、多版本材料，不是把所有代码块串起来执行。安装依赖按 [官方安装指南](https://docs.vllm.ai/projects/ascend/en/v0.23.0/installation.html) 与 [固定安装源码](https://github.com/vllm-project/vllm-ascend/blob/5cb98caaadeff42b5b62b996e34bb2aaa29d20fd/docs/source/installation.md) 核实。
2. **控制面与数据面分开（S1–S3）。** R03 提醒 kv_port 可能与特定 AscendDirectTransport 的动态端口范围冲突；按当前版本与设备数核对，不做全平台固定禁用区间。Layerwise proxy 还需 D→proxy 的元数据回连，源码拒绝以通配地址充当可回连地址；不能只测 proxy→P/D 的 HTTP。
3. **MC2 不由开关放行（S3–S4）。** R01/R02 的 AIV、FlashComm、图模式是查找真实执行分支的线索，不是融合算子成功证据。继续按 [MC2 指南](mc2-testing.md) 查实际 API/算子与组、资源、数值和同步，不用普通 AllReduce 代替。
4. **池化不是一个 TCP 端口（S5–S6）。** R06 的 master/数据通路与 R07 的 MetaService/ConfigStore/LocalService 分层检查；哈希一致性、协议、内存注册及真实远端命中分别验收。MemCache 与 vLLM 进程分离不等于 P/D 分离，配置中的 LocalService world_size 也不是 HCCL rank 数。
5. **不盲删资源，也不照搬挂载。** R04 的 950DT 容器和 connector 明确有 rootinfo/HiXLEP 资源关联；这是需要审计消费者的反例，不是所有容器都应挂载它们。沿用 [拓扑文件规则](topology-and-files.md)，没有依赖才建议备份后移除。
6. **官方脚本也要审阅副作用。** R07 含日志删除和 hugepages 修改；部分模型模板 source 用户环境、改通信检查开关。记录并分离这些操作，不把它们加入只读预检，不自动放宽检查或覆盖故障现场。

## agent 使用方法

用户未提供成功脚本时，从本表选择与其平台、模型、模式和版本最接近的官方配方，不把提供现场成功样本设成前置条件。只为解决当前差异读取必要代码和依赖，不要求下载整个官方仓库。

为本次对照记录：配方编号、源码提交/路径/章节、依赖版本、计划通信组、需要本次替换的节点/IP/卡/端口/路径、与现场的差异、尚未覆盖的验收。官方示例的节点身份和资源规模不写入本库执行默认值；预检配置仍按现场生成。

本次索引没有复制整套上游部署脚本，也没有执行它们。需要生成实际部署副本时另按用户的部署/修改授权处理，保留来源与必要许可证；不要在用户仅请求检索、预检或排障时重启/部署服务。之后可按 [成功样本指引](known-good-deployments.md) 补充官方基线无法覆盖的现场变体。

#!/usr/bin/env python3
"""离线枚举并排序 P/D 的 DP、TP、PP 理论候选；不连接服务器或执行服务。"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile


OBJECTIVES = {"output_throughput", "request_throughput", "ttft", "tpot", "goodput", "balanced"}
MODES = {"quick", "exhaustive"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where} 必须是对象")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"{where} 包含未知字段：{sorted(unknown)}")


def _positive_int(value, where):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{where} 必须是正整数")
    return value


def _finite_number(value, where, low=None, high=None):
    if type(value) not in (int, float):
        raise ValueError(f"{where} 必须是有限数值")
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{where} 必须是有限数值") from exc
    if not math.isfinite(value):
        raise ValueError(f"{where} 必须是有限数值")
    if low is not None and value < low:
        raise ValueError(f"{where} 不能小于 {low}")
    if high is not None and value > high:
        raise ValueError(f"{where} 不能大于 {high}")
    return value


def _finite_product(left, right, where):
    try:
        result = float(left) * float(right)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{where} 超出有限数值范围") from exc
    if not math.isfinite(result):
        raise ValueError(f"{where} 超出有限数值范围")
    return result


def _bool(value, where):
    if type(value) is not bool:
        raise ValueError(f"{where} 必须是布尔值")
    return value


def _degree_list(value, where):
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError(f"{where} 必须是 1~16 项的正整数列表")
    values = [_positive_int(item, where) for item in value]
    if len(values) != len(set(values)):
        raise ValueError(f"{where} 不能包含重复值")
    return sorted(values)


def _text(value, where):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{where} 必须是 1~256 字符的普通字符串")
    return value


def validate_config(cfg):
    _keys(cfg, {"schema_version", "name", "mode", "objective", "environment", "hardware",
                "workload", "roles", "constraints", "max_recommendations"}, "配置")
    if type(cfg.get("schema_version")) is not int or cfg["schema_version"] != 1:
        raise ValueError("schema_version 仅支持 1")
    if not isinstance(cfg.get("name"), str) or not NAME_RE.fullmatch(cfg["name"]):
        raise ValueError("name 必须是普通标识符")
    if cfg.get("mode") not in MODES:
        raise ValueError("mode 必须为 quick 或 exhaustive")
    if cfg.get("objective") not in OBJECTIVES:
        raise ValueError(f"objective 必须是 {sorted(OBJECTIVES)} 之一")

    environment = cfg.get("environment")
    _keys(environment, {"platform", "model", "vllm", "vllm_ascend", "cann", "connector",
                        "transport"}, "environment")
    for key in ("platform", "model", "vllm", "vllm_ascend", "cann", "connector", "transport"):
        _text(environment.get(key), f"environment.{key}")

    hardware = cfg.get("hardware")
    _keys(hardware, {"cluster_devices", "devices_per_host"}, "hardware")
    cluster_devices = _positive_int(hardware.get("cluster_devices"), "hardware.cluster_devices")
    devices_per_host = _positive_int(hardware.get("devices_per_host"), "hardware.devices_per_host")
    if devices_per_host > cluster_devices:
        raise ValueError("devices_per_host 不能超过 cluster_devices")

    workload = cfg.get("workload")
    _keys(workload, {"concurrency", "mean_uncached_input_tokens", "mean_output_tokens",
                     "prefix_hit_rate", "qps"}, "workload")
    _positive_int(workload.get("concurrency"), "workload.concurrency")
    _positive_int(workload.get("mean_uncached_input_tokens"), "workload.mean_uncached_input_tokens")
    _positive_int(workload.get("mean_output_tokens"), "workload.mean_output_tokens")
    _finite_number(workload.get("prefix_hit_rate"), "workload.prefix_hit_rate", 0, 1)
    _finite_number(workload.get("qps"), "workload.qps", 0)
    if workload["qps"] == 0:
        raise ValueError("workload.qps 必须大于 0")

    roles = cfg.get("roles")
    _keys(roles, {"prefill", "decode"}, "roles")
    for role in ("prefill", "decode"):
        item = roles.get(role)
        _keys(item, {"devices", "tp_sizes", "pp_sizes", "minimum_replica_devices",
                     "maximum_dp", "require_node_local_tp"}, f"roles.{role}")
        devices = _positive_int(item.get("devices"), f"roles.{role}.devices")
        if devices > cluster_devices:
            raise ValueError(f"roles.{role}.devices 不能超过集群设备数")
        _degree_list(item.get("tp_sizes"), f"roles.{role}.tp_sizes")
        _degree_list(item.get("pp_sizes"), f"roles.{role}.pp_sizes")
        minimum = _positive_int(item.get("minimum_replica_devices"),
                                f"roles.{role}.minimum_replica_devices")
        if minimum > devices:
            raise ValueError(f"roles.{role}.minimum_replica_devices 不能超过角色设备预算")
        maximum_dp = item.get("maximum_dp")
        if maximum_dp is not None:
            _positive_int(maximum_dp, f"roles.{role}.maximum_dp")
        _bool(item.get("require_node_local_tp"), f"roles.{role}.require_node_local_tp")
    if roles["prefill"]["devices"] + roles["decode"]["devices"] > cluster_devices:
        raise ValueError("P/D 角色设备预算之和超过 cluster_devices")

    constraints = cfg.get("constraints")
    _keys(constraints, {"prefill_tp_multiple_of_decode_tp", "decode_pp_must_be_one",
                        "forbid_cross_host_tp"}, "constraints")
    for key in ("prefill_tp_multiple_of_decode_tp", "decode_pp_must_be_one",
                "forbid_cross_host_tp"):
        _bool(constraints.get(key), f"constraints.{key}")

    maximum = cfg.get("max_recommendations")
    _positive_int(maximum, "max_recommendations")
    limit = 4 if cfg["mode"] == "quick" else 16
    if maximum > limit:
        raise ValueError(f"{cfg['mode']} 模式 max_recommendations 不能超过 {limit}")
    return cfg


def canonical_sha256(cfg):
    payload = json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"JSON 不允许非有限常量：{value}")


def _load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=_strict_object, parse_constant=_reject_constant)


def _placeholder_fields(cfg):
    markers = ("fill-", "replace-", "placeholder", "todo", "tbd", "unknown")
    fields = []
    for key, value in cfg["environment"].items():
        normalized = value.strip().lower()
        if normalized.startswith(markers) or normalized in markers:
            fields.append(f"environment.{key}")
    return fields


def _role_candidates(cfg, role):
    item = cfg["roles"][role]
    devices_per_host = cfg["hardware"]["devices_per_host"]
    forbid_cross = cfg["constraints"]["forbid_cross_host_tp"]
    accepted = []
    rejected = []
    for tp in sorted(item["tp_sizes"]):
        for pp in sorted(item["pp_sizes"]):
            replica_devices = tp * pp
            descriptor = {"role": role, "tp": tp, "pp": pp, "replica_devices": replica_devices}
            if item["devices"] % replica_devices:
                rejected.append(dict(descriptor, reason_code="ROLE_DEVICE_BUDGET_MISMATCH"))
                continue
            dp = item["devices"] // replica_devices
            descriptor["dp"] = dp
            if replica_devices < item["minimum_replica_devices"]:
                rejected.append(dict(descriptor, reason_code="MINIMUM_REPLICA_DEVICES"))
                continue
            if item.get("maximum_dp") is not None and dp > item["maximum_dp"]:
                rejected.append(dict(descriptor, reason_code="MAXIMUM_DP"))
                continue
            tp_exceeds_host_capacity = tp > devices_per_host
            if tp_exceeds_host_capacity and (forbid_cross or item["require_node_local_tp"]):
                rejected.append(dict(descriptor, reason_code="CROSS_HOST_TP_FORBIDDEN"))
                continue
            if role == "decode" and cfg["constraints"]["decode_pp_must_be_one"] and pp != 1:
                rejected.append(dict(descriptor, reason_code="DECODE_PP_FORBIDDEN"))
                continue
            microbatches = max(1, (cfg["workload"]["concurrency"] + dp - 1) // dp)
            bubble = (pp - 1) / (microbatches + pp - 1)
            accepted.append({"dp": dp, "tp": tp, "pp": pp,
                             "replica_devices": replica_devices,
                             "tp_fits_single_host_capacity": not tp_exceeds_host_capacity,
                             "estimated_pipeline_bubble": round(bubble, 6)})
    if not accepted:
        raise ValueError(f"roles.{role} 没有满足显式约束的 DP×TP×PP 候选")
    return accepted, rejected


def _rank_vector(cfg, prefill, decode):
    objective = cfg["objective"]
    p_over = prefill["replica_devices"] - cfg["roles"]["prefill"]["minimum_replica_devices"]
    d_over = decode["replica_devices"] - cfg["roles"]["decode"]["minimum_replica_devices"]
    p_cross = int(not prefill["tp_fits_single_host_capacity"])
    d_cross = int(not decode["tp_fits_single_host_capacity"])
    if objective == "output_throughput":
        return [d_cross, -decode["dp"], d_over, decode["pp"], p_cross, p_over,
                prefill["estimated_pipeline_bubble"], -prefill["tp"], -prefill["dp"]]
    if objective == "request_throughput":
        return [p_cross + d_cross, -min(prefill["dp"], decode["dp"]),
                -(prefill["dp"] + decode["dp"]),
                prefill["estimated_pipeline_bubble"] + decode["estimated_pipeline_bubble"],
                p_over + d_over, -prefill["tp"], -decode["tp"]]
    if objective == "goodput":
        return [p_cross + d_cross,
                prefill["estimated_pipeline_bubble"] + decode["estimated_pipeline_bubble"],
                -min(prefill["dp"], decode["dp"]), p_over + d_over,
                -prefill["tp"], -decode["tp"]]
    if objective == "ttft":
        return [p_cross, -prefill["tp"], prefill["estimated_pipeline_bubble"], p_over,
                -prefill["dp"], d_cross, d_over, -decode["dp"]]
    if objective == "tpot":
        return [d_cross, -decode["tp"], decode["pp"], d_over, -decode["dp"],
                p_cross, p_over, prefill["estimated_pipeline_bubble"]]
    return [p_cross + d_cross, p_over + d_over,
            prefill["estimated_pipeline_bubble"] + decode["estimated_pipeline_bubble"],
            -min(prefill["dp"], decode["dp"]), -prefill["tp"], -decode["tp"]]


def _ranking_policy(objective):
    if objective == "output_throughput":
        return [
            "避免 D TP size 超过单机容量",
            "增加 D DP",
            "减少 D 单副本超过已核实下限的额外设备",
            "避免 D PP",
            "避免 P TP size 超过单机容量",
            "减少 P 单副本超过已核实下限的额外设备",
            "降低 P pipeline bubble",
            "在前述条件相同时增加 P TP",
        ]
    if objective == "request_throughput":
        return [
            "减少 P/D 跨机 TP 容量风险",
            "增加 P/D 中副本数较少一侧的 DP",
            "在前述条件相同时增加总副本数",
            "降低 P/D pipeline bubble",
            "减少单副本超过已核实下限的额外设备",
        ]
    if objective == "goodput":
        return [
            "减少 P/D 跨机 TP 容量风险",
            "降低 P/D pipeline bubble",
            "增加 P/D 中副本数较少一侧的 DP",
            "减少单副本超过已核实下限的额外设备",
            "最终必须在 verify 中执行 TTFT/TPOT/错误率 SLO 门禁",
        ]
    if objective == "ttft":
        return [
            "避免 P TP size 超过单机容量",
            "增加 P TP",
            "降低 P pipeline bubble",
            "减少 P 单副本超过已核实下限的额外设备",
            "在前述条件相同时增加 P DP",
            "保持 D 的本地性与副本容量",
        ]
    if objective == "tpot":
        return [
            "避免 D TP size 超过单机容量",
            "增加 D TP",
            "避免 D PP",
            "减少 D 单副本超过已核实下限的额外设备",
            "在前述条件相同时增加 D DP",
            "保持 P 的本地性并降低 pipeline bubble",
        ]
    return [
        "避免 P/D 的 TP size 超过单机容量",
        "减少单副本超过已核实下限的额外设备",
        "降低 P/D pipeline bubble",
        "平衡 P/D 可用副本数",
    ]


def _explain(cfg, prefill, decode):
    reasons = []
    risks = []
    objective = cfg["objective"]
    if prefill["pp"] > 1:
        reasons.append("P 使用 PP，在 TP 保持于较小通信域时可避免扩大逐层 TP collective；收益依赖并发和 stage 平衡")
    if prefill["tp"] <= cfg["hardware"]["devices_per_host"]:
        reasons.append("P 的 TP size 不超过单机卡数，具备保持 TP 通信在单机域内的必要条件")
    if objective == "output_throughput":
        reasons.append("吞吐/并发目标优先更多独立 D 副本，并采用显式允许范围内较小的 D TP")
    if objective == "request_throughput":
        reasons.append("请求吞吐目标优先提升 P/D 中副本数较少的一侧，降低串行链路的供给失衡风险")
    if objective == "goodput":
        reasons.append("goodput 的理论排序优先本地容量条件和较低 pipeline bubble；是否满足 SLO 只能由实测门禁确认")
    if objective == "ttft":
        reasons.append("TTFT 目标优先增强 P 单副本并行能力，同时避免跨机 TP")
    if objective == "tpot":
        reasons.append("TPOT 目标优先增强 D 单副本并行能力，而不是无条件最大化 D DP")
    if decode["dp"] > 1:
        risks.append("增加 D DP 会复制模型并缩小单副本卡数；必须验证权重/KV 显存、TPOT、batch 效率和路由均衡")
    if prefill["pp"] > 1:
        risks.append("PP 存在 pipeline bubble 和 stage/activation 传输开销；低并发或短输入可能不如 TP-only")
    if (not prefill["tp_fits_single_host_capacity"] or
            not decode["tp_fits_single_host_capacity"]):
        risks.append("TP size 超过单机容量，实际跨机 collective 代价依赖 placement、互联和算子实现")
    risks.append("这里只校验 TP size 是否装得下一台主机；P/D 联合 placement、设备连续性和通信域仍需显式验证")
    return reasons, risks


def _verification_plan(mode):
    if mode == "quick":
        return {
            "stages": ["baseline", "smoke", "representative", "final_full_e2e"],
            "guidance": "验证排序靠前候选，并人工加入一个已有 baseline 或资源余量型备选；探索阶段固定负载，前两名再做完整 E2E 与重复测试",
            "service_workflow_limit": "每个已批准 plan 最多 32 个显式 trial",
        }
    return {
        "stages": ["baseline", "topology_coarse", "feature_combinations", "numeric_refinement",
                   "full_e2e", "repeat_and_stability"],
        "guidance": "每阶段独立生成并审批 plan；普通候选逐级淘汰，最终候选重复并运行留出负载",
        "service_workflow_limit": "单阶段最多 32 个显式 trial；阶段间重新 plan 和授权",
    }


def advise(cfg):
    validate_config(cfg)
    placeholder_fields = _placeholder_fields(cfg)
    prefill_candidates, prefill_rejected = _role_candidates(cfg, "prefill")
    decode_candidates, decode_rejected = _role_candidates(cfg, "decode")
    pairs = []
    rejected_pairs = []
    for prefill in prefill_candidates:
        for decode in decode_candidates:
            if (cfg["constraints"]["prefill_tp_multiple_of_decode_tp"] and
                    (prefill["tp"] < decode["tp"] or prefill["tp"] % decode["tp"] != 0)):
                rejected_pairs.append({"prefill": prefill, "decode": decode,
                                       "reason_code": "PREFILL_TP_RATIO"})
                continue
            vector = _rank_vector(cfg, prefill, decode)
            reasons, risks = _explain(cfg, prefill, decode)
            pairs.append({"prefill": prefill, "decode": decode, "_rank_key": vector,
                          "reasons": reasons, "risks": risks})
    if not pairs:
        raise ValueError("P/D 联合候选均被显式兼容约束过滤")
    pairs.sort(key=lambda item: (item["_rank_key"], item["prefill"]["dp"],
                                 item["prefill"]["tp"], item["prefill"]["pp"],
                                 item["decode"]["dp"], item["decode"]["tp"],
                                 item["decode"]["pp"]))
    selected = pairs[:cfg["max_recommendations"]]
    for index, item in enumerate(selected, 1):
        item.pop("_rank_key")
        item["rank"] = index
    p_work = _finite_product(cfg["workload"]["qps"],
                             cfg["workload"]["mean_uncached_input_tokens"],
                             "workload 的 P 侧 QPS×token 负载代理")
    d_work = _finite_product(cfg["workload"]["qps"],
                             cfg["workload"]["mean_output_tokens"],
                             "workload 的 D 侧 QPS×token 负载代理")
    return {
        "schema_version": 1,
        "status": "DRAFT_RECOMMENDATION" if placeholder_fields else "RECOMMENDED_FOR_VALIDATION",
        "confidence": "THEORY_ONLY",
        "input_sha256": canonical_sha256(cfg),
        "name": cfg["name"],
        "mode": cfg["mode"],
        "objective": cfg["objective"],
        "environment": cfg["environment"],
        "input_warnings": ([
            "环境指纹仍含待填写占位符；候选仅用于演示，填写并重新运行后才可进入 verify："
            + ", ".join(placeholder_fields)
        ] if placeholder_fields else []),
        "budget_summary": {
            "cluster_devices": cfg["hardware"]["cluster_devices"],
            "prefill_devices": cfg["roles"]["prefill"]["devices"],
            "decode_devices": cfg["roles"]["decode"]["devices"],
            "allocated_devices": (cfg["roles"]["prefill"]["devices"]
                                  + cfg["roles"]["decode"]["devices"]),
            "unallocated_devices": (cfg["hardware"]["cluster_devices"]
                                    - cfg["roles"]["prefill"]["devices"]
                                    - cfg["roles"]["decode"]["devices"]),
            "devices_per_host": cfg["hardware"]["devices_per_host"],
            "minimum_hosts_by_device_count": (
                cfg["hardware"]["cluster_devices"] + cfg["hardware"]["devices_per_host"] - 1
            ) // cfg["hardware"]["devices_per_host"],
            "placement_status": "NOT_VALIDATED",
            "notice": "主机数只按设备容量下界计算；未验证 P/D 联合装箱、设备映射、互联或可用性",
        },
        "workload_proxies": {
            "prefill_uncached_tokens_per_second": p_work,
            "decode_output_tokens_per_second": d_work,
            "prefix_hit_rate": cfg["workload"]["prefix_hit_rate"],
            "notice": "mean_uncached_input_tokens 已是缓存复用后的输入口径，不再乘命中率；两项只是负载量代理，不能当作相同成本的容量或真实性能预测",
        },
        "ranking_policy": {
            "method": "按下列优先级做确定性字典序排序；不是加权分数或真实性能预测",
            "priority": _ranking_policy(cfg["objective"]),
        },
        "recommended": selected[0],
        "ranked_candidates": selected,
        "rejected_summary": {
            "prefill": prefill_rejected[:64],
            "prefill_total": len(prefill_rejected),
            "prefill_truncated": len(prefill_rejected) > 64,
            "decode": decode_rejected[:64],
            "decode_total": len(decode_rejected),
            "decode_truncated": len(decode_rejected) > 64,
            "pair_count": len(rejected_pairs),
            "pair_reason_codes": sorted({item["reason_code"] for item in rejected_pairs}),
        },
        "verification_plan": _verification_plan(cfg["mode"]),
        "assumptions": [
            "tp_sizes/pp_sizes、最小单副本卡数和兼容约束已经按目标版本核实",
            "角色设备预算、负载口径和目标指标在候选之间保持不变",
            "排序是可解释的理论先验，不预测真实毫秒、吞吐或精度",
        ],
        "limitations": [
            "当前工具固定用户给定的 P/D 设备预算，不自动重分配角色卡数",
            "token/QPS 仅形成负载代理；缺少当前模型实测服务时间时，不把 P/D token 成本硬合成为虚假容量分数",
            "排序主要由目标指标、合法拓扑、本地性和并发下的 PP bubble 驱动，需用 verify/quick 校准",
            "TP size 不超过单机容量只是可放置的必要条件；工具没有主机/设备 placement，不能证明 TP 实际位于单机域",
        ],
        "must_verify": [
            "服务能够启动且运行参数实际生效",
            "精度、失败请求、致命日志和测试后健康通过",
            "TTFT、TPOT、吞吐、峰值显存、KV 容量和 P/D 排队满足用户门槛",
            "P/D 每个实例的主机、设备和 rank placement 能同时装箱且与通信域一致",
        ],
    }


def _write_report(path, payload, force, protected):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_dir():
        raise ValueError("输出路径不能是符号链接或目录")
    try:
        if target.resolve() == Path(protected).resolve():
            raise ValueError("输出路径不能覆盖输入配置")
    except OSError as exc:
        raise ValueError(f"无法解析输出路径：{exc}") from exc
    data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temp_name = None
    reserved_identity = None
    if not force:
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ValueError("输出已存在；需要替换时显式使用 --force") from exc
        else:
            os.close(descriptor)
            stat_result = target.stat()
            reserved_identity = (stat_result.st_dev, stat_result.st_ino)
    published = False
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                         prefix=target.name + ".tmp.", delete=False) as handle:
            temp_name = handle.name
            handle.write(data)
        os.replace(temp_name, target)
        published = True
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
        if reserved_identity and not published:
            try:
                stat_result = target.stat()
                if (stat_result.st_dev, stat_result.st_ino) == reserved_identity:
                    target.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.force and not args.out:
            raise ValueError("--force 仅能与 --out 一起使用")
        cfg = _load_config(args.config)
        report = advise(cfg)
        if args.out:
            _write_report(args.out, report, args.force, args.config)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

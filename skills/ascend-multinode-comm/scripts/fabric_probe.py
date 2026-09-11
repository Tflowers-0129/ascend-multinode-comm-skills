#!/usr/bin/env python3
"""宿主机设备网络取证与有界 HCCS ping；不 source、不进入容器、不创建计算 rank。"""
import argparse
import base64
import concurrent.futures
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

PREFIX = "ASCEND_FABRIC "
SOURCE = globals().get("SOURCE") or Path(__file__).read_text(encoding="utf-8")
BOOT = "import sys;SOURCE=sys.stdin.buffer.read().decode('utf-8');exec(compile(SOURCE,'<fabric-probe>','exec'))"


class ProbeDeadline(Exception):
    pass


def terminate(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


def command(argv, timeout=8, limit=128000):
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            terminate(proc)
            output, _ = proc.communicate()
            rc = 124
        except BaseException:
            terminate(proc)
            proc.communicate()
            raise
        return dict(rc=rc, text=output[:limit], truncated=len(output) > limit,
                    seconds=round(time.monotonic() - start, 3))
    except OSError as exc:
        return dict(rc=127, text=str(exc), truncated=False)


def successful(result):
    return result.get("rc") == 0 and not result.get("truncated", False)


def parse_mapping(result):
    """仅解析已知 -m 表头；不从 npu-smi 主状态表猜数量或使用 card*2。"""
    text = result.get("text", "")
    if not successful(result) or not re.search(
            r"NPU ID\s+Chip ID\s+Chip Logic ID\s+Chip Phy-ID\s+Chip Name", text):
        raise ValueError("无法识别 npu-smi -m 映射表；保留原文，需版本适配")
    devices = []
    for line in text.splitlines():
        if not re.match(r"^\s*\d+\s+", line):
            continue
        fields = line.split()
        if len(fields) == 5 and fields[2:4] == ["-", "-"] and fields[4].lower() == "mcu":
            continue
        if len(fields) != 5 or not all(re.fullmatch(r"\d+", f) for f in fields[:4]):
            raise ValueError("映射存在未知或不完整计算设备行，禁止部分枚举")
        card, chip, logical, physical = map(int, fields[:4])
        devices.append(dict(card=card, chip=chip, logical=logical, physical=physical,
                            chip_name=fields[4]))
    if not devices or len(devices) > 256:
        raise ValueError("映射无计算设备或超过本工具单节点 256 设备上限")
    for keys in (("card", "chip"), ("logical",), ("physical",)):
        if len({tuple(d[k] for k in keys) for d in devices}) != len(devices):
            raise ValueError("设备映射重复")
    return devices


def field(result, label):
    if not successful(result):
        return None
    values = re.findall(r"^\s*" + re.escape(label) + r"\s*:\s*([^\r\n]+)$",
                        result.get("text", ""), re.M)
    return values[0].strip() if len(values) == 1 else None


def number(result, label):
    value = field(result, label)
    return int(value) if value and value.isdecimal() else None


def ipv4(value):
    try:
        addr = ipaddress.IPv4Address(value)
        return str(addr) if not (addr.is_unspecified or addr.is_multicast or addr.is_loopback) else None
    except (ValueError, TypeError):
        return None


def parse_device(raw):
    return dict(**raw["mapping"], vnic_ip=ipv4(field(raw.get("vnic", {}), "vnic ipaddr")),
                vnic_link=field(raw.get("vnic", {}), "vnic link status"),
                pod=number(raw.get("spod", {}), "Super Pod ID"),
                pod_size=number(raw.get("spod", {}), "Super Pod Size"),
                server_index=number(raw.get("spod", {}), "Server Index"),
                sdid=number(raw.get("spod", {}), "SDID"),
                roce_link=field(raw.get("link", {}), "link status"),
                roce_ip_state="NOT_CONFIGURED" if raw.get("ip", {}).get("rc") == 13 and
                "no ip was preset" in raw["ip"].get("text", "") else "UNVERIFIED",
                hccs_health=field(raw.get("hccs", {}), "hccs health status"))


def collect():
    queries = {"mapping": ["npu-smi", "info", "-m"],
               "list": ["npu-smi", "info", "-l"],
               "npu_help": ["npu-smi", "info", "--help"],
               "hccn_help": ["hccn_tool", "-h"],
               "host_ip": ["ip", "-j", "address", "show"],
               "host_routes": ["ip", "-j", "route", "show"],
               "rdma": ["rdma", "link", "show"], "urma": ["urma_admin", "show"]}
    result = {k: command(v) for k, v in queries.items()}
    result.update(hostname=socket.gethostname(), sampled_at=time.time())
    result["paths"] = {p: "directory" if Path(p).is_dir() else "file" if Path(p).is_file()
                       else "other" if Path(p).exists() else "missing" for p in ("/dev/uburma", "/dev/ummu", "/sys/class/ubcore",
                                                "/etc/hccl_rootinfo.json", "/etc/hixlep.json",
                                                "/etc/hixlep", "/lib/route.conf")}
    result["devices"], result["raw_devices"] = [], []
    try:
        mapping = parse_mapping(result["mapping"])
    except ValueError as exc:
        result["mapping_error"] = str(exc)
        return result
    npu_help = result["npu_help"].get("text", "") if successful(result["npu_help"]) else ""
    hccn_help = result["hccn_help"].get("text", "") if successful(result["hccn_help"]) else ""
    if re.search(r"\btopo\b", npu_help):
        result["topo"] = command(["npu-smi", "info", "-t", "topo"])
    for m in mapping:
        raw = {"mapping": m}
        for key in ("ip", "link", "net_health", "vnic", "lldp"):
            if re.search(r"hccn_tool\s+-i\s+\w+\s+-" + key + r"\b", hccn_help):
                raw[key] = command(["hccn_tool", "-i", str(m["physical"]), "-" + key, "-g"])
        for key, kind in (("spod", "spod-info"), ("hccs", "hccs"), ("board", "board")):
            if re.search(r"\b" + kind + r"\b", npu_help):
                raw[key] = command(["npu-smi", "info", "-t", kind,
                                    "-i", str(m["card"]), "-c", str(m["chip"])])
        result["raw_devices"].append(raw)
        result["devices"].append(parse_device(raw))
    return result


def classify(inventory):
    devices = inventory.get("devices", [])
    topo = inventory.get("topo", {})
    text = topo.get("text", "") if successful(topo) else ""
    # 只认矩阵行中的 token，不把图例列出 HCCS_SW 当成实际拓扑。
    tokens = {v for line in text.splitlines() if re.match(r"^\s*Phy-ID\d+\s", line)
              for v in line.split()[1:]}
    no_roce = bool(devices) and all(d["roce_link"] == "DOWN" and
                                                  d["roce_ip_state"] == "NOT_CONFIGURED" for d in devices)
    hccs = "HCCS_SW" in tokens or "HCCS" in tokens or any(d["hccs_health"] == "OK" for d in devices)
    paths = inventory.get("paths", {})
    ub = any(paths.get(p) in {"file", "directory", "other"} for p in ("/dev/uburma", "/dev/ummu", "/sys/class/ubcore"))
    urma = inventory.get("urma", {})
    ub = ub or (successful(urma) and bool(urma.get("text", "").strip()))
    return dict(hccs="OBSERVED" if hccs else "UNVERIFIED",
                intranode_topology=sorted(tokens & {"HCCS_SW", "HCCS", "SIO", "PIX", "PHB", "SYS"}),
                roce="NOT_CONFIGURED_LINK_DOWN" if no_roce else "UNVERIFIED",
                ub_uboe="CANDIDATE_REQUIRES_LINK_EVIDENCE" if ub else "NO_EVIDENCE",
                inter_node_physical_fullmesh="UNVERIFIED", hccl_algo="NOT_COLLECTED",
                scope="设备状态不是跨机通过；缺工具不等于硬件不支持；HCCS_SW 不等于物理直连")


def collisions(inventories):
    output = []
    for key in ("vnic_ip", "sdid"):
        groups = {}
        for name, inv in inventories.items():
            for d in inv.get("devices", []):
                if d.get(key) is not None:
                    groups.setdefault(d[key], []).append(dict(node=name, device=d["physical"], pod=d["pod"]))
        for value, owners in groups.items():
            if len(owners) > 1:
                output.append(dict(field=key, value=value, owners=owners,
                                   reason="跨域重叠不必然是冲突；同一可寻址域内须唯一，不能将本机响应算远端通过"))
    return output


def find_device(inventories, node, physical):
    return next((d for d in inventories[node].get("devices", []) if d["physical"] == physical), None)


def plan_edge(inventories, source, target, a, b):
    row = dict(source=source, target=target, source_device=a, target_device=b, status="UNVERIFIED")
    src, dst = find_device(inventories, source, a), find_device(inventories, target, b)
    if not src or not dst:
        return dict(row, reason="源或目标设备没有成功采集的映射")
    row.update(address=dst["vnic_ip"], source_pod=src["pod"], target_pod=dst["pod"],
               source_sdid=src["sdid"], target_sdid=dst["sdid"], source_vnic_ip=src["vnic_ip"],
               target_vnic_ip=dst["vnic_ip"], source_server_index=src["server_index"],
               target_server_index=dst["server_index"])
    if source == target and a == b:
        return dict(row, reason="拒绝把设备自 ping 当成跨设备检测")
    if any(d[k] is None for d in (src, dst) for k in ("vnic_ip", "pod", "sdid", "server_index")):
        return dict(row, reason="缺少有效 vNIC、Pod、SDID 或 Server Index，无法归属对端")
    if src["vnic_link"] != "UP" or dst["vnic_link"] != "UP":
        return dict(row, reason="vNIC 非 UP 或状态未知，先核实设备链路")
    for name, inv in inventories.items():
        for d in inv.get("devices", []):
            if name == target and d["physical"] == b:
                continue
            overlap = d["vnic_ip"] == dst["vnic_ip"] or d["sdid"] == dst["sdid"]
            if overlap and (name == source or d["pod"] in (None, src["pod"], dst["pod"])):
                return dict(row, reason="目标 vNIC/SDID 与本机或相关通信域其他设备重叠，拒绝误归属")
    if source != target and src["pod"] == dst["pod"] and src["server_index"] == dst["server_index"]:
        return dict(row, reason="同 Pod 的不同主机 Server Index 重复，先核实是否重复 SSH 入口")
    row.update(status="PLANNED", attribution="same_pod" if src["pod"] == dst["pod"] else "cross_pod_unverified")
    return row


def parse_ping(result, count=3):
    text = result.get("text", "")
    stats = re.findall(r"(\d+) packets transmitted, (\d+) received, ([0-9.]+)% packets loss", text)
    failure = re.search(r"send fail|not start to send|ping fail|timed?\s*out", text, re.I)
    failure = failure or any(int(v, 16) != 0 for v in re.findall(r"L1 plane check result\s*=\s*(0x[0-9a-fA-F]+)", text))
    if result.get("rc") != 0 or failure:
        status = "FAIL"
    elif not successful(result) or len(stats) != 1:
        status = "UNVERIFIED"
    else:
        sent, received, loss = stats[0]
        status = "PASS" if int(sent) == count and int(received) == count and float(loss) == 0 else "FAIL"
    answer = dict(status=status)
    if len(stats) == 1:
        answer.update(transmitted=int(stats[0][0]), received=int(stats[0][1]), loss_percent=float(stats[0][2]))
    return answer


def ping_supported(result):
    if not successful(result):
        return False
    sections = re.split(r"(?=hccn_tool\s+-i)", result.get("text", ""))
    return any(re.match(r"hccn_tool\s+-i\s+\w+\s+-hccs_ping\b", s) and
               all(re.search(r"\b" + word + r"\s+%[sd]", s) for word in ("address", "cnt", "timeout"))
               for s in sections)


def current_identity(physical):
    try:
        mapping = parse_mapping(command(["npu-smi", "info", "-m"]))
    except ValueError as exc:
        return dict(error=str(exc))
    m = next((d for d in mapping if d["physical"] == physical), None)
    if not m:
        return dict(error="设备映射变化")
    spod = command(["npu-smi", "info", "-t", "spod-info", "-i", str(m["card"]), "-c", str(m["chip"])])
    vnic = command(["hccn_tool", "-i", str(m["physical"]), "-vnic", "-g"])
    return parse_device(dict(mapping=m, spod=spod, vnic=vnic))


def identity_matches(identity, payload, prefix):
    return identity.get("vnic_link") == "UP" and all(
        identity.get(k) is not None and identity[k] == payload[prefix + "_" + k]
        for k in ("pod", "sdid", "vnic_ip", "server_index"))


def ping_one(payload):
    if not ping_supported(command(["hccn_tool", "-h"])):
        return dict(status="UNVERIFIED", reason="当前工具帮助未确认有界 hccs_ping 参数")
    # 每次执行前重读本机身份与 vNIC，防止计划后卡映射或地址变更。
    identity = current_identity(payload["source_device"])
    if not identity_matches(identity, payload, "source"):
        return dict(status="UNVERIFIED", reason="源设备身份/vNIC 变化或无法核实，请重新采集")
    argv = ["hccn_tool", "-i", str(payload["source_device"]), "-hccs_ping", "-g", "address", payload["address"],
            "cnt", "3", "timeout", "500"]
    raw = command(argv, timeout=10)
    return dict(**parse_ping(raw), command=argv, raw=raw)


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"nodes"}:
        raise ValueError("设备网络配置仅接受 nodes；不接受密码、容器或部署环境字段")
    nodes = config["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 64:
        raise ValueError("nodes 须有 1～64 个明确节点")
    names = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"name", "ssh", "port", "identity_file", "python"}:
            raise ValueError("节点只接受 name/ssh/port/identity_file/python，不接受密码")
        for key in ("name", "ssh"):
            value = node.get(key)
            pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}" if key == "name" else r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*"
            if not isinstance(value, str) or not re.fullmatch(pattern, value) or "REPLACE" in value.upper():
                raise ValueError("请填写有效的 " + key + "；SSH 支持 IPv4/主机名/别名，IPv6 请使用 SSH 别名")
        if node["name"] in names or node["ssh"] == "local":
            raise ValueError("节点名不能重复；本工具要求明确 SSH 目标，不接受 local")
        names.add(node["name"])
        port = node.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("SSH port 须为 1～65535 整数")
        if not isinstance(node.get("python", "python3"), str) or not re.fullmatch(r"[A-Za-z0-9_/.-]+", node.get("python", "python3")):
            raise ValueError("python 须为单个可执行文件名或路径")
        if "identity_file" in node and (not isinstance(node["identity_file"], str) or not node["identity_file"] or
                                       any(c in node["identity_file"] for c in "\r\n\0")):
            raise ValueError("identity_file 须为本地密钥路径，不是私钥内容")
    return config


def remote_argv(node, payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-p", str(node.get("port", 22))]
    if node.get("identity_file"):
        argv += ["-i", node["identity_file"], "-o", "IdentitiesOnly=yes"]
    return argv + ["--", node["ssh"], shlex.join([node.get("python", "python3"), "-u", "-c", BOOT, "--agent", encoded])]


def remote(node, payload):
    try:
        proc = subprocess.run(remote_argv(node, payload), input=SOURCE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                              timeout=payload.get("budget_s", 600) + 20)
        records = [json.loads(line[len(PREFIX):]) for line in proc.stdout.splitlines() if line.startswith(PREFIX)]
        if proc.returncode == 0 and len(records) == 1:
            return records[0]
        return dict(error="SSH/远端探针失败或未返回唯一结果", rc=proc.returncode,
                    diagnostics=proc.stderr[-2000:])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return dict(error="SSH/远端探针失败", detail=str(type(exc).__name__))


def execute_edge(nodes, inventories, row):
    if row["status"] != "PLANNED":
        return row
    if not ping_supported(inventories[row["source"]].get("hccn_help", {})):
        return dict(row, status="UNVERIFIED", reason="当前版本帮助未确认有界 hccs_ping 参数")
    identity = remote(nodes[row["target"]], dict(action="identity", physical=row["target_device"], budget_s=30))
    if not identity_matches(identity, row, "target"):
        return dict(row, status="UNVERIFIED", reason="目标设备身份/vNIC 变化或无法核实，请重新采集")
    result = remote(nodes[row["source"]], dict(row, action="ping", budget_s=55))
    status = result.get("status", "UNVERIFIED")
    if status == "PASS" and row["attribution"] != "same_pod":
        return dict(row, status="UNVERIFIED", result=result,
                    reason="跨 Pod 收到响应但不能证实对端身份，需要交换侧/路由证据")
    return dict(row, status=status, result=result)


def run(config, mode="inspect", source=None, target=None, pairs=None, same_index=False,
        bidirectional=False, execute=False):
    validate_config(config)
    nodes = {n["name"]: n for n in config["nodes"]}
    if mode == "ping" and (source not in nodes or target not in nodes or not (pairs or same_index)):
        raise ValueError("ping 须明确 source、target 与 pair 或 same-index；不默认测全部节点")
    if pairs and same_index:
        raise ValueError("pair 与 same-index 只能选择一个")
    pairs = list(pairs or [])
    if any(len(p) != 2 or any(type(i) is not int or not 0 <= i <= 65535 for i in p) for p in pairs):
        raise ValueError("pair 须为两个非负物理设备 ID")
    if len(pairs) != len(set(pairs)) or len(pairs) * (2 if bidirectional else 1) > 512:
        raise ValueError("pair 重复或超过单次 512 条有向边上限")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        inventories = dict(zip(nodes, pool.map(lambda n: remote(n, dict(action="inspect", budget_s=600)), nodes.values())))
    report = dict(schema_version=1, created_at=time.time(), mode=mode, status="UNVERIFIED",
                  inventory=inventories, classification={k: classify(v) for k, v in inventories.items()},
                  collisions=collisions(inventories), edges=[], coverage_notes=[],
                  unverified=["TCPStore", "Gloo", "HCCL collective", "MC2", "KV", "模型服务", "跨机物理 FullMesh"])
    if mode != "ping":
        return report
    if same_index:
        a = {d["physical"] for d in inventories[source].get("devices", [])}
        b = {d["physical"] for d in inventories[target].get("devices", [])}
        pairs = [(i, i) for i in sorted(a & b)]
        if a != b:
            report["coverage_notes"].append("两节点设备 ID 不同；未匹配设备未测，不可称全部设备通过")
    if len(pairs) * (2 if bidirectional else 1) > 512:
        raise ValueError("发现的设备对超过单次 512 条有向边上限")
    plans = [plan_edge(inventories, source, target, a, b) for a, b in pairs]
    if bidirectional:
        plans += [plan_edge(inventories, target, source, b, a) for a, b in pairs]
    # 顺序执行：不并发打同一张卡，不隐式扩展全卡对矩阵。
    report["edges"] = [execute_edge(nodes, inventories, row) if execute else row for row in plans]
    states = [e["status"] for e in report["edges"]]
    report["status"] = "FAIL" if "FAIL" in states else "PASS" if states and all(s == "PASS" for s in states) and not report["coverage_notes"] else "UNVERIFIED"
    report["scope"] = "仅列出的有向设备边、当前宿主机 HCCS 小包；不是全卡对、collective 或服务验收"
    return report


def markdown(report):
    lines = ["# 宿主机设备网络检测", "", "结果：" + report["status"], "",
             "只代表已列出的设备与方向；不进入容器、不证明 HCCL/MC2/服务通过。", "",
             "| 节点 | HCCS | 机内拓扑 | RoCE | UB/UBoE |", "|---|---|---|---|---|"]
    for name, c in report["classification"].items():
        lines.append(f'| {name} | {c["hccs"]} | {", ".join(c["intranode_topology"])} | {c["roce"]} | {c["ub_uboe"]} |')
    lines += ["", "物理 FullMesh 与 HCCL_ALGO 均未验证；缺工具不是硬件不支持。", "",
              "| 节点 | 物理 ID | card/chip | vNIC | Pod ID / Size / Server Index | SDID |",
              "|---|---|---|---|---|---|"]
    for name, inv in report["inventory"].items():
        for d in inv.get("devices", []):
            lines.append(f'| {name} | {d["physical"]} | {d["card"]}/{d["chip"]} | {d["vnic_ip"]} | {d["pod"]} / {d["pod_size"]} / {d["server_index"]} | {d["sdid"]} |')
        if inv.get("error") or inv.get("mapping_error"):
            lines.append(f'| {name} | 未完成采集，查看 JSON 中错误 | — | — | — | — |')
    lines += ["",
              "| 源 → 目标（物理设备 ID） | 状态 | 说明 |", "|---|---|---|"]
    for edge in report["edges"]:
        tested = edge.get("result", {})
        stats = f'发 {tested.get("transmitted", "未知")} / 收 {tested.get("received", "未知")} / 丢包 {tested.get("loss_percent", "未知")}%'
        reason = edge.get("reason", tested.get("reason", stats if tested else "仅计划，未发送"))
        lines.append(f'| {edge["source"]}:{edge["source_device"]} → {edge["target"]}:{edge["target_device"]} | {edge["status"]} | {reason} |')
    lines += ["", f'检测到 {len(report["collisions"])} 项地址/SDID 重叠，详见 JSON。跨域重叠不自动判冲突。', ""]
    lines += report["coverage_notes"]
    lines += ["", "未验证：" + "、".join(report["unverified"]), ""]
    return "\n".join(lines)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--agent":
        payload = json.loads(base64.b64decode(sys.argv[2]))
        if os.name != "posix":
            raise ValueError("被测端需要 Linux")
        # 总预算限制远端生命周期；每个子命令另有超时。超时后不继续派发。
        def expired(_signum, _frame):
            raise ProbeDeadline("远端总预算耗尽，未完成项保持未验证")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(payload["budget_s"])
        try:
            if payload["action"] == "inspect":
                result = collect()
            elif payload["action"] == "identity":
                result = current_identity(payload["physical"])
            elif payload["action"] == "ping":
                result = ping_one(payload)
            else:
                raise ValueError("未知探针动作")
        except ProbeDeadline as exc:
            result = dict(error=str(exc))
        finally:
            signal.alarm(0)
        print(PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inspect", "ping"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True, help="新 JSON 路径，同时生成同名 .md；不覆盖旧文件")
    parser.add_argument("--source")
    parser.add_argument("--target")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--pair", action="append", help="源物理ID:目标物理ID，可重复；必须来自现场 -m")
    selection.add_argument("--same-index", action="store_true", help="按两端发现的相同物理 ID 匹配，不是全卡对")
    parser.add_argument("--bidirectional", action="store_true")
    parser.add_argument("--execute", action="store_true", help="明确授权本次有界 HCCS 小包，否则只生成计划")
    args = parser.parse_args()
    try:
        dest = Path(args.out)
        md = dest.with_suffix(".md")
        if dest.suffix != ".json" or dest.exists() or md.exists():
            raise ValueError("out 必须是未使用的 .json 路径，配套 .md 也不能存在")
        if args.mode == "inspect" and any((args.source, args.target, args.pair, args.same_index, args.bidirectional, args.execute)):
            raise ValueError("inspect 不接受主动测试参数")
        pairs = [tuple(map(int, p.split(":"))) for p in (args.pair or [])]
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        report = run(config, args.mode, args.source, args.target, pairs,
                     args.same_index, args.bidirectional, args.execute)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        with md.open("x", encoding="utf-8") as handle:
            handle.write(markdown(report))
        print(json.dumps(dict(status=report["status"], report=str(dest)), ensure_ascii=False))
        return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[report["status"]]
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())

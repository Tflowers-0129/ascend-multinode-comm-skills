#!/usr/bin/env python3
"""昇腾多机通信预检：标准库控制器 + 同命名空间、有限生命周期探针。"""
import argparse
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

PREFIX = "A5COMM "
SOURCE = globals().get("SOURCE") or Path(__file__).read_text(encoding="utf-8")
BOOT = "import sys;SOURCE=sys.stdin.buffer.read().decode('utf-8');exec(compile(SOURCE,'<a5-preflight>','exec'))"
STAGES = {"tcpstore", "gloo", "hccl"}
ENV_KEYS = {"HCCL_IF_IP", "HCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "HCCL_ALGO",
            "HCCL_IF_BASE_PORT", "HCCL_HOST_SOCKET_PORT_RANGE", "ASCEND_RT_VISIBLE_DEVICES",
            "ASCEND_VISIBLE_DEVICES", "PYTHONHASHSEED", "HCCL_CONNECT_TIMEOUT",
            "HCCL_EXEC_TIMEOUT", "ASCEND_GLOBAL_RESOURCE_CONFIG"}


def emit(kind, **kw):
    print(PREFIX + json.dumps(dict(event=kind, time=time.time(), **kw), ensure_ascii=False), flush=True)


def encode(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def stop_owned(p):
    """仅终止本工具创建的会话/进程组；不查找或杀死用户进程。"""
    if p.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        p.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
            p.wait(timeout=3)
        except ProcessLookupError:
            pass


def run_cmd(argv, timeout=8):
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            return {"rc": p.returncode, "text": out[-16000:]}
        except subprocess.TimeoutExpired:
            stop_owned(p)
            return {"rc": 124, "text": "TIMEOUT"}
    except OSError as e:
        return {"rc": 127, "text": str(e)}


def file_info(path):
    p = Path(path)
    result = {"path": path, "exists": p.exists(), "is_dir": p.is_dir()}
    try:
        if p.is_file():
            result["size"] = p.stat().st_size
            if result["size"] <= 2 * 1024 * 1024:
                b = p.read_bytes()
                result["sha256"] = hashlib.sha256(b).hexdigest()
                if p.suffix == ".json":
                    obj = json.loads(b)
                    result["json_type"] = type(obj).__name__
                    result["top_keys"] = sorted(obj) if isinstance(obj, dict) else []
        elif p.is_dir():
            result["entries"] = sorted(x.name for x in p.iterdir())[:128]
    except (OSError, ValueError) as e:
        result["error"] = str(e)
    return result


def read_small(path):
    try:
        return Path(path).read_text().strip()[:4096]
    except OSError:
        return None


def inventory(payload):
    commands = {"ip": ["ip", "-j", "address", "show"],
                "routes": ["ip", "-j", "route", "show"],
                "rdma": ["rdma", "link", "show"], "urma": ["urma_admin", "show"],
                "npu": ["npu-smi", "info"], "mapping": ["npu-smi", "info", "-m"],
                "listeners": ["ss", "-ltn"],
                "versions": [sys.executable, "-c",
                    "import importlib.metadata as m,json; names=['torch','torch-npu','vllm','vllm-ascend'];"
                    "ds={d.metadata['Name'].lower():d.version for d in m.distributions()};"
                    "print(json.dumps({x:ds.get(x,'MISSING') for x in names}))"]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        data = dict(zip(commands, ex.map(run_cmd, commands.values())))
    data["hostname"] = socket.gethostname()
    data["clock"] = time.time()
    data["env"] = {k: os.environ[k] for k in sorted(ENV_KEYS) if k in os.environ}
    data["proxy_present"] = [k for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if os.getenv(k)]
    data["no_proxy"] = os.getenv("no_proxy", os.getenv("NO_PROXY", ""))
    data["files"] = [file_info(x) for x in ("/etc/hccl_rootinfo.json", "/etc/hixlep.json", "/etc/hixlep", "/lib/route.conf", "/usr/local/Ascend/driver/version.info")]
    data["mounts"] = []
    try:
        data["mounts"] = [line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                          if any(x in line for x in ("/etc/hccl_rootinfo.json", "/etc/hixlep", "/lib/route.conf"))]
    except OSError:
        pass
    data["ub_devices"] = [str(p) for p in Path("/dev").glob("ub*")]
    data["rdma_devices"] = [p.name for p in Path("/sys/class/infiniband").glob("*")]
    data["rdma_ports"] = []
    for port in Path("/sys/class/infiniband").glob("*/ports/*"):
        data["rdma_ports"].append({"device": port.parent.parent.name, "port": port.name,
                                  "link_layer": read_small(port / "link_layer"),
                                  "state": read_small(port / "state"),
                                  "gid_types": sorted({v for f in (port / "gid_attrs/types").glob("*") if (v := read_small(f))})})
    data["hccn"] = {}
    # hccn 使用物理卡号；不能从 torch logical index 猜测。
    for dev in payload.get("physical_devices", []):
        data["hccn"][str(dev)] = {field: run_cmd(["hccn_tool", "-i", str(dev), "-" + field, "-g"], 5)
                                  for field in ("ip", "link", "net_health", "lldp")}
    return data


def candidates(inv, cidr=None):
    if inv["ip"]["rc"] != 0:
        raise ValueError("无法通过 ip -j 获取网卡")
    network = ipaddress.ip_network(cidr) if cidr else None
    rows = []
    for nic in json.loads(inv["ip"]["text"]):
        if nic.get("operstate") != "UP" or "UP" not in nic.get("flags", []):
            continue
        if re.match(r"^(lo$|docker|veth|virbr|br-)", nic["ifname"]):
            continue
        for addr in nic.get("addr_info", []):
            if addr.get("family") != "inet" or addr.get("scope") != "global":
                continue
            ip = ipaddress.ip_address(addr["local"])
            if ip.is_loopback or ip.is_link_local or (network and ip not in network):
                continue
            rows.append({"ip": str(ip), "nic": nic["ifname"], "mtu": nic.get("mtu"),
                         "network": str(ipaddress.ip_network(str(ip) + "/" + str(addr["prefixlen"]), strict=False))})
    return rows


def select_addresses(nodes, inventories, cidr=None):
    choices = {n["name"]: candidates(inventories[n["name"]], cidr) for n in nodes}
    for n in nodes:
        if n.get("data_ip", "auto") != "auto":
            choices[n["name"]] = [x for x in choices[n["name"]] if x["ip"] == n["data_ip"]]
    common = set.intersection(*({x["network"] for x in xs} for xs in choices.values()))
    if not cidr and common:
        choices = {k: [x for x in xs if x["network"] in common] for k, xs in choices.items()}
    if any(len(xs) != 1 for xs in choices.values()):
        raise ValueError("业务网卡不唯一或不存在，请指定 fabric_cidr/data_ip：" + json.dumps(choices))
    selected = {k: xs[0] for k, xs in choices.items()}
    if len({x["ip"] for x in selected.values()}) != len(selected):
        raise ValueError("节点业务 IP 重复")
    return selected


def topology(inv):
    evidence = []
    capabilities = []
    if any(port.get("link_layer") == "Ethernet" and any("RoCE" in g for g in port.get("gid_types", [])) for port in inv.get("rdma_ports", [])):
        capabilities.append("RoCE")
    if inv.get("rdma_devices"):
        evidence.append("发现 RDMA 设备；需 link_layer 与实际传输日志确认 RoCE")
    if inv.get("ub_devices") or (inv.get("urma", {}).get("rc") == 0 and inv["urma"]["text"].strip()):
        evidence.append("发现 UB/URMA 设备或管理输出；需拓扑/链路及运行日志确认 UB/UBoE")
        capabilities.append("UB/URMA")
    algo = inv.get("env", {}).get("HCCL_ALGO", "")
    return {"transport": "UNVERIFIED", "capability_candidates": capabilities, "physical_topology": "UNVERIFIED",
            "configured_algo": algo, "fullmesh_configured": "level0:fullmesh" in algo,
            "evidence": evidence, "note": "物理 fullmesh 需全端口邻接表；collective 成功不是物理直连证明"}


def dns_probe(payload):
    results = []
    for peer in payload["peers"]:
        start = time.monotonic()
        # NSS/getnameinfo 不保证服从 socket timeout，必须用可终止的子进程。
        program = ("import socket,json;ip=" + repr(peer) + ";name=socket.getnameinfo((ip,0),0)[0];"
                   "print(json.dumps({'name':name,'forward':sorted({x[4][0] for x in socket.getaddrinfo(name,None)})}))")
        r = run_cmd([sys.executable, "-c", program], payload.get("dns_timeout_s", 4))
        item = {"peer": peer, "elapsed_ms": round((time.monotonic()-start)*1000, 2), **r}
        if r["rc"] == 0:
            obj = json.loads(r["text"])
            item["consistent"] = peer in obj["forward"]
        results.append(item)
    return results


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("对端提前关闭连接")
        data.extend(chunk)
    return bytes(data)


def serve(payload):
    listeners = []
    try:
        for port in payload["ports"]:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(s)
            s.bind((payload["ip"], port))
            s.listen(64)
            s.settimeout(0.2)
        # 所有 bind 成功后才发 ready；不能碰已有业务服务。
        emit("ready", ip=payload["ip"], ports=payload["ports"])
        deadline = time.monotonic() + payload["timeout_s"]
        counts = {}

        def handle(s, port):
            done = set()
            while len(done) < payload["expected"] and time.monotonic() < deadline:
                try:
                    c, _ = s.accept()
                except socket.timeout:
                    continue
                with c:
                    c.settimeout(min(5, max(.1, deadline - time.monotonic())))
                    try:
                        if exact(c, 64) != payload["token"].encode():
                            continue
                        client = exact(c, 32)
                        for size in payload["sizes"]:
                            if struct.unpack("!I", exact(c, 4))[0] != size:
                                raise ValueError("长度不匹配")
                            data = exact(c, size)
                            c.sendall(hashlib.sha256(data).digest())
                        done.add(client)
                    except (OSError, ValueError):
                        continue
            counts[port] = len(done)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(listeners)) as ex:
            list(ex.map(lambda v: handle(*v), zip(listeners, payload["ports"])))
        if any(x != payload["expected"] for x in counts.values()):
            raise TimeoutError("未收到全部预期客户端：" + str(counts))
        return {"received": counts}
    finally:
        for s in listeners:
            s.close()


def dial(payload):
    results = []
    for target in payload["targets"]:
        for port in target["ports"]:
            start = time.monotonic()
            r = {"source": payload["ip"], "target": target["ip"], "port": port}
            try:
                with socket.socket() as s:
                    s.settimeout(payload.get("connect_timeout_s", 4))
                    s.bind((payload["ip"], 0))
                    s.connect((target["ip"], port))
                    r["handshake_ms"] = round((time.monotonic() - start)*1000, 2)
                    s.sendall(target["token"].encode() + hashlib.sha256(payload["ip"].encode()).digest())
                    for size in payload["sizes"]:
                        data = bytes((i % 251 for i in range(size)))
                        s.sendall(struct.pack("!I", size) + data)
                        if exact(s, 32) != hashlib.sha256(data).digest():
                            raise ValueError("数据摘要错误")
                    r["status"] = "PASS"
                    r["sizes"] = payload["sizes"]
            except (OSError, ValueError) as e:
                r.update(status="FAIL", error=type(e).__name__ + ": " + str(e))
            results.append(r)
    return results


def collective(payload):
    from datetime import timedelta
    import torch
    import torch.distributed as dist
    stage, rank, world = payload["stage"], payload["rank"], payload["world"]
    timeout = timedelta(seconds=payload["timeout_s"])
    emit("phase", phase="imported", rank=rank, stage=stage, torch=torch.__version__)
    device = "cpu"
    if stage == "hccl":
        import torch_npu  # noqa: F401
        index = payload["device"]
        if index >= torch.npu.device_count():
            raise ValueError("逻辑卡超出当前容器可见卡范围")
        torch.npu.set_device(index)
        device = "npu:" + str(index)
    emit("phase", phase="tcpstore_enter", rank=rank, stage=stage)
    store = dist.TCPStore(payload["master"], payload["port"], world, rank == 0, timeout,
                          wait_for_workers=True, use_libuv=payload.get("use_libuv", True))
    store.set("rank/" + str(rank), "ok")
    store.wait(["rank/" + str(i) for i in range(world)], timeout)
    emit("phase", phase="tcpstore_ready", rank=rank, stage=stage)
    if stage == "tcpstore":
        for i in range(world):
            if store.get("rank/" + str(i)) != b"ok":
                raise ValueError("TCPStore 内容错误")
        store.set("done/" + str(rank), "1")
        store.wait(["done/" + str(i) for i in range(world)], timeout)
        # 最后由 rank0 确认所有客户端完成读写；客户端提交退出许可。
        if rank != 0:
            store.set("leave/" + str(rank), "1")
        else:
            store.wait(["leave/" + str(i) for i in range(1, world)], timeout)
        return {"stage": stage, "rank": rank}
    dist.init_process_group(stage, store=store, rank=rank, world_size=world, timeout=timeout)
    emit("phase", phase="group_ready", rank=rank, stage=stage, device=device)
    try:
        for count in payload.get("elements", [16, 4096]):
            for repeat in range(payload.get("repeats", 2)):
                x = torch.full((count,), float(rank + 1), dtype=torch.float32, device=device)
                dist.all_reduce(x)
                if not bool(torch.all(x == world * (world + 1) / 2).item()):
                    raise ValueError("all_reduce 数值错误")
                x.fill_(rank + 1)
                out = [torch.empty_like(x) for _ in range(world)]
                dist.all_gather(out, x)
                if not all(bool(torch.all(y == i + 1).item()) for i, y in enumerate(out)):
                    raise ValueError("all_gather 数值错误")
                x = torch.cat([torch.full((count,), float(rank * world + j), device=device) for j in range(world)])
                y = torch.empty_like(x)
                dist.all_to_all_single(y, x)
                want = torch.cat([torch.full((count,), float(j * world + rank), device=device) for j in range(world)])
                if not bool(torch.equal(y, want)):
                    raise ValueError("all_to_all_single 数值错误")
                dist.barrier()
                if stage == "hccl":
                    torch.npu.synchronize()
                emit("phase", phase="collectives_checked", rank=rank, stage=stage, elements=count, repeat=repeat)
        return {"stage": stage, "rank": rank, "device": device}
    finally:
        dist.destroy_process_group()


def adapter(payload):
    """适配器 argv 只来自用户配置，不来自日志或远端返回内容。"""
    p = subprocess.Popen(payload["argv"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    output, _ = p.communicate()
    if p.returncode != 0:
        raise RuntimeError("适配器失败 rc=" + str(p.returncode) + "\n" + output[-8000:])
    lines = [line[len("A5_ADAPTER "):] for line in output.splitlines() if line.startswith("A5_ADAPTER ")]
    if not lines:
        raise ValueError("适配器没有输出 A5_ADAPTER 结构化验收证据")
    evidence = json.loads(lines[-1])
    if evidence.get("status") != "PASS" or any(evidence.get("checks", {}).get(k) is not True for k in payload["required_checks"]):
        raise ValueError("适配器缺少必要验收项：" + json.dumps(evidence))
    return evidence


def supervised(payload):
    """远端 watchdog 在目标命名空间内，生命周期不依赖 SSH 是否保持连接。"""
    jobs = payload.get("workers") or [dict(payload, supervised=True)]
    children = []

    def shutdown(*_):
        for child in children:
            stop_owned(child)
        raise SystemExit(130)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    try:
        for job in jobs:
            job["supervised"] = True
            p = subprocess.Popen([sys.executable, "-u", "-c", BOOT, "--agent", encode(job)],
                                 stdin=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True)
            children.append(p)
            p.stdin.write(SOURCE)
            p.stdin.close()
        end = time.monotonic() + payload["timeout_s"]
        while any(p.poll() is None for p in children):
            if any(p.poll() not in (None, 0) for p in children):
                raise RuntimeError("本节点 worker 失败，终止同组本工具 worker")
            if time.monotonic() > end:
                raise TimeoutError("远端 watchdog 超时，回收本次测试进程组")
            time.sleep(.05)
        if any(p.returncode != 0 for p in children):
            raise RuntimeError("worker 非零退出")
        emit("complete", status="PASS")
    finally:
        for p in children:
            stop_owned(p)


def agent(payload):
    if not payload.get("supervised"):
        return supervised(payload)
    functions = {"inventory": inventory, "dns": dns_probe, "serve": serve,
                 "dial": dial, "collective": collective, "adapter": adapter}
    result = functions[payload["action"]](payload)
    emit("result", action=payload["action"], result=result)


def validate_config(cfg):
    if cfg.get("schema_version") != 1 or cfg.get("mode") not in {"colocated", "disaggregated", "pooled"}:
        raise ValueError("schema_version/mode 错误")
    nodes = cfg.get("nodes", [])
    if not 2 <= len(nodes) <= 64:
        raise ValueError("需要 2~64 个显式节点")
    names = [n["name"] for n in nodes]
    if len(set(names)) != len(names):
        raise ValueError("节点名重复")
    for n in nodes:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", n["ssh"]):
            raise ValueError("SSH 目标格式无效")
        if "REPLACE" in n.get("container", ""):
            raise ValueError("请填写真实容器名称，宿主机检测可移除 container 字段")
        for field in ("devices", "physical_devices"):
            devs = n.get(field, [])
            if (field == "devices" and not devs and n.get("role", "worker") not in {"store", "router"}) or len(set(devs)) != len(devs) or any(type(x) is not int or not 0 <= x < 64 for x in devs):
                raise ValueError(field + " 必须为非重复的显式卡列表")
        if n.get("data_ip", "auto") != "auto" and ipaddress.ip_address(n["data_ip"]).version != 4:
            raise ValueError("当前工具仅实现 IPv4；IPv6 未验证")
    if cfg.get("fabric_cidr") and ipaddress.ip_network(cfg["fabric_cidr"]).version != 4:
        raise ValueError("当前工具仅实现 IPv4")
    ports = cfg.get("tcp_ports", [])
    if not ports or len(ports) > 64 or len(set(ports)) != len(ports):
        raise ValueError("tcp_ports 需为 1~64 个唯一端口")
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports + [cfg.get("master_port")]):
        raise ValueError("端口应为 1024~65535")
    if cfg["master_port"] in ports:
        raise ValueError("master_port 不能复用探针监听端口")
    if not 5 <= cfg.get("timeout_s", 90) <= 600:
        raise ValueError("timeout_s 应为 5~600")
    if not set(cfg.get("stages", [])) <= STAGES:
        raise ValueError("未知 collective 阶段")
    groups = cfg.get("groups", [])
    if not groups or len({g["name"] for g in groups}) != len(groups):
        raise ValueError("groups 必须明确且名称唯一")
    for g in groups:
        if len(g["nodes"]) != len(set(g["nodes"])) or not set(g["nodes"]) <= set(names) or not g["nodes"]:
            raise ValueError("通信域节点错误")
        if any(not n.get("devices") for n in nodes if n["name"] in g["nodes"]):
            raise ValueError("CPU-only store/router 不应加入 NPU 模型通信域")
    for key in cfg.get("environment", {}):
        if key not in ENV_KEYS:
            raise ValueError("未支持的环境变量：" + key)
    if any(k in cfg.get("environment", {}) for k in ("HCCL_IF_IP", "HCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME")):
        raise ValueError("网卡/IP 由探测结果逐节点设置，不允许全局硬编码")
    return cfg


def remote_argv(node, payload, env):
    cmd = [node.get("python", "python3"), "-u", "-c", BOOT, "--agent", encode(payload)]
    shell = " && ".join(["source " + shlex.quote(x) for x in node.get("env_scripts", [])] +
                         ["exec env " + " ".join(shlex.quote(k + "=" + str(v)) for k, v in env.items()) + " " + shlex.join(cmd)])
    if node["ssh"] == "local":
        return ["bash", "-c", shell]
    runtime = ["bash", "-c", shell]
    if node.get("container"):
        runtime = ["docker", "exec", "-i", node["container"]] + runtime
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "--", node["ssh"], shlex.join(runtime)]


class Remote:
    def __init__(self, node, payload, env=None):
        self.events, self.raw = [], []
        self.q = queue.Queue()
        self.p = subprocess.Popen(remote_argv(node, payload, env or {}), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace", start_new_session=True)

        def reader():
            for line in self.p.stdout:
                if line.startswith(PREFIX):
                    try:
                        event = json.loads(line[len(PREFIX):])
                        self.events.append(event)
                        self.q.put(event)
                    except ValueError:
                        pass
                else:
                    self.raw.append(line.rstrip())
                    self.raw[:] = self.raw[-60:]
            self.q.put({"event": "eof"})
            self.p.stdout.close()

        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()
        try:
            self.p.stdin.write(SOURCE)
            self.p.stdin.close()
        except (OSError, BrokenPipeError):
            self.p.stdin.close()

    def ready(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                e = self.q.get(timeout=max(.01, end-time.monotonic()))
            except queue.Empty:
                break
            if e["event"] == "ready":
                return e
            if e["event"] in ("error", "eof"):
                break
        raise RuntimeError("监听器未就绪，不会向该端口发送探针：" + str(self.raw[-3:]) + str(self.events[-2:]))

    def finish(self, timeout):
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_owned(self.p)
        self.thread.join(timeout=3)
        ok = self.p.returncode == 0 and any(e.get("event") == "complete" for e in self.events)
        return {"status": "PASS" if ok else "FAIL", "rc": self.p.returncode,
                "events": self.events, "diagnostics": self.raw[-30:]}


def call_remote(node, payload, env=None):
    return Remote(node, payload, env).finish(payload["timeout_s"] + 20)


def get_result(remote):
    if remote["status"] != "PASS":
        raise RuntimeError(json.dumps(remote, ensure_ascii=False))
    return [e["result"] for e in remote["events"] if e["event"] == "result"][-1]


def tcp_matrix(nodes, selected, cfg):
    servers = []
    sizes = [8, 4096, 65536]
    try:
        targets = [{"name": n["name"], "ip": selected[n["name"]]["ip"], "ports": cfg["tcp_ports"], "token": secrets.token_hex(32)} for n in nodes]
        for node, target in zip(nodes, targets):
            p = Remote(node, dict(action="serve", ip=target["ip"], ports=target["ports"], token=target["token"],
                                  sizes=sizes, expected=len(nodes)-1, timeout_s=cfg["timeout_s"]))
            servers.append(p)
        for server in servers:
            server.ready(min(20, cfg["timeout_s"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            futures = [ex.submit(call_remote, n, dict(action="dial", ip=t["ip"], sizes=sizes,
                       targets=[x for x in targets if x["name"] != n["name"]], timeout_s=cfg["timeout_s"])) for n, t in zip(nodes, targets)]
            clients = [f.result() for f in futures]
        listeners = [s.finish(cfg["timeout_s"]+10) for s in servers]
        edges = [r for c in clients for r in get_result(c)]
        ok = all(r["status"] == "PASS" for r in edges) and all(s["status"] == "PASS" for s in listeners)
        return {"status": "PASS" if ok else "FAIL", "edges": edges, "listeners": listeners}
    finally:
        for s in servers:
            stop_owned(s.p)


def run_group(nodes, selected, cfg, stage):
    total = sum(len(n["devices"]) for n in nodes)
    rank = 0
    jobs = []
    master = selected[nodes[0]["name"]]["ip"]
    for n in nodes:
        workers = []
        for dev in n["devices"]:
            workers.append(dict(action="collective", stage=stage, rank=rank, world=total, device=dev,
                                master=master, port=cfg["master_port"], timeout_s=cfg["timeout_s"],
                                use_libuv=cfg.get("use_libuv", True)))
            rank += 1
        ip = selected[n["name"]]
        env = dict(cfg.get("environment", {}), HCCL_IF_IP=ip["ip"], HCCL_SOCKET_IFNAME="=" + ip["nic"],
                   GLOO_SOCKET_IFNAME=ip["nic"])
        jobs.append((n, dict(workers=workers, timeout_s=cfg["timeout_s"]), env))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
        outcomes = list(ex.map(lambda j: call_remote(*j), jobs))
    return {"status": "PASS" if all(x["status"] == "PASS" for x in outcomes) else "FAIL",
            "world_size": total, "nodes": {n["name"]: r for n, r in zip(nodes, outcomes)}}


def gate(report, scope, max_age=3600):
    checks = report.get("checks", {})
    required = report.get("required", {}).get(scope, [])
    fresh = 0 <= time.time() - report.get("time", 0) <= max_age
    return bool(required) and fresh and all(checks.get(k, {}).get("status") == "PASS" for k in required)


def run(config, command):
    cfg = validate_config(config)
    nodes = cfg["nodes"]
    report = {"schema_version": 1, "time": time.time(), "command": command,
              "mode": cfg["mode"], "config_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest(),
              "checks": {}, "inventory": {}, "selected": {}, "required": {"primitives": ["inventory", "runtime_alignment", "dns", "tcp"]},
              "boundary": "仅覆盖本次环境、节点/卡组、端口和负载；不能保证模型服务必然启动。"}
    checks = report["checks"]
    for g in cfg["groups"]:
        for stage in sorted(STAGES):
            key = g["name"] + "/" + stage
            report["required"]["primitives"].append(key)
            checks[key] = {"status": "UNVERIFIED"}
    service_required = list(report["required"]["primitives"])
    extra = ["model_e2e"]
    if cfg["mode"] == "disaggregated" or cfg.get("require_kv_transfer"):
        extra.append("kv_transfer")
    if cfg["mode"] == "pooled":
        extra.append("kv_pool")
    if cfg.get("require_mc2"):
        extra.append("mc2")
    for key in extra:
        checks[key] = {"status": "UNVERIFIED", "reason": "需真实版本适配器与验收证据"}
    report["required"]["service"] = service_required + extra
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            vals = list(ex.map(lambda n: call_remote(n, dict(action="inventory", physical_devices=n.get("physical_devices", []), timeout_s=cfg["timeout_s"]), cfg.get("environment", {})), nodes))
        for n, val in zip(nodes, vals):
            report["inventory"][n["name"]] = get_result(val)
        selected = select_addresses(nodes, report["inventory"], cfg.get("fabric_cidr"))
        report["selected"] = selected
        report["topology"] = {k: topology(v) for k, v in report["inventory"].items()}
        report["warnings"] = []
        alignment_errors = []
        report["tested_environment"] = {}
        for k, inv in report["inventory"].items():
            chosen = selected[k]
            report["tested_environment"][k] = dict(cfg.get("environment", {}),
                HCCL_IF_IP=chosen["ip"], HCCL_SOCKET_IFNAME="=" + chosen["nic"], GLOO_SOCKET_IFNAME=chosen["nic"])
            old_ip = inv.get("env", {}).get("HCCL_IF_IP")
            if old_ip and old_ip != chosen["ip"]:
                alignment_errors.append(k + ": 实际环境 HCCL_IF_IP=" + old_ip + " 与选中 IP=" + chosen["ip"] + " 不一致")
            if inv["proxy_present"]:
                report["warnings"].append(k + ": 存在代理变量，HTTP 适配器需验证内部流量未被代理")
            if inv["mounts"]:
                report["warnings"].append(k + ": 存在拓扑文件挂载，检查版本依赖、来源及是否陈旧")
        if len({x["mtu"] for x in selected.values()}) > 1:
            report["warnings"].append("业务网卡 MTU 不一致；小包成功不代表大包/设备传输通过")
        checks["inventory"] = {"status": "PASS"}
        checks["runtime_alignment"] = {"status": "FAIL" if alignment_errors else "PASS", "errors": alignment_errors,
                                       "scope": "HCCL_IF_IP 一致性；测试会显式设置网卡变量，服务必须使用同一 tested_environment"}
        if command == "inspect":
            return report
        if alignment_errors:
            return report
        ips = [x["ip"] for x in selected.values()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            dns = list(ex.map(lambda n: call_remote(n, dict(action="dns", peers=ips, timeout_s=cfg["timeout_s"])), nodes))
        dns_rows = {n["name"]: get_result(r) for n, r in zip(nodes, dns)}
        checks["dns"] = {"status": "PASS" if all(x["rc"] == 0 and x.get("consistent") for xs in dns_rows.values() for x in xs) else "FAIL", "nodes": dns_rows}
        checks["tcp"] = tcp_matrix(nodes, selected, cfg)
        if checks["tcp"]["status"] != "PASS" or checks["dns"]["status"] != "PASS":
            return report
        if command == "pairs":
            if len(nodes) != 2 or any(not n.get("devices") for n in nodes):
                raise ValueError("pairs 需要且仅支持两个节点")
            report["pairs"] = []
            for a in nodes[0]["devices"]:
                for b in nodes[1]["devices"]:
                    pairnodes = [dict(nodes[0], devices=[a]), dict(nodes[1], devices=[b])]
                    r = run_group(pairnodes, selected, cfg, "hccl")
                    report["pairs"].append(dict(source_device=a, target_device=b, **r))
            checks["card_pairs"] = {"status": "PASS" if all(x["status"] == "PASS" for x in report["pairs"]) else "FAIL"}
            report["required"]["pairs"] = ["inventory", "runtime_alignment", "dns", "tcp", "card_pairs"]
            return report
        for g in cfg["groups"]:
            groupnodes = [next(n for n in nodes if n["name"] == name) for name in g["nodes"]]
            for stage in ("tcpstore", "gloo", "hccl"):
                if stage in cfg["stages"]:
                    checks[g["name"] + "/" + stage] = run_group(groupnodes, selected, cfg, stage)
                    if checks[g["name"] + "/" + stage]["status"] == "FAIL":
                        return report
        contract = {"mc2": ["fused_operator", "numerical_correctness", "cross_node"],
                    "kv_transfer": ["metadata", "remote_kv", "checksum", "release"],
                    "kv_pool": ["register", "put", "remote_get", "checksum", "cache_hit", "cleanup"],
                    "model_e2e": ["request", "nonempty_output", "correct_route"]}
        for item in cfg.get("adapters", []):
            if item["stage"] not in extra:
                raise ValueError("适配器 stage 不在当前模式验收范围")
            n = next(n for n in nodes if n["name"] == item["node"])
            r = call_remote(n, dict(action="adapter", argv=item["argv"], required_checks=contract[item["stage"]], timeout_s=cfg["timeout_s"]))
            checks[item["stage"]] = r
    except Exception as e:
        checks["orchestrator"] = {"status": "FAIL", "error": type(e).__name__ + ": " + str(e)}
        for required in report["required"].values():
            required.append("orchestrator")
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2 and sys.argv[1] == "--agent":
        try:
            agent(json.loads(base64.b64decode(sys.argv[2])))
        except Exception as e:
            emit("error", error=type(e).__name__ + ": " + str(e), traceback=traceback.format_exc()[-8000:])
            return 1
        return 0
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("inspect", "check", "pairs"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
        s.add_argument("--out", required=True)
    s = sub.add_parser("gate")
    s.add_argument("--report", required=True)
    s.add_argument("--scope", choices=["primitives", "service", "pairs"], required=True)
    s.add_argument("--max-age-s", type=int, default=3600)
    args = p.parse_args()
    if args.command == "gate":
        r = json.loads(Path(args.report).read_text(encoding="utf-8"))
        ok = gate(r, args.scope, args.max_age_s)
        print("PASS" if ok else "FAIL/UNVERIFIED：所选范围未完整通过或报告已过期")
        return 0 if ok else 2
    report = run(json.loads(Path(args.config).read_text(encoding="utf-8")), args.command)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v["status"] for k, v in report["checks"].items()}, ensure_ascii=False, indent=2))
    print("报告：" + str(path.resolve()))
    if any(x["status"] == "FAIL" for x in report["checks"].values()):
        return 1
    return 0 if gate(report, "pairs" if args.command == "pairs" else "service") else 2


if __name__ == "__main__":
    sys.exit(main())

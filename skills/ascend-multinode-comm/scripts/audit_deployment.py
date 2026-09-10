#!/usr/bin/env python3
"""只读部署审计：静态 shell 子集，绝不执行、source、eval 或导入上传代码。"""
import argparse
from collections import defaultdict
import html
import ipaddress
import json
from pathlib import Path
import re
import sys

ENV = {"HCCL_IF_IP", "HCCL_SOCKET_IFNAME", "HCCL_ALGO", "HCCL_IF_BASE_PORT",
       "HCCL_HOST_SOCKET_PORT_RANGE", "GLOO_SOCKET_IFNAME", "MASTER_ADDR", "MASTER_PORT",
       "WORLD_SIZE", "RANK", "LOCAL_RANK", "ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES",
       "VLLM_HOST_IP", "PYTHONHASHSEED", "NO_PROXY", "no_proxy"}
PROXIES = {"http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
FLAGS = {"--data-parallel-size": "dp", "--data-parallel-size-local": "dp_local",
         "--data-parallel-start-rank": "dp_start", "--data-parallel-rank": "dp_rank",
         "--tensor-parallel-size": "tp", "--pipeline-parallel-size": "pp",
         "--data-parallel-address": "dp_address", "--data-parallel-rpc-port": "dp_port",
         "--port": "api_port", "--host": "bind_ip", "--kv-transfer-config": "kv",
         "--distributed-executor-backend": "executor",
         "--master-addr": "master_address", "--master-port": "master_port",
         "--nnodes": "nnodes", "--node-rank": "node_rank", "--nproc-per-node": "nproc",
         "--dp-size": "dp", "--dp-size-local": "dp_local", "--dp-rank-start": "dp_start",
         "--tp-size": "tp", "--pp-size": "pp", "--dp-address": "dp_address",
         "--dp-rpc-port": "dp_port", "--vllm-start-port": "api_port"}
NUMBERS = {"dp", "dp_local", "dp_start", "dp_rank", "tp", "pp", "api_port", "dp_port",
           "master_port", "node_rank", "nnodes", "nproc", "MASTER_PORT", "WORLD_SIZE",
           "RANK", "LOCAL_RANK", "HCCL_IF_BASE_PORT"}
UNKNOWN = "<unresolved>"
SUPPORTED = {".sh", ".bash", ".env"}


def resolved(value):
    return isinstance(value, str) and UNKNOWN not in value and "$" not in value


def lex(text, env, first_line):
    """保留物理行号、单双引号语义；变量的值不再作为 shell 语法解释。"""
    tokens, buf, quote, active = [], [], None, False
    line = start = first_line
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\n":
            line += 1
        if not quote and c.isspace():
            if active:
                tokens.append(("".join(buf), start))
                buf, active = [], False
            i += 1
            continue
        if not quote and c == "#" and not active:
            j = text.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if not active:
            start, active = line, True
        if c in "'\"" and (quote is None or quote == c):
            quote = c if quote is None else None
            i += 1
            continue
        if c == "\\" and quote != "'":
            if i+1 >= len(text):
                raise ValueError("末尾反斜杠")
            nxt = text[i+1]
            if nxt == "\n":
                line += 1
            elif quote == '"' and nxt not in ("$", chr(96), '"', "\\"):
                buf.extend(["\\", nxt])
            else:
                buf.append(nxt)
            i += 2
            continue
        if c == chr(96) and quote != "'":
            raise ValueError("命令替换")
        if c == "$" and quote != "'":
            if text[i:i+2] == "$(":
                raise ValueError("命令/算术替换")
            m = re.match(r"\$(?:\{([A-Za-z_]\w*)(?::-([^}]*))?\}|([A-Za-z_]\w*)|([0-9@*?#]))", text[i:])
            if m:
                value = env.get(m[1] or m[3] or m[4], {}).get("value")
                if not value and m[2] is not None:
                    value = m[2]
                value = UNKNOWN if value is None else value
                if quote is None and re.search(r"\s|[*?]", value):
                    value = UNKNOWN  # 未加引号的分词/通配符不强行推断。
                buf.append(value)
                i += len(m[0])
                continue
        if quote is None and c in ";|&<>":
            raise ValueError("复合命令、管道或重定向")
        buf.append(c)
        i += 1
    if quote:
        raise ValueError("未闭合引号")
    if active:
        tokens.append(("".join(buf), start))
    return tokens


def logical_lines(text):
    buf, start, quote = [], 1, None
    for n, raw in enumerate(text.splitlines(keepends=True), 1):
        if not buf:
            start = n
        buf.append(raw)
        i = 0
        while i < len(raw):
            c = raw[i]
            if c == "\\" and quote != "'":
                i += 2
                continue
            if c == "#" and quote is None and (i == 0 or raw[i-1].isspace()):
                break
            if c in "'\"" and (quote is None or quote == c):
                quote = c if quote is None else None
            i += 1
        if quote is None and not raw.rstrip("\r\n").endswith("\\"):
            yield start, "".join(buf)
            buf = []
    if buf:
        yield start, "".join(buf)


def number(facts, key):
    raw = facts.get(key, {}).get("value")
    return int(raw) if isinstance(raw, str) and re.fullmatch(r"-?\d+", raw) else None


def loc(file, line, field):
    return {"file": file, "line": line, "field": field}


class Auditor:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.findings, self.units, self.files = [], [], set()

    def inside(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("引用越出上传目录，未读取")
        return path

    def issue(self, code, severity, phase, message, evidence, fix, conditional=False):
        self.findings.append({"code": code, "severity": "RISK" if conditional and severity == "ERROR" else severity,
                              "phase": phase, "message": message, "evidence": evidence,
                              "suggestion": fix, "confidence": "conditional" if conditional else "static"})

    def parse(self, path, ctx, env=None, stack=(), tainted=False):
        path = self.inside(path)
        rel = path.relative_to(self.root).as_posix()
        env = {} if env is None else env
        if path in stack:
            self.issue("SOURCE_CYCLE", "NEEDS_REVIEW", "S0", "source 引用成环", [loc(rel, 1, "source")],
                       "检查重复 source 链及运行时 guard。")
            return True
        if len(stack) >= 16 or path.stat().st_size > 1024*1024:
            raise ValueError("单文件或 source 深度超出审计上限")
        self.files.add(rel)
        text = path.read_text(encoding="utf-8-sig")
        if "<<" in text:
            self.issue("HEREDOC_REVIEW", "NEEDS_REVIEW", "S0", "含 heredoc，未将嵌入文本当部署语句",
                       [loc(rel, 1, "heredoc")], "人工追踪生成与执行两阶段的远端/容器脚本。")
            return True
        complex_flow = bool(re.search(r"(?m)^\s*(?:if|for|while|until|case|function|elif|else)\b|^\s*\w+\s*\(\s*\)", text))
        uncertain = tainted or complex_flow
        if complex_flow:
            self.issue("CONTROL_FLOW", "NEEDS_REVIEW", "S0", "分支/循环/函数中的静态值仅为候选",
                       [loc(rel, 1, "control_flow")], "逐节点展开实际参数、分支和函数调用，不运行脚本。")
        for line, stmt in logical_lines(text):
            if not stmt.strip() or stmt.lstrip().startswith("#"):
                continue
            try:
                tokens = lex(stmt, env, line)
            except ValueError:
                self.issue("DYNAMIC_SHELL", "NEEDS_REVIEW", "S0", "语句超出安全静态解析子集，未执行",
                           [loc(rel, line, "shell")], "人工分析替换、重定向、复合语句的结果与作用域。")
                uncertain = True
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)=", stmt)
                if m:
                    env[m[1]] = {"value": UNKNOWN, "line": line, "file": rel, "exported": False}
                continue
            if not tokens:
                continue
            command = tokens[0][0]
            if command in {"source", "."}:
                try:
                    if len(tokens) != 2 or not resolved(tokens[1][0]):
                        raise ValueError()
                    target = self.inside(Path(ctx["cwd"]) / tokens[1][0])
                    if not target.is_file():
                        raise ValueError()
                    uncertain = self.parse(target, ctx, env, (*stack, path), uncertain) or uncertain
                except (ValueError, OSError):
                    self.issue("SOURCE_EXTERNAL", "NEEDS_REVIEW", "S0", "source 目标/传参不在已解析上传范围",
                               [loc(rel, line, "source")], "提供本地依赖、实际 cwd；外部 CANN 脚本未上传不代表服务器缺失。")
                    uncertain = True
                continue
            if command == "cd":
                self.issue("CWD_CHANGE", "NEEDS_REVIEW", "S0", "工作目录切换未自动模拟",
                           [loc(rel, line, "cd")], "人工核对相对路径在宿主/容器中的落点。")
                uncertain = True
                continue
            export = command == "export"
            values = list(tokens[1:] if export or command == "env" else tokens)
            assigned = {}
            while values and re.match(r"^[A-Za-z_]\w*=", values[0][0]):
                token, at = values.pop(0)
                key, value = token.split("=", 1)
                assigned[key] = {"value": value, "line": at, "file": rel,
                                 "exported": export or env.get(key, {}).get("exported", False)}
            if export and values:
                for key, at in values:
                    if re.fullmatch(r"[A-Za-z_]\w*", key):
                        assigned[key] = dict(env.get(key, {"value": UNKNOWN, "line": at, "file": rel}), exported=True)
            if not values or export:
                for key, val in assigned.items():
                    prior = [u for u in self.units if u["id"] == ctx["id"] and u["file"] == rel and u["line"] < val["line"]]
                    if key in ENV and prior:
                        self.issue("LATE_COMM_ENV", "RISK", "S0/S3", key + " 在服务启动语句之后才设置",
                                   [loc(rel, val["line"], key), loc(rel, prior[-1]["line"], "service")],
                                   "已启动进程不会继承之后的 export；检查是否应移到启动前，或仅针对后续实例。")
                env.update(assigned)
                continue
            current = {k: dict(v) for k, v in env.items()}
            current.update({k: dict(v, exported=True) for k, v in assigned.items()})
            words = [x[0] for x in values]
            if words[0] == "exit":
                if stack or uncertain:
                    self.issue("EXIT_FLOW_REVIEW", "NEEDS_REVIEW", "S0", "source/条件中的 exit 影响调用方控制流",
                               [loc(rel, line, "exit")], "人工追踪是否结束整个入口；之后的候选值不能视为确定生效。")
                    uncertain = True
                break
            if words[0] == "unset":
                for key in words[1:]:
                    env.pop(key, None)
                continue
            docker = words[0] == "docker" and any(x in words for x in ("run", "create", "exec"))
            python = bool(re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(words[0]).name))
            service = (words[0] == "vllm" and len(words) > 1 and words[1] == "serve") or (
                python and len(words) > 1 and (Path(words[1]).name == "launch_online_dp.py" or
                (len(words) > 2 and words[1] == "-m" and words[2].startswith("vllm.entrypoints."))))
            torchrun = Path(words[0]).name == "torchrun" or (python and words[1:3] == ["-m", "torch.distributed.run"])
            if docker:
                self.docker(values, rel)
            elif service or torchrun:
                self.service(values, current, rel, ctx, uncertain, "torchrun" if torchrun else "vllm")
            elif words[0] not in {"set", "echo", "printf", "true", "false", "exit"}:
                self.issue("COMMAND_REVIEW", "NEEDS_REVIEW", "S0", "包装器/外部命令未自动展开",
                           [loc(rel, line, "command")], "人工追踪 bash/python/ssh 包装、变量导出与命名空间。")
                uncertain = True
        return uncertain

    def docker(self, tokens, rel):
        words = [x[0] for x in tokens]
        for word, line in tokens:
            if "/etc/hccl_rootinfo.json" in word:
                self.issue("ROOTINFO_MOUNT", "RISK", "S0/S3/S5", "容器命令涉及 rootinfo，可能引入陈旧拓扑",
                           [loc(rel, line, "hccl_rootinfo.json")], "默认不额外注入；核对版本消费方，无依赖才备份并移除挂载。")
            if "/etc/hixlep.json" in word:
                self.issue("HIXLEP_PATH", "RISK", "S5", "出现 hixlep.json 单文件，须与目录入口区分",
                           [loc(rel, line, "hixlep")], "核对 connector 的路径、文件/目录类型及各机拓扑。")
        hostnet = any(w in {"--net=host", "--network=host"} for w in words) or any(
            words[i] in {"--net", "--network"} and words[i+1] == "host" for i in range(len(words)-1))
        if ("run" in words or "create" in words) and not hostnet:
            self.issue("CONTAINER_NETWORK", "RISK", "S1/S2/S3/S5", "未显式使用 host 网络，需验证跨机入站/动态回连",
                       [loc(rel, tokens[0][1], "docker network")], "检查实际 network 和端口映射；bridge 不必然错误。")
        self.issue("CONTAINER_ENV_BOUNDARY", "NEEDS_REVIEW", "S0", "宿主 export 不代表容器服务已继承",
                   [loc(rel, tokens[0][1], "docker environment")], "检查 -e/--env-file、容器入口和 source；不跨 namespace 强行传播。")

    def evidence(self, unit, *keys):
        return [dict(loc(unit["facts"][k]["file"], unit["facts"][k]["line"], k),
                     deployment=unit["id"], node=unit.get("node"))
                for k in keys if k in unit["facts"]]

    def service(self, tokens, env, rel, ctx, uncertain, kind):
        facts = {}
        for key, val in env.items():
            if key not in ENV | PROXIES:
                continue
            if not val["exported"]:
                self.issue("ENV_NOT_EXPORTED", "RISK", "S1/S2/S3", key + " 未显式导出",
                           [loc(val["file"], val["line"], key)], "核对 export/命令前赋值/外部已导出环境；赋值不等于子进程继承。")
                continue
            facts[key] = {"value": "SET" if key in PROXIES else val["value"], "file": val["file"], "line": val["line"]}
        i = 3 if kind == "torchrun" and tokens[1:3] and [t[0] for t in tokens[1:3]] == ["-m", "torch.distributed.run"] else 1
        while i < len(tokens):
            word, at = tokens[i]
            key, sep, value = word.partition("=")
            key = key.replace("_", "-") if key.startswith("--") else key
            if kind == "torchrun":
                if key in {"--standalone", "--module", "-m", "--no-python", "--run-path"}:
                    i += 1
                    continue
                if not word.startswith("-"):
                    break  # 后续是 training_script 及其参数，不当作 launcher 参数。
                if key not in {"--master-addr", "--master-port", "--nnodes", "--node-rank", "--nproc-per-node"}:
                    self.issue("TORCHRUN_OPTION_REVIEW", "NEEDS_REVIEW", "S1", "torchrun 选项超出自动提取范围",
                               [loc(rel, at, "torchrun")], "按版本展开 rendezvous/launcher 参数，业务脚本参数不可混入。")
                    break
            if key in FLAGS:
                if not sep:
                    if i+1 == len(tokens) or tokens[i+1][0].startswith("--"):
                        self.issue("MISSING_FLAG_VALUE", "ERROR", "S0", key + " 缺少参数值",
                                   [loc(rel, at, key)], "补齐当前版本支持的参数值。", uncertain)
                        i += 1
                        continue
                    value = tokens[i+1][0]
                    i += 1
                facts[FLAGS[key]] = {"value": value, "file": rel, "line": at}
            i += 1
        unit = {k: ctx.get(k) for k in ("id", "node", "group", "role", "namespace")}
        unit.update(file=rel, line=tokens[0][1], conditional=uncertain, kind=kind, facts=facts)
        self.units.append(unit)
        self.check_unit(unit)

    def check_unit(self, u):
        f = u["facts"]
        def add(code, severity, phase, msg, keys, fix):
            self.issue(code, severity, phase, msg, self.evidence(u, *keys), fix, u["conditional"])
        for key, fact in list(f.items()):
            value = fact["value"]
            if not resolved(value):
                add("UNRESOLVED_VALUE", "NEEDS_REVIEW", "S0", key + " 的最终值未解析", [key], "提供实际参数、环境和分支。")
                continue
            if key in NUMBERS:
                if (key == "nnodes" and re.fullmatch(r"[1-9]\d*:[1-9]\d*", value)) or (key == "nproc" and value in {"auto", "cpu", "gpu"}):
                    add("ELASTIC_LAYOUT", "NEEDS_REVIEW", "S1", "torchrun 使用动态/弹性规模", [key], "根据运行时资源和实际 rendezvous 规模验收，不当作普通整数错误。")
                    continue
                n = number(f, key)
                lower = 0 if key in {"dp_start", "dp_rank", "node_rank", "RANK", "LOCAL_RANK"} else 1
                valid = n is not None and n >= lower
                if key in {"api_port", "dp_port", "master_port", "MASTER_PORT", "HCCL_IF_BASE_PORT"}:
                    valid = n is not None and 0 <= n <= 65535
                    if n == 0:
                        add("DYNAMIC_PORT", "RISK", "S1", key + " 为 0：需核对动态端口如何公布", [key], "确认该版本是否支持自动端口，以及其他节点如何获取实际监听端口。")
                if not valid:
                    add("INVALID_NUMBER", "ERROR", "S0/S1", key + " 的整数范围无效", [key], "端口 1~65535，规模为正整数，rank 从 0 开始。")
            if key in {"MASTER_ADDR", "HCCL_IF_IP", "VLLM_HOST_IP", "master_address", "dp_address"}:
                try:
                    ip = ipaddress.ip_address(value)
                    if ip.is_unspecified or ip.is_loopback:
                        add("NONROUTABLE_ENDPOINT", "RISK", "S1/S3", key + " 使用 wildcard/loopback", [key],
                            "监听可用 0.0.0.0；公布给其他节点的 endpoint 应为实际业务地址。")
                except ValueError:
                    if key == "HCCL_IF_IP":
                        add("INVALID_HCCL_IP", "ERROR", "S3", "HCCL_IF_IP 不是 IP 字面量", [key], "使用本节点实际 Host IP，而非网卡名。")
            if key in {"ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES"} and re.fullmatch(r"\d+(?:,\d+)*", value):
                if len(value.split(",")) != len(set(value.split(","))):
                    add("DUPLICATE_DEVICE", "ERROR", "S0/S3", key + " 卡号重复", [key], "确认可见卡映射后去重。")
        dp, local, start = (number(f, k) for k in ("dp", "dp_local", "dp_start"))
        if None not in (dp, local, start) and start+local > dp:
            add("DP_RANGE", "RISK", "S1/S2", "DP 起始 rank + 本地规模超过全局规模", ["dp", "dp_local", "dp_start"],
                "核对版本、DP 模式及包装器语义；mp 显式切分应落在全局范围。")
        if "HCCL_IF_IP" in f and "HCCL_SOCKET_IFNAME" in f:
            add("HCCL_IP_PRECEDENCE", "RISK", "S3", "HCCL_IF_IP 优先于 IFNAME，仅改网卡变量可能无效",
                ["HCCL_IF_IP", "HCCL_SOCKET_IFNAME"], "核对 IP 所属 UP 网卡，再做源绑定/真实 HCCL 检测。")
        if "HCCL_HOST_SOCKET_PORT_RANGE" in f and "HCCL_IF_BASE_PORT" in f:
            add("HCCL_PORT_PRECEDENCE", "RISK", "S3", "同时声明 HCCL 范围与基准端口",
                ["HCCL_HOST_SOCKET_PORT_RANGE", "HCCL_IF_BASE_PORT"], "按版本确认优先级与 MC2 约束，不套固定端口数。")
        if any(k in f for k in PROXIES):
            add("PROXY_ROUTE", "RISK", "S5/S6", "继承代理变量，内部流量可能被送往代理",
                [k for k in f if k in PROXIES], "检查 no_proxy、业务 IP/域名及真实内部 HTTP 请求。")
        if "kv" in f and resolved(f["kv"]["value"]):
            try:
                obj = json.loads(f["kv"]["value"])
                if not isinstance(obj, dict):
                    raise ValueError()
                extra = obj.get("kv_connector_extra_config", {})
                u["kv"] = {k: obj[k] for k in ("kv_connector", "kv_role", "engine_id", "kv_port") if k in obj}
                if isinstance(extra, dict):
                    u["kv"]["layout"] = {side: {k: extra[side][k] for k in ("dp_size", "tp_size", "pp_size") if k in extra[side]}
                                         for side in ("prefill", "decode") if isinstance(extra.get(side), dict)}
                    if "ascend_local_comm_res_path" in extra:
                        u["kv"]["local_comm_res_path"] = extra["ascend_local_comm_res_path"]
            except (ValueError, TypeError):
                add("INVALID_KV_JSON", "ERROR", "S0/S5", "kv-transfer-config 不是有效 JSON 对象", ["kv"], "修正 JSON 引号、转义和展开规则。")
        if "kv" in f:
            f["kv"]["value"] = "[仅输出必要通信字段]"  # 不输出原始 connector 密钥/认证参数。

    def compare(self, pd_links=()):
        groups = defaultdict(list)
        for u in self.units:
            if u.get("group"):
                groups[u["group"]].append(u)
        for group, units in groups.items():
            for key in ("dp", "tp", "pp", "dp_port", "dp_address", "MASTER_ADDR", "MASTER_PORT", "WORLD_SIZE", "nnodes"):
                known = [u for u in units if key in u["facts"] and resolved(u["facts"][key]["value"])]
                if len({u["facts"][key]["value"] for u in known}) > 1:
                    self.issue("GROUP_MISMATCH", "RISK", "S1/S2/S3", group + " 内 " + key + " 不一致",
                               [e for u in known for e in self.evidence(u, key)],
                               "确认同一通信域和启动方案；不同 P/D 域不要强行统一。", any(u["conditional"] for u in known))
            for index, a in enumerate(units):
                for b in units[index+1:]:
                    af, bf = a["facts"], b["facts"]
                    if a.get("node") and b.get("node") and a["node"] != b["node"]:
                        if "HCCL_IF_IP" in af and "HCCL_IF_IP" in bf and resolved(af["HCCL_IF_IP"]["value"]) and af["HCCL_IF_IP"]["value"] == bf["HCCL_IF_IP"]["value"]:
                            self.issue("DUPLICATE_HOST_IP", "RISK", "S3", "不同节点复用了同一个 HCCL_IF_IP",
                                       self.evidence(a, "HCCL_IF_IP") + self.evidence(b, "HCCL_IF_IP"),
                                       "核对是否误拷贝主节点 IP；各节点应选择其本地业务地址。")
                    for key in ("RANK", "node_rank"):
                        if key in af and key in bf and resolved(af[key]["value"]) and af[key]["value"] == bf[key]["value"]:
                            self.issue("DUPLICATE_RANK", "RISK", "S1/S3", group + " 中 " + key + " 重复",
                                       self.evidence(a, key) + self.evidence(b, key), "核对 rank 唯一性及覆盖，不混比不同 torchrun 会话。")
                    ar = [number(af, k) for k in ("dp_start", "dp_local")]
                    br = [number(bf, k) for k in ("dp_start", "dp_local")]
                    if None not in ar+br and max(ar[0], br[0]) < min(sum(ar), sum(br)):
                        self.issue("DP_OVERLAP", "RISK", "S1/S2", group + " 的 DP rank 区间重叠",
                                   self.evidence(a, "dp_start", "dp_local") + self.evidence(b, "dp_start", "dp_local"),
                                   "按实际 mp DP 模式重分区间；Ray/外部 DP 需先核对参数语义。")
        for index, a in enumerate(self.units):
            for b in self.units[index+1:]:
                if not a.get("node") or not a.get("namespace") or (a["node"], a["namespace"]) != (b.get("node"), b.get("namespace")):
                    continue
                x, y = number(a["facts"], "api_port"), number(b["facts"], "api_port")
                if x is not None and x == y:
                    self.issue("POSSIBLE_PORT_COLLISION", "RISK", "S0/S6", "同节点/namespace 的两个入口声明相同 API 端口",
                               self.evidence(a, "api_port") + self.evidence(b, "api_port"),
                               "确认是否同时运行、bind IP 是否冲突；不把 DP 共享 rendezvous 端口判冲突。")
        for link in pd_links:
            linked = {side: groups.get(link.get(side + "_group"), []) for side in ("prefill", "decode")}
            for units in linked.values():
                for u in units:
                    for side, peers in linked.items():
                        layout = u.get("kv", {}).get("layout", {}).get(side, {})
                        for cli, meta in (("dp", "dp_size"), ("tp", "tp_size"), ("pp", "pp_size")):
                            actual = {number(p["facts"], cli) for p in peers}
                            if len(actual) == 1 and None not in actual and meta in layout and not any(layout[meta] == n for n in actual):
                                self.issue("KV_LAYOUT_MISMATCH", "RISK", "S5/S6", side + "." + meta + " 与关联组 CLI 声明不符",
                                           self.evidence(u, "kv") + [e for peer in peers for e in self.evidence(peer, cli)],
                                           "按真实 P/D 全局布局修正元数据；核对 connector 与包装器版本。")
            if any(not units for units in linked.values()):
                self.issue("PD_GROUP_MISSING", "NEEDS_REVIEW", "S5", "pd_links 关联组未提取到入口", [],
                           "补齐 P/D 两侧清单及可解析入口；不跨不相关实例比较。")
        if not groups:
            self.issue("GROUP_NOT_DECLARED", "NEEDS_REVIEW", "S1/S3", "未声明通信域，未跨文件断言 rank/端口冲突",
                       [], "提供实际同时运行的 node/group/namespace，P 与 D 分别分组。")

    def report(self, pd_links=()):
        self.compare(pd_links)
        counts = {k: sum(f["severity"] == k for f in self.findings) for k in ("ERROR", "RISK", "NEEDS_REVIEW")}
        return {"schema_version": 1, "mode": "static-only",
                "status": "ERRORS_FOUND" if counts["ERROR"] else "UNVERIFIED", "counts": counts,
                "files_read": sorted(self.files), "units": self.units, "findings": self.findings,
                "runtime_required": ["容器内正反 DNS/TCPStore", "实际网卡/IP/路由与端口占用",
                                     "Gloo peer 互联", "HCCL/逐卡通信", "版本对应 KV/MC2 与真实请求"],
                "boundary": "不执行上传内容；未命中规则不是 PASS。复杂脚本需技能人工分析，静态结果不能保证建链。"}


def audit(root, manifest=None):
    a = Auditor(root)
    if not a.root.is_dir():
        raise ValueError("root 不是目录")
    if manifest:
        if manifest.get("schema_version") != 1:
            raise ValueError("清单 schema_version 应为 1")
        contexts = manifest.get("deployments", [])
        if not contexts or len({c["id"] for c in contexts}) != len(contexts):
            raise ValueError("deployments 不能为空且 id 唯一")
    else:
        contexts = []
        for p in sorted(a.root.rglob("*")):
            if not p.is_file() or p.is_symlink() or set(p.relative_to(a.root).parts) & {".git", "reports", "__pycache__"}:
                continue
            rel = p.relative_to(a.root).as_posix()
            if p.suffix in SUPPORTED or p.name == ".env":
                contexts.append({"id": rel, "entry": rel})
            elif p.suffix in {".py", ".yaml", ".yml", ".json"}:
                a.issue("NON_SHELL_REVIEW", "NEEDS_REVIEW", "S0", "此文件需要技能人工读取，非 shell 解析覆盖",
                        [loc(rel, 1, "file")], "追踪 Python/Compose/K8s/MPI 的入口与参数，不导入/执行。")
    if len(contexts) > 128:
        raise ValueError("入口过多，请用清单限制至 128 个")
    for original in contexts:
        c = dict(original)
        c["cwd"] = str(a.inside(a.root / c.get("cwd", ".")))
        entry = a.inside(a.root / c["entry"])
        if entry.suffix not in SUPPORTED and entry.name != ".env":
            a.issue("NON_SHELL_REVIEW", "NEEDS_REVIEW", "S0", "此入口不是受支持的静态 shell",
                    [loc(c["entry"], 1, "entry")], "使用技能读取对应脚本，不运行/导入。")
            continue
        env = {k: {"value": str(v), "exported": True, "file": "<manifest:" + c["id"] + ">", "line": 1}
               for k, v in c.get("env", {}).items()}
        a.parse(entry, c, env)
    if not a.units:
        a.issue("NO_SERVICE_EXTRACTED", "NEEDS_REVIEW", "S0", "未提取到可判定的服务入口", [],
                "检查包装器、Python、YAML 或动态 shell，由技能人工展开。")
    return a.report((manifest or {}).get("pd_links", []))


def markdown(report):
    def safe(value):
        return html.escape(str(value)).replace("|", r"\|").replace("\n", " ").replace(chr(96), "&#96;")

    out = ["# 多节点部署脚本建链审计", "", "结论：" + report["status"], "", report["boundary"], "",
           "统计：" + json.dumps(report["counts"], ensure_ascii=False), ""]
    if report["units"]:
        out += ["## 提取的部署入口（不是现场确认值）", "",
                "| 入口 ID / 节点 | 通信域 / namespace | 服务来源 | 已提取通信配置 |",
                "|---|---|---|---|"]
        for u in report["units"]:
            facts = "; ".join(k + "=" + str(v["value"]) for k, v in sorted(u["facts"].items()))
            cells = [str(u["id"]) + " / " + str(u.get("node") or "未指定"),
                     str(u.get("group") or "未指定") + " / " + str(u.get("namespace") or "未指定"),
                     u["file"] + ":" + str(u["line"]) + ("（条件候选）" if u["conditional"] else ""),
                     facts or "未提取"]
            out.append("| " + " | ".join(safe(c) for c in cells) + " |")
        out.append("")
    for f in report["findings"]:
        evidence = []
        for e in f["evidence"]:
            context = "入口 " + str(e["deployment"]) + " / 节点 " + str(e.get("node") or "未指定") + " · " if "deployment" in e else ""
            evidence.append(context + e["file"] + ":" + str(e["line"]) + " (" + e["field"] + ")")
        out += ["## " + f["severity"] + " · " + f["code"], "", safe(f["message"]), "",
                "影响阶段：" + f["phase"] + "；置信来源：" + f["confidence"], "",
                "证据：" + safe("；".join(evidence) or "部署清单/覆盖范围"), "",
                "建议：" + safe(f["suggestion"]), ""]
    out += ["## 尚需现场验证", "", *("- " + safe(item) for item in report["runtime_required"]), ""]
    return "\n".join(out)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, help="上传脚本目录，只读此范围")
    p.add_argument("--manifest", help="可选节点/通信域/入口清单")
    p.add_argument("--out", required=True, help="新 JSON 路径，同时生成同名 .md")
    args = p.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig")) if args.manifest else None
    report = audit(args.root, manifest)
    output = Path(args.out).resolve()
    if output.suffix != ".json" or output.exists() or output.with_suffix(".md").exists():
        p.error("请指定尚不存在的 .json/.md 报告路径，不覆盖源文件或旧报告")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False))
    print("报告：" + str(output))
    return 1 if report["counts"]["ERROR"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

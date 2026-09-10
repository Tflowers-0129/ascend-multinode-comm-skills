#!/usr/bin/env python3
"""生成或执行官方 hccl_test 计划；不自动安装 MPI/CANN，不修改拓扑文件。"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
import time
from preflight import stop_owned

BINARIES = {"allgather": "all_gather_test", "allreduce": "all_reduce_test",
            "all2all": "alltoall_test", "reduce-scatter": "reduce_scatter_test"}


def parse_hosts(values):
    hosts = []
    for v in values:
        match = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):([1-9][0-9]*)", v)
        if not match or int(match[2]) > 64:
            raise ValueError("host 格式应为 hostname:卡数，不支持 IPv6")
        hosts.append((match[1], int(match[2])))
    if len(hosts) < 2 or len({x[0] for x in hosts}) != len(hosts):
        raise ValueError("需要至少两个非重复节点")
    if len({x[1] for x in hosts}) != 1:
        raise ValueError("本包装器要求各节点卡数一致；异构场景需明确 MPI rank/device 映射")
    return hosts


def hostfile(hosts, mpi):
    if mpi == "mpich":
        return "".join(f"{host}:{slots}\n" for host, slots in hosts)
    return "".join(f"{host} slots={slots}\n" for host, slots in hosts)


def plan(args, file):
    hosts = parse_hosts(args.host)
    source = getattr(args, "source", None)
    inherit_env = getattr(args, "inherit_env", False)
    if bool(source) == bool(inherit_env):
        raise ValueError("请显式指定 --source 或 --inherit-env，不能隐式复用现场环境脚本")
    binary = str(PurePosixPath(args.directory) / "bin" / BINARIES[args.op])
    command = ["mpirun", "-f" if args.mpi == "mpich" else "--hostfile", str(file), "-n",
               str(sum(x[1] for x in hosts)), binary, "-b", "8", "-e",
               "1G" if args.profile in {"large-1g", "historical-1g"} else "1M", "-f", "2"]
    if args.aiv:
        command += ["-a", "aiv"]
    if args.check:
        command += ["-c", "1"]
    if args.mpi == "openmpi":
        command[1:1] = ["--map-by", f"ppr:{hosts[0][1]}:node"]
    shell = ("source " + shlex.quote(source) + " && " if source else "") + "cd -- " + shlex.quote(args.directory)
    if args.fullmesh:
        shell += " && export HCCL_ALGO=level0:fullmesh"
    shell += " && exec " + shlex.join(command)
    return {"hosts": hosts, "hostfile": hostfile(hosts, args.mpi), "argv": command,
            "shell": shell, "binary": binary, "environment_mode": "source" if source else "inherit",
            "boundary": "MPI 全体参与不等于独立卡对全覆盖；AIV 不等于 MC2；退出码零不自动证明数值通过"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", action="append", required=True, help="本次 MPI 目标 hostname:slots；每个参与节点重复一次")
    env_group = p.add_mutually_exclusive_group(required=True)
    env_group.add_argument("--source", help="本次已核实的环境初始化脚本")
    env_group.add_argument("--inherit-env", action="store_true", help="明确使用当前已准备好的环境，不 source 文件")
    p.add_argument("--directory", required=True, help="本次已核实的 hccl_test 安装目录")
    p.add_argument("--mpi", choices=["mpich", "openmpi"], required=True)
    p.add_argument("--op", choices=BINARIES, default="all2all")
    p.add_argument("--profile", choices=["smoke", "large-1g", "historical-1g"], default="smoke",
                   help="historical-1g 是 large-1g 的兼容别名，不绑定节点或 CANN 版本")
    p.add_argument("--aiv", action="store_true")
    p.add_argument("--fullmesh", action="store_true")
    p.add_argument("--check", action="store_true", help="本地版本支持 -c 1 时启用正确性校验")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--timeout-s", type=int, default=180)
    p.add_argument("--out", default="reports/hccl-bench.json")
    args = p.parse_args()
    if not 1 <= args.timeout_s <= 3600:
        p.error("timeout-s 应为 1~3600")
    info = plan(args, "hostfile.generated")
    if not args.execute:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if os.name != "posix":
        p.error("执行模式需在参与测试的 Linux 节点/容器运行；当前仅可生成计划")
    if (args.source and not Path(args.source).is_file()) or not Path(info["binary"]).is_file():
        p.error("环境脚本或测试二进制不存在")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="a5-hccl-bench-") as tmp:
        file = Path(tmp) / "hostfile"
        info = plan(args, file)
        file.write_text(info["hostfile"], encoding="utf-8")
        version_command = ("source " + shlex.quote(args.source) + " && " if args.source else "") + "mpirun --version"
        probe = subprocess.run(["bash", "-c", version_command],
                               capture_output=True, text=True, timeout=15)
        version = probe.stdout + probe.stderr
        detected = "openmpi" if "Open MPI" in version or "OpenRTE" in version else "mpich" if "HYDRA" in version.upper() or "MPICH" in version.upper() else "unknown"
        if probe.returncode != 0 or detected != args.mpi:
            p.error("MPI 实现与 --mpi 不一致或无法识别：" + version[-1000:])
        logpath = output.with_suffix(".log")
        start = time.time()
        with logpath.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(["bash", "-c", info["shell"]], stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                rc = proc.wait(timeout=args.timeout_s)
            except subprocess.TimeoutExpired:
                stop_owned(proc)
                rc = 124
            except KeyboardInterrupt:
                stop_owned(proc)
                raise
        result = {"status": "FAIL" if rc != 0 else "UNVERIFIED", "process_rc": rc,
                  "execution": "PASS" if rc == 0 else "FAIL", "correctness": "UNVERIFIED",
                  "time": start, "elapsed_s": time.time()-start, "mpi_version": version,
                  "plan": info, "log": str(logpath.resolve()),
                  "next": "核对 rank/device 映射、各尺寸校验列及带宽；MPI 中断后确认各节点无本次残留 rank"}
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if rc else 2


if __name__ == "__main__":
    raise SystemExit(main())

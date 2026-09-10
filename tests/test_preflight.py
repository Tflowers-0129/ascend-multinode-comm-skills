import json
from pathlib import Path
import queue
import shlex
import socket
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts"
sys.path.insert(0, str(SCRIPTS))
import preflight as p
import hccl_bench as b


def inv(ips, state="UP"):
    return {"ip": {"rc": 0, "text": json.dumps([
        {"ifname": "eth" + str(i), "operstate": state, "flags": ["UP"], "mtu": 1500,
         "addr_info": [{"family": "inet", "scope": "global", "local": x, "prefixlen": 24}]}
        for i, x in enumerate(ips)])}}


def cfg():
    return {"schema_version": 1, "mode": "colocated", "tcp_ports": [29671], "timeout_s": 5,
            "master_port": 29672, "stages": ["tcpstore", "gloo", "hccl"],
            "nodes": [{"name": "a", "ssh": "node-a", "devices": [0]},
                      {"name": "b", "ssh": "node-b", "devices": [0]}],
            "groups": [{"name": "model", "nodes": ["a", "b"]}]}


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        p.validate_config(cfg())

    def test_cpu_store_not_in_model_group(self):
        c = cfg()
        c["nodes"].append({"name": "store", "ssh": "store-host", "role": "store", "devices": []})
        p.validate_config(c)
        c["groups"][0]["nodes"].append("store")
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_roce_capability_not_active_transport(self):
        out = p.topology({"rdma_ports": [{"link_layer": "Ethernet", "gid_types": ["RoCE v2"]}]})
        self.assertIn("RoCE", out["capability_candidates"])
        self.assertEqual(out["transport"], "UNVERIFIED")

    def test_ssh_injection(self):
        c = cfg()
        c["nodes"][0]["ssh"] = "-oProxyCommand=bad"
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_duplicate_devices(self):
        c = cfg()
        c["nodes"][0]["devices"] = [0, 0]
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_ports(self):
        c = cfg()
        c["master_port"] = c["tcp_ports"][0]
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_ipv6_explicitly_rejected(self):
        c = cfg()
        c["nodes"][0]["data_ip"] = "::1"
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_global_ip_not_allowed(self):
        c = cfg()
        c["environment"] = {"HCCL_IF_IP": "10.0.0.1"}
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_choose_unique_not_management_suffix(self):
        rows = {"a": inv(["198.51.100.34", "203.0.113.137"]), "b": inv(["198.51.100.80", "203.0.113.52"])}
        out = p.select_addresses(cfg()["nodes"], rows, "203.0.113.0/24")
        self.assertEqual(out["a"]["ip"], "203.0.113.137")
        with self.assertRaises(ValueError):
            p.select_addresses(cfg()["nodes"], rows)

    def test_down_nic(self):
        self.assertEqual(p.candidates(inv(["10.1.1.1"], "DOWN")), [])

    def test_fullmesh_is_not_transport_proof(self):
        t = p.topology({"env": {"HCCL_ALGO": "level0:fullmesh"}, "rdma_devices": ["mlx5_0"]})
        self.assertTrue(t["fullmesh_configured"])
        self.assertEqual(t["transport"], "UNVERIFIED")
        self.assertEqual(t["physical_topology"], "UNVERIFIED")

    def test_safe_shell_quoting(self):
        cmd = p.remote_argv({"ssh": "user@node", "container": "container name", "env_scripts": ["/a b/env.sh"]},
                            {"action": "inventory"}, {"HCCL_ALGO": "level0:fullmesh"})
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("docker exec -i", cmd[-1])

    def test_remote_container_context_round_trip(self):
        node = dict(ssh="audit@node", container="worker", workdir="/srv/owner's deployment",
                    container_user="1000:1000", env_scripts=["./env.sh"])
        c = cfg()
        c["nodes"][0].update(node)
        p.validate_config(c)
        argv = p.remote_argv(node, {"action": "inventory"}, {})
        runtime = shlex.split(argv[-1])
        self.assertEqual(runtime[:8], ["docker", "exec", "-i", "--workdir", node["workdir"], "--user", "1000:1000", "worker"])
        self.assertEqual(runtime[8:10], ["bash", "-c"])
        commands = runtime[10].split(" && ")
        self.assertEqual(shlex.split(commands[0]), ["cd", "--", node["workdir"]])
        self.assertEqual(shlex.split(commands[1]), ["source", "./env.sh"])

    def test_host_context_precedes_source(self):
        argv = p.remote_argv(dict(ssh="local", workdir="/srv/run", env_scripts=["env.sh"]),
                             {"action": "inventory"}, {})
        self.assertEqual(argv[:2], ["bash", "-c"])
        self.assertTrue(argv[2].startswith("cd -- /srv/run && source env.sh && exec env "))

    def test_invalid_workdir_rejected(self):
        for value in ("relative", "", "C:\\deploy", "/srv/\nunsafe", "/srv/\x00", None):
            with self.subTest(value=value):
                c = cfg()
                c["nodes"][0]["workdir"] = value
                with self.assertRaises(ValueError):
                    p.validate_config(c)

    def test_invalid_container_user_rejected(self):
        for value in ("--privileged", "user;bad", "", None):
            with self.subTest(value=value):
                c = cfg()
                c["nodes"][0].update(container="worker", container_user=value)
                with self.assertRaises(ValueError):
                    p.validate_config(c)

    def test_container_user_requires_container(self):
        c = cfg()
        c["nodes"][0]["container_user"] = "1000"
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_local_cannot_silently_ignore_container(self):
        c = cfg()
        c["nodes"][0].update(ssh="local", container="worker")
        with self.assertRaises(ValueError):
            p.validate_config(c)

    def test_example_targets_rejected_before_connection(self):
        c = json.loads((SCRIPTS.parent / "examples/cluster.json").read_text(encoding="utf-8"))
        for node in c["nodes"]:
            node["container"] = "confirmed-container"
        with patch.object(p, "call_remote") as remote:
            with self.assertRaisesRegex(ValueError, "SSH"):
                p.run(c, "inspect")
            remote.assert_not_called()


class GateTests(unittest.TestCase):
    def test_skips_and_stale_not_pass(self):
        r = {"time": time.time(), "required": {"service": ["gloo", "kv"]},
             "checks": {"gloo": {"status": "PASS"}, "kv": {"status": "UNVERIFIED"}}}
        self.assertFalse(p.gate(r, "service"))
        r["checks"]["kv"]["status"] = "PASS"
        self.assertTrue(p.gate(r, "service"))
        r["time"] -= 7200
        self.assertFalse(p.gate(r, "service"))
        self.assertFalse(p.gate(r, "absent"))

    def test_readiness_failure_never_dials(self):
        c = cfg()
        selected = {"a": {"ip": "10.0.0.1"}, "b": {"ip": "10.0.0.2"}}
        with patch.object(p, "Remote") as remote, patch.object(p, "call_remote") as dial:
            remote.return_value.ready.side_effect = RuntimeError("bind failed")
            remote.return_value.p.poll.return_value = 1
            with self.assertRaises(RuntimeError):
                p.tcp_matrix(c["nodes"], selected, c)
            dial.assert_not_called()


class SocketTests(unittest.TestCase):
    def test_watchdog_times_out_child(self):
        payload = {"action": "adapter", "argv": [sys.executable, "-c", "import time;time.sleep(30)"],
                   "required_checks": [], "timeout_s": .2}
        result = subprocess.run([sys.executable, "-u", "-c", p.BOOT, "--agent", p.encode(payload)],
                                input=p.SOURCE, capture_output=True, text=True, encoding="utf-8", timeout=8)
        self.assertEqual(result.returncode, 1)
        self.assertIn("TimeoutError", result.stdout)
        self.assertNotIn('"event": "complete"', result.stdout)

    def test_adapter_no_evidence_rejected(self):
        with self.assertRaises(ValueError):
            p.adapter({"argv": [sys.executable, "-c", "print('OK')"], "required_checks": ["remote_get"]})

    def test_real_tcp_handshake_and_payload(self):
        ready, result = queue.Queue(), queue.Queue()
        reserve = socket.socket()
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
        reserve.close()
        payload = {"ip": "127.0.0.1", "ports": [port], "token": "a"*64,
                   "sizes": [8, 4096, 65536], "expected": 1, "timeout_s": 3}
        def server():
            try:
                result.put(p.serve(payload))
            except Exception as e:
                result.put(e)
        with patch.object(p, "emit", side_effect=lambda *a, **kw: ready.put(kw)):
            t = threading.Thread(target=server)
            t.start()
            ready.get(timeout=3)
            rows = p.dial(dict(ip="127.0.0.1", sizes=payload["sizes"], targets=[payload]))
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(rows[0]["status"], "PASS")
        self.assertIsInstance(result.get(), dict)

    def test_bind_conflict_no_ready(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            with patch.object(p, "emit") as emit:
                with self.assertRaises(OSError):
                    p.serve({"ip": "127.0.0.1", "ports": [s.getsockname()[1]]})
                emit.assert_not_called()

    def test_refused_not_pass(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        rows = p.dial({"ip": "127.0.0.1", "sizes": [8],
                       "targets": [{"ip": "127.0.0.1", "ports": [port], "token": "a"*64}]})
        self.assertEqual(rows[0]["status"], "FAIL")

    def test_dns_real(self):
        r = p.dns_probe({"peers": ["127.0.0.1"]})
        self.assertEqual(r[0]["rc"], 0)
        self.assertIsInstance(r[0]["consistent"], bool)  # 本机 DNS 配置可能真实不一致。

    def test_command_timeout(self):
        out = p.run_cmd([sys.executable, "-c", "import time;time.sleep(30)"], timeout=.15)
        self.assertEqual(out["rc"], 124)

    def test_remote_bootstrap_without_file(self):
        payload = {"action": "dns", "peers": ["127.0.0.1"], "timeout_s": 5}
        result = subprocess.run([sys.executable, "-u", "-c", p.BOOT, "--agent", p.encode(payload)],
                                input=p.SOURCE, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"event": "complete"', result.stdout)


class BenchTests(unittest.TestCase):
    def test_linux_plan_generated_on_windows(self):
        args = SimpleNamespace(host=["a:8", "b:8"], directory="/usr/local/Ascend/tools/hccl_test",
                               op="all2all", mpi="mpich", profile="large-1g", aiv=True,
                               check=False, fullmesh=True, source="/opt/test-env/setup.sh")
        out = b.plan(args, "hostfile")
        self.assertEqual(out["binary"], "/usr/local/Ascend/tools/hccl_test/bin/alltoall_test")
        self.assertIn("level0:fullmesh", out["shell"])

    def test_hostfile_variants(self):
        hosts = b.parse_hosts(["worker-red:4", "worker-blue:4", "worker-green:4"])
        self.assertEqual(b.hostfile(hosts, "mpich"), "worker-red:4\nworker-blue:4\nworker-green:4\n")
        self.assertIn("worker-green slots=4", b.hostfile(hosts, "openmpi"))

    def test_host_injection(self):
        with self.assertRaises(ValueError):
            b.parse_hosts(["a;rm:8", "b:8"])

    def test_card_count_mismatch(self):
        with self.assertRaises(ValueError):
            b.parse_hosts(["a:8", "b:16"])

    def test_plan_uses_all_explicit_targets_and_sums_ranks(self):
        args = SimpleNamespace(host=["worker-red:4", "worker-blue:4", "worker-green:4"],
                               directory="/opt/cann/tools/hccl_test", source=None, inherit_env=True,
                               op="allreduce", mpi="openmpi", profile="smoke", aiv=False, check=False, fullmesh=False)
        out = b.plan(args, "ranks")
        self.assertEqual(out["argv"][out["argv"].index("-n")+1], "12")
        self.assertEqual([h for h, _ in out["hosts"]], ["worker-red", "worker-blue", "worker-green"])
        self.assertEqual(out["environment_mode"], "inherit")
        self.assertNotIn("source ", out["shell"])
        self.assertNotIn("HCCL_ALGO", out["shell"])

    def test_bench_cli_requires_target_environment_and_directory(self):
        for argv in (
            ["--host", "a:1", "--host", "b:1", "--mpi", "mpich", "--directory", "/opt/tests"],
            ["--host", "a:1", "--host", "b:1", "--mpi", "mpich", "--inherit-env"],
            ["--mpi", "mpich", "--inherit-env", "--directory", "/opt/tests"],
        ):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, b.__file__, *argv], capture_output=True)
                self.assertEqual(result.returncode, 2)

    def test_plan_no_implicit_environment(self):
        args = SimpleNamespace(host=["a:1", "b:1"], source=None)
        with self.assertRaisesRegex(ValueError, "source"):
            b.plan(args, "ranks")

    def test_large_profile_legacy_alias_not_tied_to_nodes(self):
        args = SimpleNamespace(host=["worker-orange:2", "worker-purple:2"],
                               directory="/opt/tests", source="/opt/env.sh",
                               op="all2all", mpi="mpich", profile="large-1g", aiv=True, check=False, fullmesh=False)
        large = b.plan(args, "ranks")
        args.profile = "historical-1g"
        self.assertEqual(large["argv"], b.plan(args, "ranks")["argv"])


if __name__ == "__main__":
    unittest.main()

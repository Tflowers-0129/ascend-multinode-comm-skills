"""合成设备证据/本地进程回归；不连接历史服务器，不冒充硬件验收。"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts/fabric_probe.py"
spec = importlib.util.spec_from_file_location("fabric_probe", SCRIPT)
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
HEADER = "NPU ID   Chip ID   Chip Logic ID   Chip Phy-ID   Chip Name\n"
HELP = """hccn_tool -i devid -hccs_ping
  [-g] : get result
  address %s : IPv4
  cnt %d : packet count
  timeout %d : wait time
"""


def result(text, rc=0, **kw):
    return dict(text=text, rc=rc, truncated=False, **kw)


def device(physical=3, pod=7, index=0, ip="192.0.2.10", sdid=17):
    raw = dict(mapping=dict(card=5, chip=1, logical=9, physical=physical, chip_name="Ascend910"),
               vnic=result(f"vnic link status: UP\nvnic ipaddr: {ip}\nvnic netmask: 255.255.255.0\n"),
               spod=result(f"SDID : {sdid}\nSuper Pod Size : 32\nSuper Pod ID : {pod}\nServer Index : {index}\n"),
               ip=result("Get ipconf failed, because no ip was preset there!", 13),
               link=result("link status: DOWN\n"), hccs=result("hccs health status : OK\n"))
    return f.parse_device(raw)


def fixtures():
    config = {"nodes": [{"name": "node-a", "ssh": "audit@node-a"},
                        {"name": "node-b", "ssh": "audit@node-b"},
                        {"name": "node-c", "ssh": "audit@node-c"}]}
    data = {"node-a": {"devices": [device(pod=0)], "hccn_help": result(HELP)},
            "node-b": {"devices": [device()], "hccn_help": result(HELP)},
            "node-c": {"devices": [device(index=1, ip="192.0.2.20", sdid=33)], "hccn_help": result(HELP)}}
    return config, data


class MappingTests(unittest.TestCase):
    def test_collection_uses_real_mapping_not_platform_guess_or_dense_indices(self):
        calls = []
        def query(argv, **kw):
            calls.append(argv)
            if argv == ["npu-smi", "info", "-m"]:
                return result(HEADER + "5 1 9 3 Ascend910\n")
            if argv == ["npu-smi", "info", "--help"]:
                return result("board, topo, hccs, spod-info")
            if argv == ["hccn_tool", "-h"]:
                return result(HELP + "\nhccn_tool -i devid -vnic\n [-g] : read")
            return result("unsupported", 1)
        with patch.object(f, "command", side_effect=query):
            inv = f.collect()
        self.assertEqual([d["physical"] for d in inv["devices"]], [3])
        self.assertIn(["npu-smi", "info", "-t", "spod-info", "-i", "5", "-c", "1"], calls)
        self.assertIn(["hccn_tool", "-i", "3", "-vnic", "-g"], calls)
        self.assertFalse(any("-hccs_ping" in c for c in calls))

    def test_noncontiguous_card_chip_mapping_and_mcu(self):
        ds = f.parse_mapping(result(HEADER + "5 1 9 3 Ascend910\n5 2 - - Mcu\n8 0 12 11 Ascend950\n"))
        self.assertEqual([(d["card"], d["chip"], d["logical"], d["physical"]) for d in ds],
                         [(5, 1, 9, 3), (8, 0, 12, 11)])

    def test_unknown_failed_truncated_or_partial_mapping_rejected(self):
        for r in (result("0 0 0 0 Ascend910"), result(HEADER + "0 0 0 0 Ascend910", 1),
                  dict(result(HEADER + "0 0 0 0 Ascend910"), truncated=True),
                  result(HEADER + "0 0 0 0 Ascend910\n1 1 - 2 Ascend910")):
            with self.subTest(r=r), self.assertRaises(ValueError):
                f.parse_mapping(r)

    def test_duplicate_physical_or_card_chip_rejected(self):
        for line in ("8 1 12 3 Ascend910", "5 1 12 11 Ascend910"):
            with self.assertRaises(ValueError):
                f.parse_mapping(result(HEADER + "5 1 9 3 Ascend910\n" + line))

    def test_zero_pod_and_sdid_are_valid(self):
        d = device(pod=0, sdid=0)
        self.assertEqual((d["pod"], d["sdid"]), (0, 0))

    def test_error_ip_and_duplicate_fields_are_not_address_evidence(self):
        for r in (result("vnic ipaddr: 192.0.2.3", 1),
                  result("error on 192.0.2.3"),
                  result("vnic ipaddr: 192.0.2.3\nvnic ipaddr: 192.0.2.4")):
            self.assertIsNone(f.field(r, "vnic ipaddr"))
        for value in ("0.0.0.0", "::1", "999.1.2.3", "224.0.0.1", None):
            self.assertIsNone(f.ipv4(value))


class ClassificationTests(unittest.TestCase):
    def test_roce_down_does_not_hide_hccs_and_fullmesh_not_claimed(self):
        c = f.classify(dict(devices=[device()], topo=result("Phy-ID3 X HCCS_SW\nPhy-ID11 HCCS_SW X\n")))
        self.assertEqual(c["roce"], "NOT_CONFIGURED_LINK_DOWN")
        self.assertEqual(c["hccs"], "OBSERVED")
        self.assertEqual(c["inter_node_physical_fullmesh"], "UNVERIFIED")
        self.assertEqual(c["hccl_algo"], "NOT_COLLECTED")

    def test_legend_only_or_failed_query_not_topology(self):
        for r in (result("HCCS_SW = through switch"), result("Phy-ID0 X HCCS_SW", 1)):
            self.assertEqual(f.classify(dict(topo=r))["intranode_topology"], [])

    def test_missing_tools_or_empty_devices_not_negative_hardware_proof(self):
        c = f.classify(dict(error="SSH failed", urma=result("not found", 127)))
        self.assertEqual(c["hccs"], "UNVERIFIED")
        self.assertEqual(c["roce"], "UNVERIFIED")
        self.assertEqual(c["ub_uboe"], "NO_EVIDENCE")

    def test_character_device_is_only_ub_candidate(self):
        c = f.classify(dict(paths={"/dev/uburma": "other"}))
        self.assertEqual(c["ub_uboe"], "CANDIDATE_REQUIRES_LINK_EVIDENCE")


class AttributionTests(unittest.TestCase):
    def test_same_pod_pass_plan_despite_duplicate_in_other_pod(self):
        _, inv = fixtures()
        row = f.plan_edge(inv, "node-c", "node-b", 3, 3)
        self.assertEqual(row["status"], "PLANNED")
        self.assertEqual(row["attribution"], "same_pod")
        self.assertEqual(len(f.collisions(inv)), 2)

    def test_local_duplicate_cannot_be_remote_success(self):
        _, inv = fixtures()
        for a, b in (("node-a", "node-b"), ("node-b", "node-a"), ("node-c", "node-a")):
            with self.subTest(a=a, b=b):
                self.assertEqual(f.plan_edge(inv, a, b, 3, 3)["status"], "UNVERIFIED")

    def test_cross_pod_unique_target_keeps_attribution_unknown(self):
        _, inv = fixtures()
        row = f.plan_edge(inv, "node-a", "node-c", 3, 3)
        self.assertEqual(row["status"], "PLANNED")
        self.assertEqual(row["attribution"], "cross_pod_unverified")

    def test_unknown_mapping_pod_or_down_vnic_not_planned(self):
        for change in ({"pod": None}, {"vnic_link": "DOWN"}, {"vnic_ip": None}):
            _, inv = fixtures()
            inv["node-c"]["devices"][0].update(change)
            self.assertEqual(f.plan_edge(inv, "node-b", "node-c", 3, 3)["status"], "UNVERIFIED")
        self.assertEqual(f.plan_edge(inv, "node-b", "node-c", 19, 3)["status"], "UNVERIFIED")

    def test_same_device_rejected_but_explicit_local_cross_card_allowed(self):
        _, inv = fixtures()
        inv["node-b"]["devices"].append(device(physical=11, ip="192.0.2.11", sdid=18))
        self.assertEqual(f.plan_edge(inv, "node-b", "node-b", 3, 3)["status"], "UNVERIFIED")
        self.assertEqual(f.plan_edge(inv, "node-b", "node-b", 3, 11)["status"], "PLANNED")

    def test_same_pod_duplicate_server_index_blocked(self):
        _, inv = fixtures()
        inv["node-c"]["devices"][0]["server_index"] = 0
        self.assertEqual(f.plan_edge(inv, "node-b", "node-c", 3, 3)["status"], "UNVERIFIED")


class PingTests(unittest.TestCase):
    def test_rc_zero_and_success_footer_with_send_failure_is_fail(self):
        raw = result("This pkt send fail!\nThis pkt ping not start to send!\n"
                     "1 packets transmitted, 0 received, 100.00% packets loss\nCmd executed successfully!")
        self.assertEqual(f.parse_ping(raw)["status"], "FAIL")

    def test_packet_count_loss_timeout_and_unknown_output(self):
        cases = [("3 packets transmitted, 3 received, 0.00% packets loss", 0, "PASS"),
                 ("3 packets transmitted, 2 received, 33.33% packets loss", 0, "FAIL"),
                 ("1 packets transmitted, 1 received, 0.00% packets loss", 0, "FAIL"),
                 ("Cmd executed successfully!", 0, "UNVERIFIED"),
                 ("3 packets transmitted, 3 received, 0.00% packets loss", 124, "FAIL")]
        for text, rc, expected in cases:
            self.assertEqual(f.parse_ping(result(text, rc))["status"], expected)

    def test_truncated_statistics_or_nonzero_plane_result_not_pass(self):
        text = "3 packets transmitted, 3 received, 0.00% packets loss"
        self.assertEqual(f.parse_ping(dict(result(text), truncated=True))["status"], "UNVERIFIED")
        self.assertEqual(f.parse_ping(result(text + "\nL1 plane check result = 0x1"))["status"], "FAIL")

    def test_supported_help_must_include_count_and_timeout_in_same_section(self):
        self.assertTrue(f.ping_supported(result(HELP)))
        self.assertFalse(f.ping_supported(result(HELP.replace("timeout %d", "wait %d"))))
        self.assertFalse(f.ping_supported(result(HELP, 1)))

    def test_command_missing_and_timeout(self):
        self.assertEqual(f.command(["ascend-test-intentionally-missing-binary"])["rc"], 127)
        self.assertEqual(f.command([sys.executable, "-c", "import time;time.sleep(3)"], timeout=.1)["rc"], 124)

    @unittest.skipUnless(os.name == "posix", "远端总预算仅 Linux")
    def test_deadline_not_swallowed_by_command(self):
        def alarm(_signum, _frame):
            raise f.ProbeDeadline("test budget")
        previous = f.signal.signal(f.signal.SIGALRM, alarm)
        try:
            f.signal.setitimer(f.signal.ITIMER_REAL, .1)
            with self.assertRaises(f.ProbeDeadline):
                f.command([sys.executable, "-c", "import time;time.sleep(3)"], timeout=2)
        finally:
            f.signal.setitimer(f.signal.ITIMER_REAL, 0)
            f.signal.signal(f.signal.SIGALRM, previous)


class WorkflowTests(unittest.TestCase):
    def test_ssh_banner_does_not_break_protocol_json(self):
        config, _ = fixtures()
        proc = subprocess.CompletedProcess([], 0, "Login banner\n" + f.PREFIX + '{"devices": []}\n', "")
        with patch.object(f.subprocess, "run", return_value=proc):
            self.assertEqual(f.remote(config["nodes"][0], {"action": "inspect"}), {"devices": []})

    def test_credentials_container_and_bad_pairs_rejected_before_ssh(self):
        config, _ = fixtures()
        for key in ("password", "container", "env_scripts"):
            c = copy.deepcopy(config)
            c["nodes"][0][key] = "example"
            with patch.object(f, "remote") as remote, self.assertRaises(ValueError):
                f.run(c)
            remote.assert_not_called()
        with patch.object(f, "remote") as remote, self.assertRaises(ValueError):
            f.run(config, "ping", "node-a", "node-c", [(3, -1)])
        remote.assert_not_called()

    def test_ssh_key_and_safe_host_policy(self):
        config, _ = fixtures()
        n = config["nodes"][0]
        n.update(identity_file="C:/keys/test key", port=2202)
        f.validate_config(config)
        argv = f.remote_argv(n, {"action": "inspect"})
        self.assertIn("C:/keys/test key", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertNotIn("source ", argv[-1])
        self.assertNotIn("docker", argv[-1])

    def test_inspect_and_plan_never_ping(self):
        config, inv = fixtures()
        for mode in ("inspect", "ping"):
            calls = []
            def remote(n, p):
                calls.append(p["action"])
                return inv[n["name"]]
            with patch.object(f, "remote", side_effect=remote):
                r = f.run(config, mode, "node-b", "node-c", [(3, 3)], execute=False)
            self.assertEqual(calls, ["inspect"] * 3)
            self.assertEqual(r["status"], "UNVERIFIED")

    def test_bidirectional_execution_and_collective_never_marked_pass(self):
        config, inv = fixtures()
        calls = []
        def remote(n, p):
            calls.append((n["name"], p["action"]))
            if p["action"] == "inspect":
                return inv[n["name"]]
            if p["action"] == "identity":
                return inv[n["name"]]["devices"][0]
            return dict(status="PASS", transmitted=3, received=3)
        with patch.object(f, "remote", side_effect=remote):
            r = f.run(config, "ping", "node-b", "node-c", [(3, 3)], bidirectional=True, execute=True)
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(len(r["edges"]), 2)
        self.assertEqual([n for n, action in calls if action == "ping"], ["node-b", "node-c"])
        self.assertIn("HCCL collective", r["unverified"])

    def test_cross_pod_response_not_promoted_and_failure_preserved(self):
        config, inv = fixtures()
        row = f.plan_edge(inv, "node-a", "node-c", 3, 3)
        nodes = {n["name"]: n for n in config["nodes"]}
        for status, expected in (("PASS", "UNVERIFIED"), ("FAIL", "FAIL")):
            with patch.object(f, "remote", side_effect=[inv["node-c"]["devices"][0], dict(status=status)]):
                self.assertEqual(f.execute_edge(nodes, inv, row)["status"], expected)

    def test_target_identity_changed_prevents_ping(self):
        config, inv = fixtures()
        row = f.plan_edge(inv, "node-b", "node-c", 3, 3)
        with patch.object(f, "remote", return_value=device(pod=19)) as remote:
            r = f.execute_edge({n["name"]: n for n in config["nodes"]}, inv, row)
        self.assertEqual(r["status"], "UNVERIFIED")
        self.assertEqual(remote.call_count, 1)

    def test_different_device_counts_not_silent_full_coverage(self):
        config, inv = fixtures()
        inv["node-b"]["devices"].append(device(physical=11, ip="192.0.2.11", sdid=18))
        with patch.object(f, "remote", side_effect=lambda n, p: inv[n["name"]]), patch.object(
                f, "execute_edge", side_effect=lambda n, i, row: dict(row, status="PASS")):
            r = f.run(config, "ping", "node-b", "node-c", same_index=True, execute=True)
        self.assertEqual(r["status"], "UNVERIFIED")
        self.assertTrue(r["coverage_notes"])

    def test_cli_existing_output_fails_before_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "old.json"
            out.write_text("preserve", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SCRIPT), "inspect", "--config", "does-not-exist",
                                   "--out", str(out)], capture_output=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(out.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()

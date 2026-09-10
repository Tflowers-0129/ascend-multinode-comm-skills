"""合成现场证据与故障注入；不是 A3/A5 硬件或版本兼容性验收。"""
import unittest
from unittest.mock import patch

from test_preflight import cfg, inv, p
from test_mc2 import case


def hardware(text, rc=0):
    return {"npu": {"rc": rc, "text": text}}


class PlatformTests(unittest.TestCase):
    def test_live_model_identity(self):
        for model, family in (("Ascend910_93", "A3"), ("NPU Name: 910_9392", "A3"),
                              ("Ascend 950DT", "A5"), ("950PR_9599", "A5")):
            with self.subTest(model=model):
                row = p.platform_identity(hardware(model))
                self.assertEqual(row["observed"], family)
                self.assertEqual(row["status"], "PASS")

    def test_declared_family_and_architecture_are_not_hardware_proof(self):
        for value in ("DAV_2201", "ASCEND910B", "A3", "Qwen-A3B", "container-a5", "my910_93image"):
            with self.subTest(value=value):
                row = p.platform_identity(hardware(value), "A3")
                self.assertEqual(row["status"], "UNVERIFIED")
                self.assertEqual(row["observed"], "UNKNOWN")

    def test_failed_command_is_not_model_evidence(self):
        self.assertEqual(p.platform_identity(hardware("910_9392", 1))["status"], "UNVERIFIED")

    def test_a2_not_misidentified_as_a3(self):
        row = p.platform_identity(hardware("Ascend910B2"))
        self.assertEqual(row["observed"], "A2")
        self.assertEqual(row["status"], "UNVERIFIED")
        self.assertEqual(p.platform_identity(hardware("Ascend910B2"), "A3")["status"], "FAIL")

    def test_mismatch_and_conflicting_live_models_fail(self):
        self.assertEqual(p.platform_identity(hardware("950DT"), "A3")["status"], "FAIL")
        self.assertEqual(p.platform_identity(hardware("910_9392 950PR"))["status"], "FAIL")

    def test_hccs_candidate_not_active_transport_or_fullmesh_proof(self):
        row = hardware("910_9392")
        row.update(env={"HCCL_ALGO": "level0:fullmesh"}, ub_devices=[], urma={"rc": 127, "text": "absent"})
        result = p.topology(row)
        self.assertIn("HCCS", result["capability_candidates"])
        self.assertNotIn("UB/URMA", result["capability_candidates"])
        self.assertEqual(result["transport"], "UNVERIFIED")
        self.assertEqual(result["physical_topology"], "UNVERIFIED")
        self.assertTrue(result["fullmesh_configured"])

    def test_a3_readonly_fields_do_not_leak_to_unknown_or_a5(self):
        self.assertIn("vnic", p.hccn_fields(hardware("910_9392")))
        self.assertIn("netdetect", p.hccn_fields(hardware("910_9392")))
        for row in ({}, hardware("950PR"), hardware("DAV_2201")):
            self.assertNotIn("vnic", p.hccn_fields(row))
        self.assertNotIn("hccs_ping", p.hccn_fields(hardware("910_9392")))

    def test_mixed_platforms_in_one_group_require_separate_support(self):
        config = cfg()
        result = p.platform_audit(config["nodes"], {"a": hardware("910_9392"), "b": hardware("950PR")}, config["groups"])
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["mixed_groups"], ["model"])
        self.assertTrue(result["block_active"])

    def test_separate_pd_groups_not_one_mixed_collective_group(self):
        config = cfg()
        groups = [{"name": "prefill", "nodes": ["a"]}, {"name": "decode", "nodes": ["b"]}]
        result = p.platform_audit(config["nodes"], {"a": hardware("910_9392"), "b": hardware("950DT")}, groups)
        self.assertEqual(result["status"], "PASS")  # 仅平台身份，不是跨平台 KV 认证。
        self.assertFalse(result["block_active"])

    def test_arbitrary_nodes_and_card_lists_and_cpu_only(self):
        config = cfg()
        config["nodes"][0]["devices"] = [1, 5, 7]
        config["nodes"].extend([{"name": "c", "ssh": "node-c", "devices": [3, 6]},
                                {"name": "store", "ssh": "store", "role": "store", "devices": []}])
        config["groups"][0]["nodes"].append("c")
        p.validate_config(config)
        result = p.platform_audit(config["nodes"], {n: hardware("910_9392") for n in ("a", "b", "c")}, config["groups"])
        self.assertEqual(set(result["nodes"]), {"a", "b", "c"})
        self.assertEqual(result["status"], "PASS")

    def test_invalid_platform_rejected_before_connection(self):
        for value in ("A2", "a3", "auto-detect", None, []):
            config = cfg()
            config["nodes"][0]["platform"] = value
            with self.subTest(value=value), patch.object(p, "call_remote") as remote:
                with self.assertRaisesRegex(ValueError, "platform"):
                    p.run(config, "inspect")
                remote.assert_not_called()

    def fake_remote(self, models, calls):
        def remote(node, payload, env=None):
            calls.append(payload["action"])
            result = inv(["192.0.2.1" if node["name"] == "a" else "192.0.2.2"])
            result.update(proxy_present=False, mounts=[], env={}, **hardware(models[node["name"]]))
            if payload["action"] == "dns":
                result = [dict(rc=0, consistent=True)]
            return dict(status="PASS", events=[dict(event="result", result=result)])
        return remote

    def test_inspect_conflict_does_not_launch_probes(self):
        config, calls = cfg(), []
        config["nodes"][0]["platform"] = "A3"
        with patch.object(p, "call_remote", side_effect=self.fake_remote({"a": "950DT", "b": "950PR"}, calls)), patch.object(p, "tcp_matrix") as tcp:
            report = p.run(config, "check")
        self.assertEqual(report["checks"]["platform"]["status"], "FAIL")
        self.assertEqual(calls, ["inventory", "inventory"])
        tcp.assert_not_called()
        self.assertFalse(p.gate(report, "primitives"))

    def test_pairs_cannot_bypass_mixed_platform_guard_via_separate_groups(self):
        config, calls = cfg(), []
        config["groups"] = [{"name": "p", "nodes": ["a"]}, {"name": "d", "nodes": ["b"]}]
        with patch.object(p, "call_remote", side_effect=self.fake_remote({"a": "910_9392", "b": "950PR"}, calls)), patch.object(p, "run_group") as group:
            report = p.run(config, "pairs")
        group.assert_not_called()
        self.assertEqual(report["checks"]["platform"]["mixed_groups"], ["card_pairs"])
        self.assertFalse(p.gate(report, "pairs"))

    def test_unknown_identity_can_test_primitives_but_not_mc2_or_service(self):
        config, calls = cfg(), []
        config.update(require_mc2=True, mc2_cases=[case()])
        for node in config["nodes"]:
            node["platform"] = "A3"
        with patch.object(p, "call_remote", side_effect=self.fake_remote({"a": "DAV_2201", "b": "DAV_2201"}, calls)), patch.object(p, "tcp_matrix", return_value=dict(status="PASS")), patch.object(p, "run_group", return_value=dict(status="PASS")) as group:
            report = p.run(config, "check")
        self.assertEqual(group.call_count, 3)
        self.assertEqual(report["checks"]["platform"]["status"], "UNVERIFIED")
        self.assertEqual(report["checks"]["mc2/dense"]["status"], "UNVERIFIED")
        self.assertTrue(p.gate(report, "primitives"))
        self.assertFalse(p.gate(report, "mc2"))
        self.assertFalse(p.gate(report, "service"))

    def test_mc2_environment_is_explicit_not_a3_defaults(self):
        config = cfg()
        self.assertNotIn("environment", p.validate_config(config))
        config["environment"] = {"HCCL_BUFFSIZE": "256", "HCCL_OP_EXPANSION_MODE": "AIV"}
        p.validate_config(config)
        argv = p.remote_argv(config["nodes"][0], {"action": "inventory"}, config["environment"])
        self.assertIn("HCCL_OP_EXPANSION_MODE=AIV", argv[-1])


if __name__ == "__main__":
    unittest.main()

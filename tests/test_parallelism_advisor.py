import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts"
sys.path.insert(0, str(SCRIPTS))
import parallelism_advisor as a


def config():
    return {
        "schema_version": 1,
        "name": "planner-test",
        "mode": "quick",
        "objective": "output_throughput",
        "environment": {
            "platform": "A2", "model": "large-moe", "vllm": "x",
            "vllm_ascend": "x", "cann": "x", "connector": "x", "transport": "hccs",
        },
        "hardware": {"cluster_devices": 64, "devices_per_host": 8},
        "workload": {
            "concurrency": 48, "mean_uncached_input_tokens": 65536,
            "mean_output_tokens": 1024, "prefix_hit_rate": 0.9, "qps": 1.0,
        },
        "roles": {
            "prefill": {
                "devices": 32, "tp_sizes": [4, 8, 16], "pp_sizes": [1, 2, 4],
                "minimum_replica_devices": 16, "maximum_dp": 4,
                "require_node_local_tp": True,
            },
            "decode": {
                "devices": 32, "tp_sizes": [2, 4, 8], "pp_sizes": [1],
                "minimum_replica_devices": 2, "maximum_dp": 16,
                "require_node_local_tp": True,
            },
        },
        "constraints": {
            "prefill_tp_multiple_of_decode_tp": True,
            "decode_pp_must_be_one": True,
            "forbid_cross_host_tp": True,
        },
        "max_recommendations": 3,
    }


class ValidationTests(unittest.TestCase):
    def test_public_example_validates(self):
        path = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/examples/parallelism-advisor.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        a.validate_config(cfg)

    def test_public_example_is_draft_until_placeholders_are_filled(self):
        path = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/examples/parallelism-advisor.json"
        report = a.advise(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(report["status"], "DRAFT_RECOMMENDATION")
        self.assertTrue(report["input_warnings"])

    def test_unknown_bool_duplicate_and_nonfinite_rejected(self):
        cases = []
        item = config(); item["schema_version"] = True; cases.append(item)
        item = config(); item["command"] = ["ssh"]; cases.append(item)
        item = config(); item["hardware"]["cluster_devices"] = True; cases.append(item)
        item = config(); item["roles"]["decode"]["tp_sizes"] = [2, 2]; cases.append(item)
        item = config(); item["workload"]["qps"] = float("nan"); cases.append(item)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    a.validate_config(value)

    def test_json_duplicate_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "duplicate.json"
            source.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复字段"):
                a._load_config(source)

    def test_extreme_numbers_fail_closed_without_nonstandard_json(self):
        cfg = config()
        cfg["workload"]["qps"] = 10 ** 10000
        with self.assertRaisesRegex(ValueError, "有限数值"):
            a.validate_config(cfg)
        cfg = config()
        cfg["workload"]["qps"] = 1e308
        with self.assertRaisesRegex(ValueError, "负载代理"):
            a.advise(cfg)
        for field in ("mean_uncached_input_tokens", "mean_output_tokens"):
            cfg = config()
            cfg["workload"][field] = 10 ** 1000
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "负载代理"):
                a.advise(cfg)

    def test_role_budgets_cannot_exceed_cluster(self):
        cfg = config()
        cfg["hardware"]["cluster_devices"] = 32
        with self.assertRaisesRegex(ValueError, "预算之和"):
            a.validate_config(cfg)

    def test_quick_and_exhaustive_recommendation_limits(self):
        cfg = config(); cfg["max_recommendations"] = 5
        with self.assertRaises(ValueError):
            a.validate_config(cfg)
        cfg["mode"] = "exhaustive"
        a.validate_config(cfg)


class AdvisorTests(unittest.TestCase):
    def test_output_throughput_prefers_expected_theoretical_shape(self):
        report = a.advise(config())
        self.assertEqual(report["status"], "RECOMMENDED_FOR_VALIDATION")
        self.assertEqual(report["confidence"], "THEORY_ONLY")
        self.assertFalse(report["input_warnings"])
        self.assertEqual(report["budget_summary"]["allocated_devices"], 64)
        self.assertEqual(report["budget_summary"]["unallocated_devices"], 0)
        self.assertIn("priority", report["ranking_policy"])
        self.assertEqual(
            {key: report["recommended"]["prefill"][key] for key in ("dp", "tp", "pp")},
            {"dp": 2, "tp": 8, "pp": 2},
        )
        self.assertEqual(
            {key: report["recommended"]["decode"][key] for key in ("dp", "tp", "pp")},
            {"dp": 16, "tp": 2, "pp": 1},
        )

    def test_larger_fast_domain_changes_prefill_choice(self):
        cfg = config()
        cfg["objective"] = "ttft"
        cfg["hardware"]["devices_per_host"] = 16
        report = a.advise(cfg)
        self.assertEqual(report["recommended"]["prefill"]["tp"], 16)
        self.assertEqual(report["recommended"]["prefill"]["pp"], 1)

    def test_tpot_does_not_assume_more_dp_is_always_better(self):
        cfg = config()
        cfg["objective"] = "tpot"
        report = a.advise(cfg)
        self.assertEqual(report["recommended"]["decode"]["tp"], 8)
        self.assertEqual(report["recommended"]["decode"]["dp"], 4)

    def test_request_throughput_and_goodput_have_distinct_policies(self):
        cfg = config()
        cfg["roles"]["prefill"]["minimum_replica_devices"] = 8
        cfg["objective"] = "output_throughput"
        output = a.advise(cfg)
        cfg["objective"] = "request_throughput"
        requests = a.advise(cfg)
        cfg["objective"] = "goodput"
        goodput = a.advise(cfg)
        self.assertNotEqual(output["ranking_policy"], requests["ranking_policy"])
        self.assertNotEqual(requests["ranking_policy"], goodput["ranking_policy"])
        self.assertGreaterEqual(requests["recommended"]["prefill"]["dp"],
                                output["recommended"]["prefill"]["dp"])

    def test_minimum_replica_devices_filters_small_shape(self):
        cfg = config()
        cfg["roles"]["decode"]["minimum_replica_devices"] = 4
        report = a.advise(cfg)
        self.assertNotEqual(report["recommended"]["decode"]["tp"], 2)
        reasons = {item["reason_code"] for item in report["rejected_summary"]["decode"]}
        self.assertIn("MINIMUM_REPLICA_DEVICES", reasons)

    def test_every_candidate_conserves_role_devices_and_tp_ratio(self):
        cfg = config()
        report = a.advise(cfg)
        for item in report["ranked_candidates"]:
            for role in ("prefill", "decode"):
                row = item[role]
                self.assertEqual(row["dp"] * row["tp"] * row["pp"], cfg["roles"][role]["devices"])
            self.assertGreaterEqual(item["prefill"]["tp"], item["decode"]["tp"])
            self.assertEqual(item["prefill"]["tp"] % item["decode"]["tp"], 0)

    def test_host_capacity_does_not_claim_joint_placement(self):
        cfg = config()
        cfg["hardware"] = {"cluster_devices": 16, "devices_per_host": 8}
        cfg["roles"]["prefill"].update({
            "devices": 12, "tp_sizes": [6], "pp_sizes": [1],
            "minimum_replica_devices": 6, "maximum_dp": 2,
        })
        cfg["roles"]["decode"].update({
            "devices": 4, "tp_sizes": [4], "pp_sizes": [1],
            "minimum_replica_devices": 4, "maximum_dp": 1,
        })
        cfg["constraints"]["prefill_tp_multiple_of_decode_tp"] = False
        report = a.advise(cfg)
        self.assertEqual(report["budget_summary"]["placement_status"], "NOT_VALIDATED")
        self.assertTrue(report["recommended"]["prefill"]["tp_fits_single_host_capacity"])
        self.assertTrue(any("placement" in item for item in report["must_verify"]))

    def test_key_order_does_not_change_report(self):
        cfg = config()
        reordered = json.loads(json.dumps(cfg, sort_keys=True))
        self.assertEqual(a.advise(cfg), a.advise(reordered))

    def test_report_never_claims_verified_or_global_optimum(self):
        text = json.dumps(a.advise(config()), ensure_ascii=False)
        for forbidden in ("已验证", "全局最优", "guaranteed", "optimal"):
            self.assertNotIn(forbidden, text)

    def test_script_has_no_remote_or_process_capability(self):
        source = Path(a.__file__).read_text(encoding="utf-8")
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue({"subprocess", "socket", "paramiko", "requests"}.isdisjoint(imports))
        forbidden_calls = {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                           "spawnv", "spawnve", "spawnvp", "spawnvpe"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_calls)

    def test_cli_is_utf8_and_does_not_overwrite(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            target = root / "report.json"
            source.write_text(json.dumps(cfg), encoding="utf-8")
            first = subprocess.run([sys.executable, a.__file__, "--config", str(source),
                                    "--out", str(target)], capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["confidence"], "THEORY_ONLY")
            second = subprocess.run([sys.executable, a.__file__, "--config", str(source),
                                     "--out", str(target)], capture_output=True)
            self.assertEqual(second.returncode, 2)
            forced = subprocess.run([sys.executable, a.__file__, "--config", str(source),
                                     "--out", str(target), "--force"], capture_output=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            unused_force = subprocess.run([sys.executable, a.__file__, "--config", str(source),
                                           "--force"], capture_output=True)
            self.assertEqual(unused_force.returncode, 2)
            protected = subprocess.run([sys.executable, a.__file__, "--config", str(source),
                                        "--out", str(source), "--force"], capture_output=True)
            self.assertEqual(protected.returncode, 2)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), cfg)

    def test_concurrent_writers_do_not_silently_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            target = root / "report.json"
            source.write_text(json.dumps(config()), encoding="utf-8")
            command = [sys.executable, a.__file__, "--config", str(source), "--out", str(target)]
            processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for _ in range(2)]
            results = [process.communicate() + (process.returncode,) for process in processes]
            self.assertEqual(sorted(result[2] for result in results), [0, 2])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["confidence"],
                             "THEORY_ONLY")


if __name__ == "__main__":
    unittest.main()

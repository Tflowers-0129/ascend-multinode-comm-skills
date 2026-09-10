import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts"))
import audit_deployment as a


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_audit(self, deployments=None, **extra):
        manifest = dict(schema_version=1, deployments=deployments, **extra) if deployments else None
        return a.audit(self.root, manifest)

    def codes(self, report):
        return {f["code"] for f in report["findings"]}

    def test_definite_invalid_ip_and_port(self):
        self.put("a.sh", "export HCCL_IF_IP=eth0\nvllm serve model --port 70000\n")
        r = self.run_audit()
        self.assertEqual(r["status"], "ERRORS_FOUND")
        self.assertTrue({"INVALID_NUMBER", "INVALID_HCCL_IP"} <= self.codes(r))

    def test_provenance_from_source_and_multiline(self):
        self.put("common.env", "IP=10.0.0.1\nexport HCCL_IF_IP=$IP\n")
        self.put("a.sh", "source ./common.env\nvllm serve model \\\n  --data-parallel-size 8 \\\n  --port 8000\n")
        r = self.run_audit([dict(id="a", entry="a.sh", node="n1", group="d")])
        f = r["units"][0]["facts"]
        self.assertEqual(f["HCCL_IF_IP"]["value"], "10.0.0.1")
        self.assertEqual(f["HCCL_IF_IP"]["file"], "common.env")
        self.assertEqual(f["HCCL_IF_IP"]["line"], 2)
        self.assertEqual(f["api_port"]["line"], 4)

    def test_single_quotes_do_not_expand(self):
        env = {"IP": {"value": "10.0.0.1"}}
        tokens = a.lex("a '$IP' \"$IP\" $" + "{MISSING:-8000}", env, 1)
        self.assertEqual([x[0] for x in tokens], ["a", "$IP", "10.0.0.1", "8000"])

    def test_command_substitution_never_executes(self):
        marker = self.root / "executed"
        self.put("a.sh", "IP=$(touch " + str(marker) + ")\nexport HCCL_IF_IP=$IP\nvllm serve m\n")
        r = self.run_audit()
        self.assertFalse(marker.exists())
        self.assertIn("DYNAMIC_SHELL", self.codes(r))
        self.assertIn("UNRESOLVED_VALUE", self.codes(r))

    def test_export_after_launch_not_retroactive(self):
        self.put("a.sh", "vllm serve m\nexport HCCL_IF_IP=10.0.0.1\n")
        r = self.run_audit()
        self.assertNotIn("HCCL_IF_IP", r["units"][0]["facts"])
        self.assertIn("LATE_COMM_ENV", self.codes(r))

    def test_elastic_and_dynamic_ports_not_definite_error(self):
        self.put("a.sh", "torchrun --nnodes 1:2 --nproc-per-node auto --master-port 0 train.py\n")
        r = self.run_audit()
        self.assertEqual(r["counts"]["ERROR"], 0)
        self.assertIn("ELASTIC_LAYOUT", self.codes(r))
        self.assertIn("DYNAMIC_PORT", self.codes(r))

    def test_torchrun_payload_not_launcher_flags(self):
        self.put("a.sh", "torchrun --master-port 8000 train.py --port 99999 --node-rank -1\n")
        r = self.run_audit()
        self.assertEqual(r["counts"]["ERROR"], 0)
        self.assertNotIn("api_port", r["units"][0]["facts"])
        self.assertNotIn("node_rank", r["units"][0]["facts"])
        self.put("a.sh", "python -m torch.distributed.run --master_port 99999 train.py\n")
        self.assertIn("INVALID_NUMBER", self.codes(self.run_audit()))

    def test_kv_malformed_layout_value_does_not_crash(self):
        kv = {"kv_connector_extra_config": {"decode": {"dp_size": {"invalid": 8}}}}
        self.put("p.sh", "vllm serve m --kv-transfer-config '" + json.dumps(kv) + "'\n")
        self.put("d.sh", "vllm serve m --data-parallel-size 8\n")
        r = self.run_audit([dict(id=x, entry=x+".sh", group=x) for x in ("p", "d")],
                           pd_links=[dict(prefill_group="p", decode_group="d")])
        self.assertIn("KV_LAYOUT_MISMATCH", self.codes(r))

    def test_echoed_command_not_a_service(self):
        self.put("a.sh", "echo python launch_online_dp.py --port 70000\n")
        r = self.run_audit()
        self.assertEqual(r["units"], [])

    def test_after_exit_not_a_service(self):
        self.put("a.sh", "exit 0\nvllm serve m --port 70000\n")
        self.assertEqual(self.run_audit()["units"], [])

    def test_inline_env_only_applies_to_one_command(self):
        self.put("a.sh", "HCCL_IF_IP=10.0.0.1 vllm serve m\nvllm serve m\n")
        units = self.run_audit()["units"]
        self.assertIn("HCCL_IF_IP", units[0]["facts"])
        self.assertNotIn("HCCL_IF_IP", units[1]["facts"])

    def test_nonexported_variable_not_process_env(self):
        self.put("a.sh", "HCCL_IF_IP=10.0.0.1\nvllm serve m\n")
        r = self.run_audit()
        self.assertIn("ENV_NOT_EXPORTED", self.codes(r))
        self.assertNotIn("HCCL_IF_IP", r["units"][0]["facts"])

    def test_group_overlap_and_duplicate_ip(self):
        script = "export HCCL_IF_IP=10.0.0.1\nvllm serve m --data-parallel-size 16 --data-parallel-size-local 8 --data-parallel-start-rank 0\n"
        self.put("a.sh", script)
        self.put("b.sh", script)
        r = self.run_audit([dict(id=x, entry=x+".sh", group="d", node=x) for x in ("a", "b")])
        self.assertTrue({"DP_OVERLAP", "DUPLICATE_HOST_IP"} <= self.codes(r))

    def test_separate_pd_groups_not_compared(self):
        self.put("p.sh", "vllm serve m --tensor-parallel-size 8 --data-parallel-size 1\n")
        self.put("d.sh", "vllm serve m --tensor-parallel-size 1 --data-parallel-size 16\n")
        r = self.run_audit([dict(id=x, entry=x+".sh", group=x, node=x) for x in ("p", "d")])
        self.assertNotIn("GROUP_MISMATCH", self.codes(r))

    def test_port_reuse_across_nodes_is_legal(self):
        for x in ("a", "b"):
            self.put(x+".sh", "vllm serve m --port 8000\n")
        contexts = [dict(id=x, entry=x+".sh", node=x, namespace="host") for x in ("a", "b")]
        self.assertNotIn("POSSIBLE_PORT_COLLISION", self.codes(self.run_audit(contexts)))
        contexts[1]["node"] = "a"
        self.assertIn("POSSIBLE_PORT_COLLISION", self.codes(self.run_audit(contexts)))

    def test_missing_group_does_not_assert_overlap(self):
        for x in ("a", "b"):
            self.put(x+".sh", "export RANK=0\nvllm serve m\n")
        r = self.run_audit()
        self.assertIn("GROUP_NOT_DECLARED", self.codes(r))
        self.assertNotIn("DUPLICATE_RANK", self.codes(r))

    def test_kv_layout_compare_and_secrets_redacted(self):
        kv = {"kv_connector": "MooncakeConnectorV1", "token": "never-publish-this",
              "kv_connector_extra_config": {"decode": {"dp_size": 8, "tp_size": 1}}}
        self.put("p.sh", "export http_proxy=http://alice:secret-password@proxy:8080\nvllm serve m --data-parallel-size 1 --tensor-parallel-size 8 --kv-transfer-config '" + json.dumps(kv) + "'\n")
        self.put("d.sh", "vllm serve m --data-parallel-size 16 --tensor-parallel-size 1\n")
        r = self.run_audit([dict(id=x, entry=x+".sh", node=x, group=x) for x in ("p", "d")],
                           pd_links=[dict(prefill_group="p", decode_group="d")])
        self.assertIn("KV_LAYOUT_MISMATCH", self.codes(r))
        self.assertNotIn("never-publish-this", json.dumps(r))
        self.assertNotIn("secret-password", json.dumps(r))

    def test_invalid_kv_json(self):
        self.put("a.sh", "vllm serve m --kv-transfer-config '{bad}'\n")
        self.assertIn("INVALID_KV_JSON", self.codes(self.run_audit()))

    def test_conditional_error_downgraded(self):
        self.put("a.sh", "if test x = y; then\nexport HCCL_IF_IP=bad\nvllm serve m --port 99999\nfi\n")
        r = self.run_audit()
        self.assertEqual(r["counts"]["ERROR"], 0)
        self.assertIn("CONTROL_FLOW", self.codes(r))

    def test_docker_boundary_and_rootinfo(self):
        self.put("a.sh", "export HCCL_IF_IP=10.0.0.1\ndocker run -v /etc/hccl_rootinfo.json:/etc/hccl_rootinfo.json image vllm serve m\n")
        r = self.run_audit()
        self.assertTrue({"CONTAINER_ENV_BOUNDARY", "ROOTINFO_MOUNT", "CONTAINER_NETWORK"} <= self.codes(r))
        self.assertEqual(r["units"], [])

    def test_heredoc_content_is_not_executed(self):
        self.put("a.sh", "cat <<'EOF'\nvllm serve m --port 99999\nEOF\n")
        r = self.run_audit()
        self.assertIn("HEREDOC_REVIEW", self.codes(r))
        self.assertNotIn("INVALID_NUMBER", self.codes(r))

    def test_source_cycle_is_bounded(self):
        self.put("a.sh", "source b.env\nvllm serve m\n")
        self.put("b.env", "source a.sh\n")
        self.assertIn("SOURCE_CYCLE", self.codes(self.run_audit([dict(id="a", entry="a.sh")])))

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.run_audit([dict(id="a", entry="../outside.sh")])

    def test_unknown_python_requires_manual_review(self):
        self.put("deploy.py", "raise RuntimeError('must not run')")
        r = self.run_audit()
        self.assertIn("NON_SHELL_REVIEW", self.codes(r))
        self.assertEqual(r["status"], "UNVERIFIED")

    def test_no_findings_never_means_pass(self):
        self.put("a.sh", "vllm serve m --port 8000\n")
        r = self.run_audit([dict(id="a", entry="a.sh", node="n1", group="g")])
        self.assertEqual(r["counts"]["ERROR"], 0)
        self.assertEqual(r["status"], "UNVERIFIED")
        self.assertTrue(r["runtime_required"])

    def test_sourced_exit_never_makes_following_values_definite(self):
        self.put("common.env", "exit 0\n")
        self.put("a.sh", "source common.env\nvllm serve m --port 99999\n")
        r = self.run_audit([dict(id="a", entry="a.sh")])
        self.assertIn("EXIT_FLOW_REVIEW", self.codes(r))
        self.assertEqual(r["counts"]["ERROR"], 0)

    def test_markdown_context_and_html_escaping(self):
        self.put("a.sh", "vllm serve m --port 99999\n")
        r = self.run_audit([dict(id="d0", node="<node|a>", group="g", entry="a.sh")])
        md = a.markdown(r)
        self.assertIn("入口 d0 / 节点 &lt;node", md)
        self.assertNotIn("<node", md)
        self.assertIn("api_port=99999", md)
        self.assertIn("尚需现场验证", md)

    def test_cli_exit_codes_and_output_no_overwrite(self):
        script = self.put("a.sh", "vllm serve m --port 70000\n")
        bad_out = self.root / "reports" / "bad.json"
        command = [sys.executable, a.__file__, "--root", str(self.root), "--out", str(bad_out)]
        first = subprocess.run(command, capture_output=True)
        self.assertEqual(first.returncode, 1, first.stderr)
        original = bad_out.read_bytes()
        self.assertTrue(bad_out.with_suffix(".md").is_file())
        second = subprocess.run(command, capture_output=True)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(bad_out.read_bytes(), original)
        self.assertEqual(script.read_text(encoding="utf-8"), "vllm serve m --port 70000\n")
        self.put("a.sh", "vllm serve m --port 8000\n")
        command[-1] = str(self.root / "reports" / "unverified.json")
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()

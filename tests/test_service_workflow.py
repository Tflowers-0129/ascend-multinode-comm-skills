import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts"
EXAMPLE = SCRIPTS.parent / "examples/service-workflow.json"
sys.path.insert(0, str(SCRIPTS))
import service_workflow as w


def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def resolved():
    cfg = example()
    cfg = w.render_config(cfg, {"max_num_batched_tokens": 16384, "trial": 1})
    cfg.pop("tuning")
    cfg["example"] = False
    return cfg


def successful_output():
    blocks = []
    for index in range(18):
        blocks.append("Failed Requests | 0\n")
        if index < 9:
            blocks.append("全量数据集测试完成\n")
            blocks.append(f"Output Token Throughput | {1000 + index}.0\n")
    return "".join(blocks)


def mark_running(fake, cfg, run_id="existing-run"):
    for service in cfg["services"]:
        node = w._node_map(cfg)[service["node"]]
        fake.up.add(fake.key(node, service["health"]["argv"]))
        fake.owners[service["name"]] = run_id


class FakeExecutor:
    def __init__(self, test_output=None, log_error=False, log_fatal=False,
                 owner_state="ABSENT", artifact_ok=True):
        self.up = set()
        self.test_output = successful_output() if test_output is None else test_output
        self.log_error = log_error
        self.log_fatal = log_fatal
        self.owner_state = owner_state
        self.artifact_ok = artifact_ok
        self.owners = {}
        self.detached = []
        self.stopped = []

    @staticmethod
    def key(node, argv):
        return node["name"], tuple(argv)

    def run(self, node, argv, timeout_s, env=None, cwd=None, log_path=None, log_mode="w",
            backup_existing=False, artifacts=None, total_timeout_s=None):
        if len(argv) >= 5 and argv[1:4] == ["-u", "-c", w.LOG_SCAN_SOURCE]:
            payload = json.loads(base64.b64decode(argv[4]))
            error_matches = {pattern: 0 for pattern in payload["error_patterns"]}
            fatal_matches = {pattern: 0 for pattern in payload["fatal_patterns"]}
            if self.log_error:
                error_matches[payload["error_patterns"][0]] = 1
            if self.log_fatal:
                fatal_matches[payload["fatal_patterns"][0]] = 1
            failed = self.log_error or self.log_fatal
            scan = {"path": payload["path"], "status": "FAIL" if failed else "PASS",
                    "size": 1, "error_matches": error_matches, "fatal_matches": fatal_matches,
                    "fatal": self.log_fatal}
            text = w.LOG_SCAN_PREFIX + base64.b64encode(json.dumps(scan).encode()).decode() + "\n"
            return {"rc": 1 if failed else 0, "text": text, "elapsed_s": .01, "timed_out": False,
                    "output_bytes": len(text), "output_truncated": False}
        if len(argv) >= 5 and argv[1:4] == ["-u", "-c", w.OWNER_CHECK_SOURCE]:
            payload = json.loads(base64.b64decode(argv[4]))
            if payload["service"] in self.owners:
                owner = {"state": "RUNNING", "run_id": self.owners[payload["service"]], "pid": 123}
            else:
                owner = {"state": self.owner_state}
            text = w.OWNER_PREFIX + base64.b64encode(json.dumps(owner).encode()).decode() + "\n"
            return {"rc": 0, "text": text, "elapsed_s": .01, "timed_out": False,
                    "output_bytes": len(text), "output_truncated": False}
        if len(argv) >= 4 and argv[1] == "-c" and "hashlib.sha256" in argv[2]:
            digest = "0" * 64 if self.artifact_ok else "f" * 64
            return {"rc": 0, "text": digest + "\n", "elapsed_s": .01,
                    "timed_out": False, "output_bytes": 65, "output_truncated": False}
        if len(argv) >= 4 and argv[1] == "-c" and "os.replace" in argv[2]:
            return {"rc": 0, "text": "", "elapsed_s": .01,
                    "timed_out": False, "output_bytes": 0, "output_truncated": False}
        if argv and Path(argv[0]).name == "curl":
            key = self.key(node, argv)
            text = '{"prefill_instances":2}' if "healthcheck" in argv[-1] else "ok"
            return {"rc": 0 if key in self.up else 7, "text": text if key in self.up else "",
                    "elapsed_s": .01, "timed_out": False, "output_bytes": len(text),
                    "output_truncated": False}
        if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
            return {"rc": 0, "text": self.test_output, "elapsed_s": 1,
                    "timed_out": False, "output_bytes": len(self.test_output.encode()),
                    "output_truncated": False}
        return {"rc": 0, "text": "", "elapsed_s": .01, "timed_out": False,
                "output_bytes": 0, "output_truncated": False}

    def detach(self, cfg, node, service, run_id, total_timeout_s=None):
        self.detached.append(service["name"])
        self.owners[service["name"]] = run_id
        self.up.add(self.key(node, service["health"]["argv"]))
        return {"rc": 0, "text": "", "elapsed_s": .01, "timed_out": False, "argv": []}

    def activate(self, node, service, run_id, total_timeout_s=None):
        if self.owners.get(service["name"]) != run_id:
            return {"rc": 125, "text": "owner mismatch", "elapsed_s": .01,
                    "timed_out": False, "worker_error": "owner mismatch"}
        return {"rc": 0, "text": "", "elapsed_s": .01, "timed_out": False}

    def stop(self, cfg, node, service, expected_run_id=None):
        if (expected_run_id is not None and service["name"] in self.owners and
                self.owners[service["name"]] != expected_run_id):
            return {"rc": 7, "text": "run_id mismatch", "elapsed_s": .01,
                    "timed_out": False, "output_bytes": 0, "output_truncated": False}
        self.stopped.append(service["name"])
        self.owners.pop(service["name"], None)
        self.up.discard(self.key(node, service["health"]["argv"]))
        return {"rc": 0, "text": "", "elapsed_s": .01, "timed_out": False,
                "output_bytes": 0, "output_truncated": False}


class ConfigTests(unittest.TestCase):
    def test_public_example_validates(self):
        cfg = example()
        w.validate_config(cfg)
        self.assertEqual(len(w.tuning_candidates(cfg)), 3)

    def test_max_batched_tokens_may_be_lower_than_model_length(self):
        cfg = example()
        self.assertLess(cfg["profile"]["roles"]["prefill"]["engine"]["max_num_batched_tokens"],
                        cfg["profile"]["model"]["max_model_len"])
        w.validate_config(cfg)

    def test_unknown_and_secret_fields_fail_closed(self):
        for key, value in (("unknown", 1), ("password", "do-not-store"),
                           ("api_key", "do-not-store"), ("HF_TOKEN", "do-not-store"),
                           ("AWS_SECRET_ACCESS_KEY", "do-not-store"),
                           ("CREDENTIALS", "do-not-store")):
            with self.subTest(key=key):
                cfg = example()
                cfg[key] = value
                with self.assertRaises(ValueError):
                    w.validate_config(cfg)

    def test_private_key_and_password_uri_rejected(self):
        cfg = example()
        cfg["nodes"][0]["identity_file"] = "-----BEGIN OPENSSH PRIVATE KEY-----"
        with self.assertRaises(ValueError):
            w.validate_config(cfg)
        cfg = example()
        cfg["services"][0]["argv"].append("https://user:pass@example.invalid")
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_shell_string_and_broad_kill_rejected(self):
        for argv in (["bash", "-lc", "vllm serve model"], ["bash", "-xec", "pkill -f vllm"],
                     ["env", "bash", "-c", "pkill -f vllm"], ["pkill", "-f", "vllm"]):
            with self.subTest(argv=argv):
                cfg = example()
                cfg["services"][0]["argv"] = argv
                with self.assertRaises(ValueError):
                    w.validate_config(cfg)

    def test_additional_shell_and_credential_argv_bypasses_rejected(self):
        for argv in (["/bin/dash", "-c", "pkill -f vllm"],
                     ["/bin/busybox", "sh", "-c", "pkill -f vllm"],
                     ["/usr/bin/java", "-jar", "/opt/deploy/unverified.jar"],
                     ["/usr/bin/awk", "-f", "/tmp/unverified.awk"],
                     ["/usr/bin/pypy3", "-c", "print('unverified')"],
                     ["/tmp/python-malicious", "/opt/deploy/start_decode_a.sh"],
                     ["pythonXYZ", "/opt/deploy/start_decode_a.sh"],
                     ["bash", "/opt/deploy/start_decode_a.sh", "--api-key=do-not-store"],
                     ["bash", "/opt/deploy/start_decode_a.sh", "--auth-token", "do-not-store"]):
            cfg = example()
            cfg["services"][0]["argv"] = argv
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                w.validate_config(cfg)

    def test_profile_role_template_and_artifact_binding_fail_closed(self):
        cfg = example()
        cfg.pop("profile")
        with self.assertRaisesRegex(ValueError, "必须提供 profile"):
            w.validate_config(cfg)
        cfg = example()
        cfg["profile"]["software"] = {}
        with self.assertRaisesRegex(ValueError, "完整声明"):
            w.validate_config(cfg)
        cfg = example()
        cfg["services"][2]["role"] = "decode"
        with self.assertRaisesRegex(ValueError, "role=decode|缺少同角色"):
            w.validate_config(cfg)
        cfg = example()
        cfg["nodes"][0]["python"] = "{{max_num_batched_tokens}}"
        with self.assertRaisesRegex(ValueError, "模板只能"):
            w.validate_config(cfg)

    def test_device_overlap_uses_execution_namespace_not_logical_node_name(self):
        cfg = example()
        first, second = cfg["nodes"][0], cfg["nodes"][1]
        second["ssh"] = first["ssh"]
        second["container"] = first["container"]
        with self.assertRaisesRegex(ValueError, "namespace"):
            w.validate_config(cfg)

    def test_literal_json_braces_in_regex_are_not_templates(self):
        cfg = example()
        cfg["tests"][0]["assertions"]["must_match"].append(r'\{"status"')
        w.validate_config(cfg)
        cfg = example()
        cfg["services"][0]["artifacts"][0]["path"] = "/opt/deploy/unrelated.sh"
        with self.assertRaisesRegex(ValueError, "实际入口"):
            w.validate_config(cfg)

    def test_health_probe_entrypoint_is_artifact_bound(self):
        cfg = example()
        cfg["services"][0]["health"].pop("artifacts")
        with self.assertRaisesRegex(ValueError, "health.artifacts"):
            w.validate_config(cfg)
        cfg = example()
        cfg["services"][0]["health"]["argv"] = ["bash", "/opt/deploy/unverified_health.sh"]
        with self.assertRaisesRegex(ValueError, "健康探针实际入口"):
            w.validate_config(cfg)

    def test_same_remote_artifact_path_cannot_claim_conflicting_digests(self):
        cfg = example()
        service_artifact = cfg["services"][0]["artifacts"][0]
        cfg["services"][0]["health"]["artifacts"][0] = {
            "path": service_artifact["path"], "sha256": "f" * 64}
        with self.assertRaisesRegex(ValueError, "不同 sha256"):
            w.validate_config(cfg)

    def test_node_python_must_be_a_bounded_python3_command(self):
        for value in ("/bin/bash", "not-python", "python3\n/bin/sh", "x" * 4097,
                      "relative/path/python3"):
            cfg = example()
            cfg["nodes"][0]["python"] = value
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                w.validate_config(cfg)

    def test_identity_file_must_be_absolute_and_plan_uses_canonical_path(self):
        cfg = example()
        cfg["nodes"][0]["identity_file"] = "relative/key"
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            w.validate_config(cfg)
        cfg = example()
        planned = w.plan(cfg)
        self.assertEqual(planned["trials"][0]["nodes"][0]["identity_file"],
                         str(Path(cfg["nodes"][0]["identity_file"]).resolve()))

    def test_unused_tuning_parameter_and_weak_assertions_rejected(self):
        cfg = example()
        for row in [cfg["tuning"]["baseline"], *cfg["tuning"]["candidates"]]:
            row["unused"] = 1
        with self.assertRaisesRegex(ValueError, "没有用于"):
            w.validate_config(cfg)
        cfg = example()
        cfg["tests"][0]["assertions"] = {"exit_code": 0, "must_match": ["done"]}
        with self.assertRaisesRegex(ValueError, "失败拒绝"):
            w.validate_config(cfg)
        cfg = example()
        cfg["tests"][0]["assertions"] = {
            "exit_code": 0, "must_match": [""], "must_not_match": ["(?!)"]}
        with self.assertRaises(ValueError):
            w.validate_config(cfg)

    def test_tuning_parameter_used_only_in_log_path_is_rejected(self):
        cfg = example()
        for service in cfg["services"]:
            for key, value in service.get("env", {}).items():
                if value == "{{max_num_batched_tokens}}":
                    service["env"][key] = "16384"
        cfg["services"][0]["log_path"] = "/opt/deploy/logs/{{max_num_batched_tokens}}.log"
        with self.assertRaisesRegex(ValueError, "没有用于"):
            w.validate_config(cfg)

    def test_sensitive_http_headers_are_rejected_from_argv(self):
        for header in ("X-API-Key: do-not-store", "Proxy-Authorization: Basic abc",
                       "X-Auth-Token: do-not-store", "Cookie: session=do-not-store",
                       "-HAuthorization: Bearer do-not-store",
                       "--header=Authorization: Bearer do-not-store",
                       "--proxy-header=Proxy-Authorization: Basic abc"):
            with self.subTest(header=header), self.assertRaisesRegex(ValueError, "凭据"):
                w._safe_user_argv(["/usr/bin/curl", "-H", header, "https://example.invalid"],
                                  "health.argv", set())

    def test_curl_health_probe_rejects_file_io_and_unbounded_options(self):
        for extra in (["-K/path"], ["-H", "@/tmp/headers"], ["--data", "@/tmp/body"],
                      ["--output", "/tmp/overwrite"], ["--upload-file", "/tmp/data"]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                w._safe_user_argv(["/usr/bin/curl", "-q", *extra, "https://example.invalid/health"],
                                  "services.demo.health.argv", set())
        with self.assertRaisesRegex(ValueError, "4xx/5xx"):
            w._safe_user_argv(["/usr/bin/curl", "-q", "-sS",
                               "https://example.invalid/health"],
                              "services.demo.health.argv", set())
        for url in ("https://example.invalid/health?access_token=do-not-store",
                    "https://example.invalid/health#api_key=do-not-store"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "query/fragment"):
                w._safe_user_argv(["/usr/bin/curl", "-q", "-fsS", url],
                                  "services.demo.health.argv", set())

    def test_loader_interpreter_and_path_environment_injection_is_rejected(self):
        for key in ("LD_PRELOAD", "BASH_ENV", "PYTHONPATH", "PYTHONWARNINGS",
                    "PYTHONUSERBASE", "HOME", "ZDOTDIR", "XDG_CONFIG_HOME",
                    "SHELLOPTS", "BASHOPTS", "PS4", "PATH"):
            cfg = example()
            cfg["services"][0]["env"][key] = "/tmp/unverified"
            with self.subTest(scope="service", key=key), self.assertRaisesRegex(ValueError, "注入"):
                w.validate_config(cfg)
            cfg = example()
            cfg["tests"][0]["env"] = {key: "/tmp/unverified"}
            with self.subTest(scope="test", key=key), self.assertRaisesRegex(ValueError, "注入"):
                w.validate_config(cfg)

    def test_writable_and_protected_paths_cannot_collide(self):
        cfg = example()
        cfg["services"][0]["log_path"] = cfg["services"][0]["pid_file"]
        with self.assertRaisesRegex(ValueError, "不得冲突"):
            w.validate_config(cfg)
        cfg = example()
        cfg["services"][0]["log_path"] = "/opt/deploy/run/tmp/../decode_a.pid.json"
        with self.assertRaisesRegex(ValueError, "规范绝对路径"):
            w.validate_config(cfg)

    def test_candidate_control_char_is_rejected_before_rendered_execution(self):
        cfg = example()
        cfg["tuning"]["candidates"][1]["max_num_batched_tokens"] = "\x00"
        with self.assertRaisesRegex(ValueError, "控制字符"):
            w.validate_config(cfg)
        cfg = example()
        cfg["tests"][0]["log_path"] = cfg["services"][2]["artifacts"][0]["path"]
        with self.assertRaisesRegex(ValueError, "不得冲突"):
            w.validate_config(cfg)

    def test_interpreter_entrypoint_options_cannot_hide_unverified_script(self):
        for argv in (["/usr/bin/python3", "-X", "/opt/deploy/start_decode_a.sh", "/tmp/unverified.py"],
                     ["/usr/bin/perl", "/tmp/unverified.pl"]):
            cfg = example()
            cfg["services"][0]["argv"] = argv
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                w.validate_config(cfg)

    @unittest.skipIf(os.name == "posix", "仅验证 Windows 控制端的 local 前置拒绝")
    def test_windows_cannot_execute_linux_local_supervisor(self):
        cfg = resolved()
        cfg["nodes"][0]["ssh"] = "local"
        with self.assertRaisesRegex(ValueError, "Linux"):
            w.validate_config(cfg, executable=True)

    def test_parallelism_and_device_overlap_checked(self):
        cfg = example()
        cfg["profile"]["roles"]["prefill"]["tp"] = 4
        with self.assertRaisesRegex(ValueError, "TP×PP"):
            w.validate_config(cfg)
        cfg = example()
        cfg["profile"]["roles"]["decode"]["instances"][0]["participants"][0]["node"] = "prefill-a"
        with self.assertRaisesRegex(ValueError, "重复"):
            w.validate_config(cfg)

    def test_feature_conflict_and_hdk_gate(self):
        cfg = example()
        cfg["profile"]["roles"]["decode"]["features"]["fused_mc2"] = True
        with self.assertRaisesRegex(ValueError, "冲突"):
            w.validate_config(cfg)
        cfg = example()
        cfg["profile"]["roles"]["decode"]["features"]["kv_pool"] = True
        cfg["profile"]["software"]["hdk"] = "25.0"
        with self.assertRaisesRegex(ValueError, "最低版本"):
            w.validate_config(cfg)

    def test_pinned_parameter_cannot_change(self):
        cfg = example()
        cfg["tuning"]["candidates"][1]["gpu_memory_utilization"] = .90
        cfg["tuning"]["baseline"]["gpu_memory_utilization"] = .92
        cfg["tuning"]["candidates"][0]["gpu_memory_utilization"] = .92
        with self.assertRaisesRegex(ValueError, "pinned"):
            w.validate_config(cfg)

    def test_tuning_baseline_is_first(self):
        cfg = example()
        cfg["tuning"]["candidates"].reverse()
        self.assertEqual(w.tuning_candidates(cfg)[0], cfg["tuning"]["baseline"])

    def test_tuning_scalar_equality_preserves_bool_int_and_float_types(self):
        cfg = {"tuning": {"max_trials": 2, "baseline": {"x": 1}, "pinned": {},
                          "candidates": [{"x": True}], "fatal_patterns": [],
                          "objective": {}}}
        self.assertEqual(w.tuning_candidates(cfg), [{"x": 1}, {"x": True}])
        cfg["tuning"]["pinned"] = {"x": 1}
        with self.assertRaisesRegex(ValueError, "pinned"):
            w.tuning_candidates(cfg)

    def test_parameter_product_is_bounded_before_materialization(self):
        cfg = example()
        cfg["tuning"].pop("candidates")
        cfg["tuning"]["parameters"] = {f"p{index}": [0, 1] for index in range(16)}
        with self.assertRaisesRegex(ValueError, "笛卡尔积"):
            w.tuning_candidates(cfg)

    def test_windows_controller_command_line_budget_fails_before_execution(self):
        cfg = resolved()
        w._validate_controller_command_lengths(cfg)
        cfg["services"][0]["env"]["LONG_BUT_FIELD_VALID"] = "x" * 20000
        with self.assertRaisesRegex(ValueError, "Windows.*上限"):
            w._validate_controller_command_lengths(cfg)

    def test_dependency_cycle_rejected(self):
        cfg = example()
        cfg["services"][0]["depends_on"] = ["proxy"]
        with self.assertRaisesRegex(ValueError, "环"):
            w.validate_config(cfg)


class PlanAndOwnershipTests(unittest.TestCase):
    def test_plan_is_deterministic_and_hash_changes_with_config(self):
        cfg = example()
        first = w.plan(cfg)
        second = w.plan(copy.deepcopy(cfg))
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["workflow_impl"]["service_workflow.py"],
                         hashlib.sha256(Path(w.__file__).read_bytes()).hexdigest())
        self.assertEqual(first["workflow_impl"]["preflight.py"], hashlib.sha256(
            Path(w.__file__).with_name("preflight.py").read_bytes()).hexdigest())
        cfg["profile"]["software"]["vllm"] = "another-version"
        self.assertNotEqual(first["plan_sha256"], w.plan(cfg)["plan_sha256"])

    def test_plan_hash_binds_workflow_implementation(self):
        cfg = example()
        with patch.object(w, "workflow_impl_sha256", return_value="a" * 64):
            first = w.plan(cfg)
        with patch.object(w, "workflow_impl_sha256", return_value="b" * 64):
            second = w.plan(cfg)
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])

    def test_plan_hash_changes_when_preflight_gate_code_changes(self):
        cfg = example()
        first = w.plan(cfg)
        original_read = Path.read_bytes
        def changed_preflight(path):
            raw = original_read(path)
            return raw + b"\n# changed gate implementation\n" if path.name == "preflight.py" else raw
        with patch.object(Path, "read_bytes", changed_preflight):
            second = w.plan(cfg)
        self.assertNotEqual(first["workflow_impl"]["preflight.py"],
                            second["workflow_impl"]["preflight.py"])
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])

    def test_supervisor_state_binds_complete_service_spec(self):
        cfg = resolved()
        node = w._node_map(cfg)[cfg["services"][0]["node"]]
        service = cfg["services"][0]
        first = w.supervisor_payload(cfg, node, service, "run-1")["spec_sha256"]
        changed = copy.deepcopy(service)
        changed["env"]["NEW_SETTING"] = "1"
        second = w.supervisor_payload(cfg, node, changed, "run-2")["spec_sha256"]
        self.assertNotEqual(first, second)

    def test_execute_requires_non_example_and_matching_plan_hash(self):
        cfg = example()
        args = SimpleNamespace(execute=True, approve=w.plan(cfg)["plan_sha256"], confirm_deployment=None)
        with self.assertRaisesRegex(ValueError, "示例"):
            w._confirm(args, cfg)
        cfg = resolved()
        for node in cfg["nodes"]:
            node["identity_file"] = sys.executable
        args = SimpleNamespace(execute=True, approve="0" * 64, confirm_deployment=None)
        with self.assertRaisesRegex(ValueError, "plan_sha256"):
            w._confirm(args, cfg)

    def test_failed_preflight_gate_never_blocks_exact_stop_or_status(self):
        cfg = resolved()
        for node in cfg["nodes"]:
            node["identity_file"] = sys.executable
        failed_plan = {"plan_sha256": "a" * 64,
                       "preflight": {"required": True, "status": "FAIL"}}
        for command in ("stop", "status"):
            args = SimpleNamespace(command=command, execute=True, approve="a" * 64,
                                   confirm_deployment=None)
            with patch.object(w, "plan", return_value=failed_plan):
                w._confirm(args, cfg)
        args = SimpleNamespace(command="launch", execute=True, approve="a" * 64,
                               confirm_deployment=None)
        with patch.object(w, "plan", return_value=failed_plan), \
                self.assertRaisesRegex(ValueError, "通信预检"):
            w._confirm(args, cfg)

    def test_missing_preflight_report_still_allows_fresh_stop_plan(self):
        cfg = resolved()
        for node in cfg["nodes"]:
            node["identity_file"] = sys.executable
        with tempfile.TemporaryDirectory() as directory:
            cfg["preflight_gate"] = {"report": str(Path(directory) / "missing-preflight.json"),
                                     "scope": "primitives", "max_age_s": 3600, "required": True}
            current = w.plan(cfg)
            self.assertEqual(current["preflight"]["status"], "UNAVAILABLE")
            stop = SimpleNamespace(command="stop", execute=True, approve=current["plan_sha256"],
                                   confirm_deployment=None)
            w._confirm(stop, cfg)
            launch = SimpleNamespace(command="launch", execute=True, approve=current["plan_sha256"],
                                     confirm_deployment=None)
            with self.assertRaisesRegex(ValueError, "通信预检"):
                w._confirm(launch, cfg)

    def test_remote_command_uses_key_and_existing_container(self):
        node = example()["nodes"][0]
        argv = w.context_argv(node, ["python3", "-V"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn(str(Path(node["identity_file"]).resolve()), argv)
        self.assertIn("docker exec -i", argv[-1])
        self.assertNotIn("--workdir", argv[-1])

    def test_supervisor_and_stopper_are_syntactically_valid_and_scoped(self):
        compile(w.SUPERVISOR_SOURCE, "<supervisor>", "exec")
        compile(w.STOPPER_SOURCE, "<stopper>", "exec")
        compile(w.WORKER_SOURCE, "<worker>", "exec")
        compile(w.LOG_SCAN_SOURCE, "<log-scan>", "exec")
        compile(w.OWNER_CHECK_SOURCE, "<owner-check>", "exec")
        compile(w.ACTIVATE_SOURCE, "<activate>", "exec")
        compile(w.ARTIFACT_VERIFY_SOURCE, "<artifact-verify>", "exec")
        self.assertIn("cmdline_sha256", w.STOPPER_SOURCE)
        self.assertIn("boot_id", w.STOPPER_SOURCE)
        self.assertIn("O_NOFOLLOW", w.WORKER_SOURCE)
        self.assertIn("O_NOFOLLOW", w.SUPERVISOR_SOURCE)
        self.assertNotIn("re.findall", w.LOG_SCAN_SOURCE)
        self.assertGreaterEqual(w.SUPERVISOR_SOURCE.count('if not control["stop"]'), 2)
        self.assertNotIn("SIGKILL", w.STOPPER_SOURCE)
        self.assertNotIn("pkill", w.STOPPER_SOURCE)

    @unittest.skipUnless(os.name == "posix", "进程组和 flock 集成仅 Linux")
    def test_worker_reaps_background_descendants_and_stopper_sees_initializing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            child_pid_path = Path(directory) / "child.pid"
            child_code = ("import pathlib,subprocess; "
                          f"p=subprocess.Popen(['sleep','30']); pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid))")
            marker = w.WORKER_PREFIX + "linux-test "
            payload = {"argv": [sys.executable, "-c", child_code], "timeout_s": 5, "env": {},
                       "cwd": directory, "log_path": None, "log_mode": "w",
                       "backup_existing": False, "result_marker": marker}
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            completed = __import__("subprocess").run(
                [sys.executable, "-u", "-c", w.WORKER_SOURCE, encoded],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            child_pid = int(child_pid_path.read_text())
            for _ in range(40):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                try:
                    if Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z":
                        break
                except OSError:
                    break
                time.sleep(.05)
            else:
                self.fail("worker background descendant survived process-group cleanup")

            log_path = str(Path(directory) / "worker.log")
            marker = w.WORKER_PREFIX + "linux-log-test "
            log_payload = {"argv": [sys.executable, "-c", "print('worker-log-ok')"],
                           "timeout_s": 5, "env": {}, "cwd": directory, "log_path": log_path,
                           "log_mode": "w", "backup_existing": True,
                           "result_marker": marker, "artifacts": []}
            log_encoded = base64.b64encode(json.dumps(log_payload).encode()).decode()
            logged = __import__("subprocess").run(
                [sys.executable, "-u", "-c", w.WORKER_SOURCE, log_encoded],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(logged.returncode, 0, logged.stdout + logged.stderr)
            self.assertIn("worker-log-ok", Path(log_path).read_text())
            self.assertTrue(logged.stdout.splitlines()[-1].startswith(marker))

            fifo_path = str(Path(directory) / "artifact.fifo")
            os.mkfifo(fifo_path)
            fifo_marker = w.WORKER_PREFIX + "linux-fifo-test "
            fifo_payload = {"argv": [sys.executable, "-c", "print('must-not-run')"],
                            "timeout_s": 5, "env": {}, "cwd": directory, "log_path": None,
                            "log_mode": "w", "backup_existing": False,
                            "result_marker": fifo_marker,
                            "artifacts": [{"path": fifo_path, "sha256": "0" * 64}]}
            fifo_encoded = base64.b64encode(json.dumps(fifo_payload).encode()).decode()
            fifo = __import__("subprocess").run(
                [sys.executable, "-u", "-c", w.WORKER_SOURCE, fifo_encoded],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(fifo.returncode, 125, fifo.stdout + fifo.stderr)
            self.assertNotIn("must-not-run", fifo.stdout)

            import fcntl
            pid_path = str(Path(directory) / "service.pid.json")
            lock_handle = open(pid_path + ".lock", "a+", encoding="utf-8")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop_payload = {"pid_file": pid_path, "deployment": "demo", "service": "svc",
                            "timeout_s": 1, "expected_run_id": None}
            stop_encoded = base64.b64encode(json.dumps(stop_payload).encode()).decode()
            stopped = __import__("subprocess").run(
                [sys.executable, "-u", "-c", w.STOPPER_SOURCE, stop_encoded],
                capture_output=True, text=True, timeout=5)
            lock_handle.close()
            self.assertEqual(stopped.returncode, 8, stopped.stdout + stopped.stderr)

    @unittest.skipUnless(os.name == "posix", "supervisor/stopper 集成仅 Linux")
    def test_supervisor_and_stopper_reap_owned_process_group(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            pid_path = str(Path(directory) / "service.pid.json")
            log_path = str(Path(directory) / "service.log")
            foreground = Path(directory) / "foreground.sh"
            foreground.write_text("#!/bin/sh\nsleep 0.2\nexec sleep 30\n", encoding="utf-8")
            payload = {"pid_file": pid_path, "log_path": log_path, "deployment": "demo",
                       "service": "svc", "run_id": "run-1", "argv": ["/bin/sh", str(foreground)],
                       "env": {}, "cwd": directory, "backup_existing_log": True, "artifacts": [],
                       "activation_path": pid_path + ".activate.test",
                       "activation_token": "run-1", "activation_timeout_s": 5,
                       "artifact_verifier_source": w.ARTIFACT_VERIFY_SOURCE,
                       "spec_sha256": "0" * 64}
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            supervisor = subprocess.Popen([sys.executable, "-u", "-c", w.SUPERVISOR_SOURCE, encoded])
            state = None
            try:
                for _ in range(100):
                    try:
                        state = json.loads(Path(pid_path).read_text())
                    except (OSError, ValueError):
                        state = None
                    if state and state.get("phase") == "AWAITING_ACTIVATION":
                        break
                    time.sleep(.05)
                self.assertIsNotNone(state)
                self.assertEqual(state.get("phase"), "AWAITING_ACTIVATION")
                Path(payload["activation_path"]).write_text("run-1", encoding="utf-8")
                for _ in range(100):
                    try:
                        state = json.loads(Path(pid_path).read_text())
                    except (OSError, ValueError):
                        state = None
                    if state and state.get("phase") == "RUNNING":
                        break
                    time.sleep(.05)
                self.assertEqual(state.get("phase"), "RUNNING")
                time.sleep(.5)
                owner_payload = {"pid_file": pid_path, "deployment": "demo", "service": "svc",
                                 "spec_sha256": "0" * 64}
                owner_encoded = base64.b64encode(json.dumps(owner_payload).encode()).decode()
                checked = subprocess.run(
                    [sys.executable, "-u", "-c", w.OWNER_CHECK_SOURCE, owner_encoded],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                marker = next(line for line in checked.stdout.splitlines()
                              if line.startswith(w.OWNER_PREFIX))
                owner = json.loads(base64.b64decode(marker[len(w.OWNER_PREFIX):]))
                self.assertEqual(owner["state"], "RUNNING", owner)
                child_pgid = state["child_pid"]
                stop_payload = {"pid_file": pid_path, "deployment": "demo", "service": "svc",
                                "timeout_s": 5, "expected_run_id": "run-1"}
                stop_encoded = base64.b64encode(json.dumps(stop_payload).encode()).decode()
                stopped = subprocess.run([sys.executable, "-u", "-c", w.STOPPER_SOURCE, stop_encoded],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
                supervisor.wait(timeout=5)
                self.assertFalse(Path(pid_path).exists())
                active = False
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        raw = (entry / "stat").read_text()
                        tail = raw[raw.rfind(")") + 2:].split()
                        active |= len(tail) >= 3 and tail[0] != "Z" and int(tail[2]) == child_pgid
                    except (OSError, ValueError):
                        pass
                self.assertFalse(active, "owned process group survived supervisor/stopper cleanup")
            finally:
                if supervisor.poll() is None:
                    supervisor.terminate()
                    try:
                        supervisor.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        supervisor.kill()
                if state and isinstance(state.get("child_pid"), int):
                    try:
                        raw = Path(f"/proc/{state['child_pid']}/stat").read_text()
                        current = raw[raw.rfind(")") + 2:].split()[19]
                        if current == state.get("child_start_time"):
                            os.killpg(state["child_pid"], 9)
                    except (OSError, IndexError):
                        pass

    def test_report_does_not_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            w._write_report(path, {"status": "PASS"})
            with self.assertRaisesRegex(ValueError, "已存在"):
                w._write_report(path, {"status": "PASS"})
            with self.assertRaisesRegex(ValueError, "普通文件"):
                w._reserve_report(Path(directory), force=True)

    def test_cli_interrupt_releases_report_reservation(self):
        cfg = resolved()
        for node in cfg["nodes"]:
            node["identity_file"] = sys.executable
        approve = w.plan(cfg)["plan_sha256"]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            report_path = Path(directory) / "launch.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            argv = ["launch", "--config", str(config_path), "--out", str(report_path),
                    "--execute", "--approve", approve]
            with patch.object(w, "launch_services", side_effect=KeyboardInterrupt()), \
                    self.assertRaises(KeyboardInterrupt):
                w.main(argv)
            self.assertFalse(Path(str(report_path) + ".lock").exists())
            self.assertEqual(json.loads(report_path.read_text())["status"], "ERROR")

    def test_cli_report_cannot_overwrite_config_before_remote_action(self):
        cfg = resolved()
        for node in cfg["nodes"]:
            node["identity_file"] = sys.executable
        approve = w.plan(cfg)["plan_sha256"]
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            original = json.dumps(cfg)
            config_path.write_text(original, encoding="utf-8")
            argv = ["launch", "--config", str(config_path), "--out", str(config_path),
                    "--force", "--execute", "--approve", approve]
            with patch.object(w, "launch_services") as launched, \
                    patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
                w.main(argv)
            launched.assert_not_called()
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_local_transport_is_reaped_on_interrupt(self):
        proc = SimpleNamespace(communicate=lambda timeout: (_ for _ in ()).throw(KeyboardInterrupt()))
        with patch.object(w.subprocess, "Popen", return_value=proc), \
                patch.object(w, "_stop_owned", return_value=True) as stopped, \
                self.assertRaises(KeyboardInterrupt):
            w._run_local(["ssh", "host"], 10)
        stopped.assert_called_once_with(proc, timeout_s=1)

    def test_status_requires_explicit_execute_and_plan_approval(self):
        with patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            w.main(["status", "--config", str(EXAMPLE), "--out", "unused.json"])

    def test_remote_result_marker_is_nonce_bound_and_must_be_last(self):
        payload = {"rc": 0, "timed_out": False, "elapsed_s": .1,
                   "output_bytes": 5, "output_truncated": False}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        local = {"rc": 0, "text": "hello\n" + w.WORKER_PREFIX + "fixed " + encoded + "\n",
                 "elapsed_s": .1, "timed_out": False}
        node = {"name": "local", "ssh": "local", "python": "python3"}
        with patch.object(w.secrets, "token_hex", return_value="fixed"), patch.object(
                w, "_run_local", return_value=local):
            result = w.RemoteExecutor().run(node, ["true"], 1)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["text"], "hello\n")
        local["text"] += "trailer\n"
        with patch.object(w.secrets, "token_hex", return_value="fixed"), patch.object(
                w, "_run_local", return_value=local):
            result = w.RemoteExecutor().run(node, ["true"], 1)
        self.assertEqual(result["rc"], 125)
        self.assertIn("transport_error", result)


class WorkflowTests(unittest.TestCase):
    def test_launch_uses_dependency_waves_and_all_services_become_healthy(self):
        cfg = resolved()
        fake = FakeExecutor()
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "PASS")
        self.assertLess(fake.detached.index("decode-a"), fake.detached.index("prefill-a"))
        self.assertLess(fake.detached.index("prefill-a"), fake.detached.index("proxy"))
        self.assertTrue(all(x["status"] == "HEALTHY" for x in report["services"].values()))

    def test_same_wave_is_fully_activated_before_readiness_waits(self):
        cfg = resolved()
        class ObserveWave(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.activated = []
                self.early_activation = False
                self.early_ready_probe = False
            def activate(self, node, service, run_id, total_timeout_s=None):
                if (service["name"].startswith("decode-") and
                        not {"decode-a", "decode-b"} <= set(self.owners)):
                    self.early_activation = True
                self.activated.append(service["name"])
                return super().activate(node, service, run_id, total_timeout_s)
            def run(self, node, argv, timeout_s, **kwargs):
                if (argv and Path(argv[0]).name == "curl" and node["name"].startswith("decode-") and
                        self.key(node, argv) in self.up and
                        not {"decode-a", "decode-b"} <= set(self.activated)):
                    self.early_ready_probe = True
                return super().run(node, argv, timeout_s, **kwargs)
        fake = ObserveWave()
        self.assertEqual(w.launch_services(cfg, fake)["status"], "PASS")
        self.assertFalse(fake.early_activation)
        self.assertFalse(fake.early_ready_probe)

    def test_launch_final_sweep_catches_earlier_wave_drift(self):
        cfg = resolved()
        class DriftAfterProxy(FakeExecutor):
            def detach(self, cfg, node, service, run_id, total_timeout_s=None):
                result = super().detach(cfg, node, service, run_id, total_timeout_s)
                if service["name"] == "proxy":
                    old = w._service_map(cfg)["decode-a"]
                    old_node = w._node_map(cfg)[old["node"]]
                    self.owners.pop("decode-a", None)
                    self.up.discard(self.key(old_node, old["health"]["argv"]))
                return result
        fake = DriftAfterProxy()
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("decode-a", report["final_status_drift"])
        self.assertIn("rollback", report)

    def test_e2e_requires_all_cases_zero_failures_and_post_health(self):
        cfg = resolved()
        fake = FakeExecutor()
        mark_running(fake, cfg)
        report = w.run_tests(cfg, executor=fake)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["tests"]["e2e-prefix-matrix"]["metrics"]["output_throughput"], 1004.0)

        bad = FakeExecutor(successful_output().replace("Failed Requests | 0", "Failed Requests | 1", 1))
        bad.up = set(fake.up)
        bad.owners = dict(fake.owners)
        self.assertEqual(w.run_tests(cfg, executor=bad)["status"], "FAIL")

        missing = FakeExecutor(successful_output().replace("全量数据集测试完成\n", "", 1))
        missing.up = set(fake.up)
        missing.owners = dict(fake.owners)
        self.assertEqual(w.run_tests(cfg, executor=missing)["status"], "FAIL")

    def test_test_run_rejects_wrong_or_drifting_run_id(self):
        cfg = resolved()

        class ReplaceDuringTest(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.test_runs = 0

            def run(self, node, argv, timeout_s, **kwargs):
                result = super().run(node, argv, timeout_s, **kwargs)
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    self.test_runs += 1
                    for name in list(self.owners):
                        self.owners[name] = "replacement-run"
                return result

        wrong = ReplaceDuringTest()
        mark_running(wrong, cfg, run_id="existing-run")
        rejected = w.run_tests(cfg, executor=wrong, expected_run_id="expected-run")
        self.assertEqual(rejected["status"], "FAIL")
        self.assertEqual(wrong.test_runs, 0)
        self.assertIn("run_id_mismatch", rejected)

        drifting = ReplaceDuringTest()
        mark_running(drifting, cfg, run_id="expected-run")
        report = w.run_tests(cfg, executor=drifting, expected_run_id="expected-run")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(drifting.test_runs, 1)
        self.assertEqual(set(report["ownership_drift"]),
                         {service["name"] for service in cfg["services"]})

    def test_stop_signals_entire_wave_before_health_probes(self):
        cfg = resolved()

        class ObserveStop(FakeExecutor):
            def __init__(self, config):
                super().__init__()
                self.events = []
                self.health_services = {tuple(service["health"]["argv"]): service["name"]
                                        for service in config["services"]}

            def stop(self, cfg, node, service, expected_run_id=None):
                self.events.append("stop:" + service["name"])
                return super().stop(cfg, node, service, expected_run_id)

            def run(self, node, argv, timeout_s, **kwargs):
                if argv and Path(argv[0]).name == "curl":
                    self.events.append("health:" + self.health_services[tuple(argv)])
                return super().run(node, argv, timeout_s, **kwargs)

        fake = ObserveStop(cfg)
        mark_running(fake, cfg)
        report = w.stop_services(cfg, executor=fake)
        self.assertEqual(report["status"], "PASS")
        for wave in w.service_waves(cfg["services"]):
            stop_positions = [fake.events.index("stop:" + name) for name in wave]
            health_positions = [index for index, event in enumerate(fake.events)
                                if event in {"health:" + name for name in wave}]
            self.assertLess(max(stop_positions), min(health_positions), wave)

    def test_truncated_output_cannot_pass(self):
        test = resolved()["tests"][0]
        result = {"rc": 0, "text": successful_output(), "output_truncated": True, "output_bytes": 999}
        self.assertEqual(w.evaluate_test(test, result)["status"], "FAIL")

    def test_timeout_transport_and_missing_metric_cannot_pass(self):
        test = resolved()["tests"][0]
        for change in ({"timed_out": True}, {"transport_error": "lost"},
                       {"text": successful_output().replace("Output Token Throughput", "Throughput")}):
            result = {"rc": 0, "text": successful_output(), "timed_out": False,
                      "output_truncated": False, "output_bytes": 999}
            result.update(change)
            with self.subTest(change=change):
                self.assertEqual(w.evaluate_test(test, result)["status"], "FAIL")

    def test_worker_error_is_fatal_control_failure_for_testing_and_tuning(self):
        cfg = resolved()
        fake = FakeExecutor()
        mark_running(fake, cfg)
        class WorkerFailure(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                result = super().run(node, argv, timeout_s, **kwargs)
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    result.update(rc=125, worker_error="entrypoint disappeared")
                return result
        broken = WorkerFailure()
        broken.up = set(fake.up)
        broken.owners = dict(fake.owners)
        report = w.run_tests(cfg, executor=broken)
        test = report["tests"]["e2e-prefix-matrix"]
        self.assertEqual(test["status"], "FAIL")
        self.assertTrue(test["fatal_for_tuning"])
        self.assertEqual(test["reason_category"], "TEST_CONTROL_INCOMPLETE")

    def test_catastrophic_regex_has_a_killable_budget(self):
        started = time.monotonic()
        evaluated = w._bounded_regex("a" * 30000 + "!", [
            {"id": 0, "kind": "search", "pattern": "(a+)+$"}], timeout_s=.2)
        self.assertFalse(evaluated["complete"])
        self.assertLess(time.monotonic() - started, 5)

    def test_regex_operations_are_passed_by_file_not_command_line(self):
        operations = [{"id": index, "kind": "search", "pattern": "a" * 1000}
                      for index in range(64)]
        payload = {"results": [{"id": item["id"], "kind": "search", "matched": False}
                               for item in operations]}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        local = {"rc": 0, "text": w.REGEX_PREFIX + encoded + "\n", "elapsed_s": .01,
                 "timed_out": False}
        with patch.object(w, "_run_local", return_value=local) as run:
            evaluated = w._bounded_regex("input", operations)
        self.assertTrue(evaluated["complete"])
        argv = run.call_args.args[0]
        self.assertLess(max(len(str(item)) for item in argv), 5000)

    def test_health_regex_shares_the_remaining_readiness_budget(self):
        cfg = resolved()
        service = cfg["services"][0]
        service["health"]["required_regex"] = "(a+)+$"
        node = w._node_map(cfg)[service["node"]]
        class LargeHealth(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                if argv and Path(argv[0]).name == "curl":
                    text = "a" * 30000 + "!"
                    return {"rc": 0, "text": text, "elapsed_s": .01, "timed_out": False,
                            "output_bytes": len(text), "output_truncated": False}
                return super().run(node, argv, timeout_s, **kwargs)
        started = time.monotonic()
        health = w.health_once(LargeHealth(), node, service, total_timeout_s=.2)
        self.assertEqual(health["status"], "FAIL")
        self.assertLess(time.monotonic() - started, 3)

    def test_non_finite_or_overflowed_metric_cannot_pass(self):
        test = resolved()["tests"][0]
        test["metrics"]["output_throughput"]["pattern"] = r"Output Token Throughput[^|]*\|\s*(\S+)"
        for value in ("NaN", "1e308"):
            output = successful_output()
            output = re.sub(r"Output Token Throughput \| [0-9.]+",
                            f"Output Token Throughput | {value}", output)
            result = {"rc": 0, "text": output, "timed_out": False,
                      "output_truncated": False, "output_bytes": len(output)}
            with self.subTest(value=value):
                self.assertEqual(w.evaluate_test(test, result)["status"], "FAIL")

    def test_transport_failure_is_not_down_evidence(self):
        cfg = resolved()
        service = cfg["services"][0]
        class BrokenTransport(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                if argv and Path(argv[0]).name == "curl":
                    return {"rc": 125, "text": "", "timed_out": False,
                            "transport_error": "lost", "elapsed_s": .01}
                return super().run(node, argv, timeout_s, **kwargs)
        check = w.health_once(BrokenTransport(), w._node_map(cfg)[service["node"]], service)
        self.assertFalse(check["down_evidence"])

    def test_worker_control_failures_are_not_down_evidence_and_125_is_reserved(self):
        cfg = resolved()
        service = cfg["services"][0]
        node = w._node_map(cfg)[service["node"]]
        for field in ("worker_error", "cleanup_error", "output_truncated"):
            class BrokenWorker(FakeExecutor):
                def run(self, node, argv, timeout_s, **kwargs):
                    result = super().run(node, argv, timeout_s, **kwargs)
                    if argv and Path(argv[0]).name == "curl":
                        result.update(rc=7, **{field: True})
                    return result
            with self.subTest(field=field):
                self.assertFalse(w.health_once(BrokenWorker(), node, service)["down_evidence"])
        invalid = example()
        invalid["services"][0]["health"]["down_exit_codes"] = [125]
        with self.assertRaises(ValueError):
            w.validate_config(invalid)

    def test_ownership_publication_uses_shared_ready_deadline_beyond_five_seconds(self):
        cfg = resolved()
        service = cfg["services"][0]
        node = w._node_map(cfg)[service["node"]]
        clock = iter([float(value) for value in range(21)])
        states = ([{"state": "ABSENT"}] * 6 +
                  [{"state": "RUNNING", "run_id": "slow-run"}])
        with patch.object(w.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(w.time, "sleep"), \
                patch.object(w, "ownership_status", side_effect=states):
            owner = w.wait_run_ownership(cfg, object(), node, service, "slow-run", deadline=20.0)
        self.assertEqual(owner["state"], "RUNNING")

    def test_foreign_run_owner_fails_without_waiting_full_ready_timeout(self):
        cfg = resolved()
        service = cfg["services"][0]
        node = w._node_map(cfg)[service["node"]]
        foreign = {"state": "AWAITING_ACTIVATION", "run_id": "other-controller"}
        started = time.monotonic()
        with patch.object(w, "ownership_status", return_value=foreign) as checked, \
                patch.object(w.time, "sleep"):
            owner = w.wait_run_ownership(cfg, object(), node, service, "this-controller",
                                         deadline=time.monotonic() + 7200)
        self.assertEqual(owner, foreign)
        self.assertEqual(checked.call_count, 1)
        self.assertLess(time.monotonic() - started, 1)

    def test_readiness_treats_activation_phase_as_transitional(self):
        cfg = resolved()
        service = cfg["services"][0]
        node = w._node_map(cfg)[service["node"]]
        fake = FakeExecutor()
        fake.up.add(fake.key(node, service["health"]["argv"]))
        states = [{"state": "AWAITING_ACTIVATION", "run_id": "run"},
                  {"state": "RUNNING", "run_id": "run"},
                  {"state": "RUNNING", "run_id": "run"}]
        with patch.object(w, "ownership_status", side_effect=states), patch.object(w.time, "sleep"):
            ready = w.wait_ready_owned(cfg, fake, node, service, "run",
                                       deadline=time.monotonic() + 5)
        self.assertEqual(ready["status"], "PASS")

    def test_service_log_fatal_pattern_fails_test(self):
        cfg = resolved()
        fake = FakeExecutor(log_fatal=True)
        mark_running(fake, cfg)
        report = w.run_tests(cfg, executor=fake)
        self.assertEqual(report["service_logs"]["status"], "FAIL")
        self.assertEqual(report["status"], "FAIL")

    def test_test_suite_stops_immediately_after_fatal_result(self):
        cfg = resolved()
        cfg["tuning"] = {"fatal_patterns": ["device page fault"]}
        second = copy.deepcopy(cfg["tests"][0])
        second["name"] = "must-not-run"
        second["log_path"] = "/opt/deploy/results/must-not-run.log"
        cfg["tests"].append(second)
        class FatalFirst(FakeExecutor):
            def __init__(self):
                super().__init__(successful_output() + "device page fault\n")
                self.test_runs = 0
            def run(self, node, argv, timeout_s, **kwargs):
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    self.test_runs += 1
                return super().run(node, argv, timeout_s, **kwargs)
        fake = FatalFirst()
        mark_running(fake, cfg)
        report = w.run_tests(cfg, executor=fake)
        self.assertEqual(fake.test_runs, 1)
        self.assertEqual(report["aborted_on_fatal"], "e2e-prefix-matrix")
        self.assertEqual(report["omitted_due_to_fatal"], ["must-not-run"])

    def test_log_scan_pass_payload_requires_complete_worker_envelope(self):
        cfg = resolved()
        class IncompleteScan(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                result = super().run(node, argv, timeout_s, **kwargs)
                if len(argv) >= 5 and argv[1:4] == ["-u", "-c", w.LOG_SCAN_SOURCE]:
                    result["cleanup_error"] = "scanner group not reaped"
                return result
        scanned = w.scan_service_logs(cfg, IncompleteScan())
        self.assertEqual(scanned["status"], "FAIL")
        self.assertTrue(all(item["fatal"] for item in scanned["services"].values()))

    def test_unhealthy_owned_service_is_not_relaunched(self):
        cfg = resolved()
        fake = FakeExecutor(owner_state="RUNNING")
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(fake.detached, [])

    def test_healthy_external_or_old_spec_service_is_not_launch_success(self):
        cfg = resolved()
        fake = FakeExecutor(owner_state="ABSENT")
        for service in cfg["services"]:
            node = w._node_map(cfg)[service["node"]]
            fake.up.add(fake.key(node, service["health"]["argv"]))
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(fake.detached, [])
        self.assertTrue(all(item["status"] == "FAIL" for item in report["services"].values()))

    def test_health_cannot_hide_non_running_supervisor_phase_or_dead_child(self):
        cfg = resolved()
        for owner_state in ("INITIALIZING_OWNED", "AWAITING_ACTIVATION", "CHILD_NOT_RUNNING"):
            fake = FakeExecutor(owner_state=owner_state)
            for service in cfg["services"]:
                node = w._node_map(cfg)[service["node"]]
                fake.up.add(fake.key(node, service["health"]["argv"]))
            with self.subTest(owner_state=owner_state):
                status = w.status_services(cfg, fake)
                self.assertTrue(all(item["status"] == "FAIL" for item in status.values()))

    def test_empty_rollback_scope_never_stops_all_services(self):
        cfg = resolved()
        fake = FakeExecutor(artifact_ok=False)
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["started_by_this_run"], [])
        self.assertEqual(fake.stopped, [])

    def test_failed_detach_ack_rolls_back_confirmed_run_id_owner(self):
        cfg = resolved()
        class LostAck(FakeExecutor):
            def detach(self, cfg, node, service, run_id, total_timeout_s=None):
                self.detached.append(service["name"])
                self.owners[service["name"]] = run_id
                return {"rc": 255, "text": "connection lost", "elapsed_s": 1,
                        "timed_out": False, "argv": []}
        fake = LostAck()
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["started_by_this_run"], ["decode-a"])
        self.assertEqual(fake.stopped, ["decode-a"])
        self.assertEqual(fake.detached, ["decode-a"])
        self.assertEqual(fake.owners, {})

    def test_failed_activation_is_rolled_back_by_exact_run_id(self):
        cfg = resolved()
        class ActivationLost(FakeExecutor):
            def activate(self, node, service, run_id, total_timeout_s=None):
                return {"rc": 125, "text": "", "elapsed_s": 1, "timed_out": True,
                        "transport_error": "activation ACK lost"}
        fake = ActivationLost()
        report = w.launch_services(cfg, fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(set(fake.stopped), {"decode-a", "decode-b"})
        self.assertEqual(set(fake.detached), {"decode-a", "decode-b"})
        self.assertEqual(fake.owners, {})

    def test_launch_interrupt_after_detach_recovers_current_run_owner(self):
        cfg = resolved()
        class InterruptedDetach(FakeExecutor):
            def detach(self, cfg, node, service, run_id, total_timeout_s=None):
                self.detached.append(service["name"])
                self.owners[service["name"]] = run_id
                self.up.add(self.key(node, service["health"]["argv"]))
                raise KeyboardInterrupt()
        fake = InterruptedDetach()
        with self.assertRaises(KeyboardInterrupt):
            w.launch_services(cfg, fake)
        self.assertEqual(fake.owners, {})
        self.assertEqual(fake.stopped, ["decode-a"])

    def test_subset_test_run_is_partial_not_full_pass(self):
        cfg = resolved()
        second = copy.deepcopy(cfg["tests"][0])
        second["name"] = "second-suite"
        second["log_path"] = "/opt/deploy/results/second.log"
        cfg["tests"].append(second)
        fake = FakeExecutor()
        mark_running(fake, cfg)
        report = w.run_tests(cfg, names=[cfg["tests"][0]["name"]], executor=fake)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["omitted"], ["second-suite"])

    def test_tune_stops_when_baseline_fails(self):
        cfg = example()
        cfg["example"] = False
        fake = FakeExecutor(test_output="Failed Requests | 1\n")
        report = w.tune(cfg, executor=fake)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["trials"]), 1)
        self.assertIn("基线", report["trials"][0]["reason"])

    def test_cleanup_error_is_fatal_and_stops_tuning(self):
        cfg = example()
        cfg["example"] = False
        class CleanupFailure(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                result = super().run(node, argv, timeout_s, **kwargs)
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    result.update(rc=125, cleanup_error="owned test process group could not be reaped")
                return result
        report = w.tune(cfg, executor=CleanupFailure())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["trials"]), 1)
        test_report = report["trials"][0]["tests"]["tests"]["e2e-prefix-matrix"]
        self.assertEqual(test_report["reason_category"], "TEST_CLEANUP_INCOMPLETE")
        self.assertTrue(test_report["fatal_for_tuning"])

    def test_tuning_rescans_logs_after_stop_and_rejects_shutdown_fatal(self):
        cfg = example()
        cfg["example"] = False
        class FatalOnShutdown(FakeExecutor):
            def stop(self, cfg, node, service, expected_run_id=None):
                result = super().stop(cfg, node, service, expected_run_id)
                if expected_run_id is not None:
                    self.log_fatal = True
                return result
        report = w.tune(cfg, executor=FatalOnShutdown())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["trials"]), 1)
        post = report["trials"][0]["post_stop_service_logs"]
        self.assertEqual(post["status"], "FAIL")
        self.assertTrue(any(item["fatal"] for item in post["services"].values()))

    def test_later_fatal_trial_cannot_be_hidden_by_earlier_best(self):
        cfg = example()
        cfg["example"] = False
        class FatalSecondTrial(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.test_runs = 0
            def run(self, node, argv, timeout_s, **kwargs):
                result = super().run(node, argv, timeout_s, **kwargs)
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    self.test_runs += 1
                    if self.test_runs == 2:
                        text = result["text"] + "device page fault\n"
                        result.update(text=text, output_bytes=len(text.encode()))
                return result
        report = w.tune(cfg, executor=FatalSecondTrial())
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(report["aborted"])
        self.assertEqual(report["abort_reason"], "FATAL_OR_CONTROL_INCOMPLETE")
        self.assertEqual(report["best"]["trial"], 1)
        self.assertEqual(len(report["trials"]), 2)

    def test_tuning_interrupt_rolls_back_only_current_run(self):
        cfg = example()
        cfg["example"] = False
        class InterruptedTest(FakeExecutor):
            def run(self, node, argv, timeout_s, **kwargs):
                if argv[:2] == ["bash", "/opt/deploy/test_e2e.sh"]:
                    raise KeyboardInterrupt()
                return super().run(node, argv, timeout_s, **kwargs)
        fake = InterruptedTest()
        with self.assertRaises(KeyboardInterrupt):
            w.tune(cfg, executor=fake)
        self.assertEqual(set(fake.stopped), {service["name"] for service in cfg["services"]})
        self.assertEqual(fake.owners, {})

    def test_select_best_ignores_failed_trial(self):
        objective = {"test": "bench", "metric": "throughput", "direction": "maximize"}
        trials = [
            {"trial": 1, "status": "PASS", "tests": {"tests": {"bench": {"metrics": {"throughput": 10}}}}},
            {"trial": 2, "status": "FAIL", "tests": {"tests": {"bench": {"metrics": {"throughput": 99}}}}},
            {"trial": 3, "status": "PASS", "tests": {"tests": {"bench": {"metrics": {"throughput": 12}}}}}
        ]
        self.assertEqual(w.select_best(trials, objective)["trial"], 3)


if __name__ == "__main__":
    unittest.main()

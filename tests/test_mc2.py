"""MC2 主机侧语义与故障注入；这些用例不代表 NPU 或真实 torch_npu 验收。"""
import copy
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/ascend-multinode-comm/scripts"))
import preflight as p
from test_preflight import cfg, inv


def case(op="matmul_all_reduce", name="dense"):
    return dict(name=name, operator=op, group="model", runner="builtin", shape=[128, 512, 128],
                dtype="float16", execution_mode="eager", repeats=2, rtol=.02, atol=.02,
                support_ref="unit-fixture-only: not hardware support evidence")


class Matrix:
    """极小 CPU 数学替身，仅用于独立验证 golden 的分布/切分语义。"""
    def __init__(self, data, dtype="float32"):
        self.data, self.dtype = data, dtype
        self.shape = (len(data), len(data[0]))

    def float(self):
        return Matrix(self.data)

    def to(self, dtype):
        return Matrix(self.data, dtype)

    def __matmul__(self, other):
        return Matrix([[sum(a*b for a, b in zip(row, col)) for col in zip(*other.data)] for row in self.data])

    def __add__(self, other):
        return Matrix([[a+b for a, b in zip(x, y)] for x, y in zip(self.data, other.data)])

    def __truediv__(self, number):
        return Matrix([[x/number for x in row] for row in self.data])

    def chunk(self, count, dim):
        assert dim == 0
        size = len(self.data)//count
        return [Matrix(self.data[i*size:(i+1)*size]) for i in range(count)]


class Generator:
    def __init__(self, device):
        assert device == "cpu"

    def manual_seed(self, seed):
        self.random = random.Random(seed)
        return self


TORCH = SimpleNamespace(Generator=Generator, float16="float16", bfloat16="bfloat16",
    randint=lambda low, high, shape, generator: Matrix([
        [generator.random.randrange(low, high) for _ in range(shape[1])] for _ in range(shape[0])]),
    cat=lambda tensors: Matrix([row for t in tensors for row in t.data], tensors[0].dtype))


class MC2Tests(unittest.TestCase):
    def config(self, *cases):
        config = cfg()
        config.update(require_mc2=True, mc2_cases=list(cases or [case()]))
        return config

    def test_builtin_and_adapter_config(self):
        p.validate_config(self.config())
        c = case("moe_dispatch_combine", "moe")
        c.update(runner="adapter", node="a", argv=["python3", "/tests/moe.py"], execution_mode="graph")
        p.validate_config(self.config(c))

    def test_invalid_cases_rejected_before_connect(self):
        bads = [dict(operator="alltoall_matmul"), dict(execution_mode="graph"), dict(shape=[3, 512, 128], operator="matmul_reduce_scatter"),
                dict(shape=[128, 16, 128]), dict(dtype="int8"), dict(rtol=float("nan")), dict(atol=float("inf")),
                dict(rtol=1), dict(repeats=1), dict(repeats=True), dict(support_ref="REPLACE"), dict(group="missing"),
                dict(runner="adapter", node="missing", argv=["true"]), dict(comm_mode="aiv"), dict(shape=[4096,4096,4096])]
        for bad in bads:
            with self.subTest(bad=bad), patch.object(p, "call_remote") as remote:
                c = case()
                c.update(bad)
                with self.assertRaises(ValueError):
                    p.run(self.config(c), "check")
                remote.assert_not_called()

    def test_manifest_requires_explicit_opt_in_unique_cases_and_cross_node_group(self):
        configs = [self.config(), self.config(case(), case()), self.config(), self.config()]
        configs[0]["require_mc2"] = False
        configs[2]["groups"][0]["nodes"] = ["a"]
        configs[3]["adapters"] = [{"stage": "mc2"}]
        for config in configs:
            with self.assertRaises(ValueError):
                p.validate_config(config)

    def test_inputs_change_by_rank_and_repeat_but_are_reproducible(self):
        c = dict(shape=[3,4,3], dtype="float16")
        x, w = p.mc2_inputs(TORCH, c, 0, 0)
        self.assertEqual(x.dtype, "float16")
        self.assertEqual(x.data, p.mc2_inputs(TORCH, c, 0, 0)[0].data)
        self.assertNotEqual(x.data, p.mc2_inputs(TORCH, c, 1, 0)[0].data)
        self.assertNotEqual(w.data, p.mc2_inputs(TORCH, c, 0, 1)[1].data)

    def test_reference_allreduce_and_reduce_scatter_rank_slices(self):
        inputs = [(Matrix([[1,2],[3,4]]), Matrix([[1,0],[0,1]])),
                  (Matrix([[5,6],[7,8]]), Matrix([[2,0],[0,3]]))]
        with patch.object(p, "mc2_inputs", side_effect=lambda t,c,r,i: inputs[r]):
            out, _ = p.mc2_reference(TORCH, case(), 0, 2, 0)
            self.assertEqual(out.data, [[11,20],[17,28]])
            for rank, expected in enumerate(([[11,20]], [[17,28]])):
                out, _ = p.mc2_reference(TORCH, case("matmul_reduce_scatter"), rank, 2, 0)
                self.assertEqual(out.data, expected)

    def test_allgather_reference_uses_rank_order_and_local_weight(self):
        inputs = [(Matrix([[1,2]]), Matrix([[1],[2]])), (Matrix([[3,4]]), Matrix([[3],[4]]))]
        with patch.object(p, "mc2_inputs", side_effect=lambda t,c,r,i: inputs[r]):
            out, gathered = p.mc2_reference(TORCH, case("all_gather_matmul"), 1, 2, 0)
            self.assertEqual(gathered.data, [[1,2],[3,4]])
            self.assertEqual(out.data, [[11],[25]])

    def test_only_real_fused_api_is_dispatched_no_collective_fallback(self):
        for op, api in p.MC2_APIS.items():
            calls = SimpleNamespace(**{api: Mock(return_value=("out", "gather") if op == "all_gather_matmul" else "out")})
            c = case(op)
            if op != "matmul_all_reduce":
                c["comm_mode"] = "aiv"
            self.assertEqual(p.mc2_invoke(calls, c, "x", "w", "hcom", 4)[0], "out")
            kwargs = {"reduce_op": "sum"} if op != "all_gather_matmul" else {"gather_output": True}
            args = ("x", "w", "hcom") if op == "matmul_all_reduce" else ("x", "w", "hcom", 4)
            if op != "matmul_all_reduce":
                kwargs["comm_mode"] = "aiv"
            getattr(calls, api).assert_called_once_with(*args, **kwargs)
            with self.assertRaises(AttributeError):
                p.mc2_invoke(SimpleNamespace(), c, "x", "w", "hcom", 4)

    def test_numeric_guard_rejects_shape_dtype_nonfinite_and_mismatch(self):
        expected = Mock(shape=(2,2))
        expected.float.return_value = expected
        actual = Mock(shape=(2,2), dtype="float16")
        converted = actual.detach.return_value.cpu.return_value.float.return_value
        converted.__sub__ = Mock(return_value=Mock(abs=Mock(return_value=Mock(max=Mock(return_value=Mock(item=Mock(return_value=.01)))))))
        torch = Mock()
        torch.isfinite.return_value.all.return_value.item.return_value = True
        torch.allclose.return_value = True
        self.assertEqual(p.mc2_tensor_check(torch, actual, expected, "float16", .02, .02), .01)
        torch.allclose.return_value = False
        with self.assertRaisesRegex(ValueError, "数值"):
            p.mc2_tensor_check(torch, actual, expected, "float16", .02, .02)
        torch.isfinite.return_value.all.return_value.item.return_value = False
        with self.assertRaisesRegex(ValueError, "NaN"):
            p.mc2_tensor_check(torch, actual, expected, "float16", .02, .02)
        for change in ({"shape": (1,2)}, {"dtype": "bfloat16"}):
            with self.assertRaisesRegex(ValueError, "shape/dtype"):
                p.mc2_tensor_check(torch, Mock(shape=change.get("shape", (2,2)), dtype=change.get("dtype", "float16")), expected, "float16", 0, 0)

    def test_multinode_workers_keep_all_ranks_and_cases(self):
        config = self.config()
        config["nodes"].append(dict(name="third", ssh="node-third", devices=[2,5]))
        selected = {n["name"]: dict(ip=f"192.0.2.{i+1}", nic="eth0") for i,n in enumerate(config["nodes"])}
        jobs = []
        def call(node, payload, env):
            jobs.append((node, payload))
            return dict(status="PASS", events=[dict(event="result", action="mc2", result=dict(
                rank=w["rank"], device=w["device"], node=w["node"], operator=w["case"]["operator"],
                case=w["case"]["name"], status="PASS", host_id=node["name"], measurements=[{},{}])) for w in payload["workers"]])
        with patch.object(p, "call_remote", side_effect=call):
            result = p.run_group(config["nodes"], selected, config, "mc2", case=case())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["world_size"], 4)
        self.assertEqual(sorted(w["rank"] for _,j in jobs for w in j["workers"]), [0,1,2,3])
        self.assertTrue(all(w["action"] == "mc2" for _,j in jobs for w in j["workers"]))
        with patch.object(p, "call_remote", return_value=dict(status="PASS", events=[])):
            self.assertEqual(p.run_group(config["nodes"], selected, config, "mc2", case=case())["status"], "FAIL")

    def request_and_evidence(self):
        request = dict(case="moe", operator="moe_dispatch_combine", group="model", execution_mode="eager",
                       repeats=2, ranks=p.mc2_rank_map(cfg()["nodes"]))
        detail = dict(request, versions={"CANN":"fixture"}, parameters={"tokens":16,"hidden":128,"dtype":"fp16"})
        detail["ranks"] = [dict(r, status="PASS", synchronized=True, numerical_correctness=True) for r in request["ranks"]]
        evidence = dict(status="PASS", checks=dict(fused_operator=True, numerical_correctness=True, cross_node=True), evidence=detail)
        return request, evidence

    def test_adapter_requires_exact_case_operator_group_mode_rank_and_repeats(self):
        request, evidence = self.request_and_evidence()
        p.validate_mc2_evidence(evidence, request)
        for key, value in (("operator","all_reduce"), ("case","other"), ("group","other"), ("execution_mode","graph"),
                           ("repeats",1), ("versions",{}), ("parameters",{}), ("ranks",evidence["evidence"]["ranks"][:1])):
            bad = copy.deepcopy(evidence)
            bad["evidence"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.validate_mc2_evidence(bad, request)
        bad = copy.deepcopy(evidence)
        bad["evidence"]["ranks"][1]["synchronized"] = False
        with self.assertRaises(ValueError):
            p.validate_mc2_evidence(bad, request)

    def test_adapter_receives_request_and_keeps_nonzero_exit_failure(self):
        request, evidence = self.request_and_evidence()
        payload = dict(argv=[sys.executable,"-c", "import json,os; r=json.loads(os.environ['A5_MC2_REQUEST']); print('A5_ADAPTER '+" + repr(json.dumps(evidence)) + ")"],
                       mc2_request=request, required_checks=["fused_operator","numerical_correctness","cross_node"])
        self.assertEqual(p.adapter(payload)["status"], "PASS")
        payload["argv"][-1] += "; raise SystemExit(3)"
        with self.assertRaisesRegex(RuntimeError, "rc=3"):
            p.adapter(payload)

    def test_mc2_failure_cannot_be_hidden_by_other_case_or_primitives(self):
        config = self.config(case(), case("all_gather_matmul", "agmm"), case("matmul_reduce_scatter", "mmrs"))
        def remote(node, payload, env=None):
            if payload["action"] == "inventory":
                result = inv(["192.0.2.1" if node["name"] == "a" else "192.0.2.2"])
                result.update(proxy_present=False, mounts=[], env={})
            else:
                result = [dict(rc=0, consistent=True)]
            return dict(status="PASS", events=[dict(event="result", result=result)])
        def group(nodes, selected, config, stage, case=None):
            return dict(status="FAIL" if case and case["name"] == "agmm" else "PASS")
        with patch.object(p, "call_remote", side_effect=remote), patch.object(p, "tcp_matrix", return_value=dict(status="PASS")), patch.object(p, "run_group", side_effect=group):
            report = p.run(config, "check")
        self.assertEqual(report["checks"]["mc2/dense"]["status"], "PASS")
        self.assertEqual(report["checks"]["mc2/agmm"]["status"], "FAIL")
        self.assertEqual(report["checks"]["mc2/mmrs"]["status"], "UNVERIFIED")
        self.assertTrue(p.gate(report, "primitives"))
        self.assertFalse(p.gate(report, "mc2"))
        self.assertFalse(p.gate(report, "service"))

    def test_inspect_does_not_run_mc2(self):
        config = self.config()
        with patch.object(p, "call_remote", side_effect=RuntimeError("fixture stop")), patch.object(p, "run_group") as group:
            report = p.run(config, "inspect")
        group.assert_not_called()
        self.assertEqual(report["checks"]["mc2/dense"]["status"], "UNVERIFIED")
        self.assertFalse(p.gate(report, "mc2"))

    def test_missing_api_reports_capability_failure_before_network(self):
        torch = SimpleNamespace(distributed=SimpleNamespace())
        with patch.dict(sys.modules, {"torch": torch, "torch.distributed": torch.distributed, "torch_npu": SimpleNamespace()}), patch.object(p, "emit") as emit:
            with self.assertRaisesRegex(RuntimeError, "缺少真实融合 API"):
                p.mc2_worker(dict(case=case(), rank=0, world=2, node="a", device=0))
        failures = [c for c in emit.call_args_list if c.args[0] == "mc2_failure"]
        self.assertEqual(failures[0].kwargs["phase"], "capability")
        self.assertEqual(failures[0].kwargs["operator"], "matmul_all_reduce")

    def test_same_host_rank_evidence_does_not_pass_cross_node(self):
        config = self.config()
        selected = {"a":dict(ip="192.0.2.1",nic="eth0"), "b":dict(ip="192.0.2.2",nic="eth0")}
        def remote(node, payload, env):
            w = payload["workers"][0]
            return dict(status="PASS", events=[dict(event="result", action="mc2", result=dict(
                rank=w["rank"], device=w["device"], node=w["node"], case="dense", operator="matmul_all_reduce",
                status="PASS", host_id="same-kernel", measurements=[{},{}]))])
        with patch.object(p, "call_remote", side_effect=remote):
            self.assertEqual(p.run_group(config["nodes"], selected, config, "mc2", case=case())["status"], "FAIL")

    def test_mc2_pass_scope_still_requires_model_e2e_for_service(self):
        config = self.config()
        def remote(node, payload, env=None):
            result = inv(["192.0.2.1" if node["name"] == "a" else "192.0.2.2"])
            result.update(proxy_present=False, mounts=[], env={})
            if payload["action"] == "dns":
                result = [dict(rc=0, consistent=True)]
            return dict(status="PASS", events=[dict(event="result", result=result)])
        with patch.object(p, "call_remote", side_effect=remote), patch.object(p, "tcp_matrix", return_value=dict(status="PASS")), patch.object(p, "run_group", return_value=dict(status="PASS")):
            report = p.run(config, "check")
        self.assertTrue(p.gate(report, "mc2"))
        self.assertFalse(p.gate(report, "service"))
        self.assertEqual(report["checks"]["model_e2e"]["status"], "UNVERIFIED")

    def test_legacy_no_case_manifest_has_no_mc2_scope(self):
        config = cfg()
        config["require_mc2"] = True
        with patch.object(p, "call_remote", side_effect=RuntimeError("fixture stop")):
            report = p.run(config, "inspect")
        self.assertNotIn("mc2", report["required"])

    def test_example_requires_support_review_before_connection(self):
        template = json.loads((Path(p.__file__).parent.parent / "examples/mc2-cases.json").read_text())
        config = cfg()
        config["groups"][0]["name"] = "model-domain"
        config.update(template)
        with patch.object(p, "call_remote") as remote, self.assertRaisesRegex(ValueError, "support_ref"):
            p.run(config, "check")
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()

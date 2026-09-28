from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest import mock
from pathlib import Path

from mcp_server import MAX_PROBE_OUTPUT_BYTES, _build_server, run_probe
from tools import change_graph, dispatch_adapter, git_analyzer, query, review_plan, review_results, routing, tech_detector, transport


class QueryToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp_dir.name)
        cls._git("init", "-b", "master")
        cls._git("config", "user.email", "query-test@example.com")
        cls._git("config", "user.name", "Query Test")

        cls._write(
            "app.py",
            "class Worker:\n"
            "    def run(self, value):\n"
            "        return value + 1\n"
            "\n"
            "def helper():\n"
            "    return 'old'\n",
        )
        cls._write("other.py", "def other():\n    return 'old'\n")
        cls._write("large.py", "\n".join(f"value_{number} = {number}" for number in range(450)) + "\n")
        cls._write(
            "versions/v3/serializers.py",
            "class ListingSerializer:\n    pass\n\ndef normalize():\n    return 'v3'\n",
        )
        cls._write(
            "versions/v4/serializers.py",
            "class ListingSerializer:\n    pass\n\ndef legacy():\n    return 'v4'\n",
        )
        cls._write("matches.py", "# MATCH_ME\n# MATCH_ME\n# MATCH_ME\n")
        cls._write("resources.py", "def map_resource(value):\n    return value\n")
        cls._write("test_models.py", "def test_model_contract():\n    assert True\n")
        cls._git("add", ".")
        cls._git("commit", "-m", "base")

        cls._write(
            "app.py",
            "class Worker:\n"
            "    def run(self, value):\n"
            "        return value + 2\n"
            "\n"
            "def helper():\n"
            "    return 'new'\n",
        )
        cls._write("other.py", "def other():\n    return 'new'\n")
        cls._write("large.py", "\n".join(f"value_{number} = {number + 1}" for number in range(450)) + "\n")
        cls._write(
            "versions/v3/serializers.py",
            "class ListingSerializer:\n    pass\n\ndef normalize():\n    return 'updated'\n",
        )
        cls._git("add", ".")
        cls._git("commit", "-m", "change")
        cls.base = "HEAD~1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp_dir.cleanup()

    @classmethod
    def _git(cls, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cls.root,
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def _write(cls, relative_path: str, content: str) -> None:
        path = cls.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _lane_id(plan: dict, display_name: str) -> str:
        return next(lane_id for lane_id, decision in plan["routing"].items()
                    if decision["display_name"] == display_name)

    @staticmethod
    def _claim(**overrides: object) -> dict:
        claim = {
            "changed_cause_anchor": {
                "file": "app.py", "line": 3, "side": "new",
                "anchor_snippet": "        return value + 2",
            },
            "affected_site_anchors": [],
            "summary": "incorrect result", "root_cause": "incorrect computation",
            "severity": "warning", "severity_reason": {
                "code": "bounded-current-risk", "rationale": "A supported request gets a wrong value.",
            },
            "reachability": {"kind": "supported-api-request", "supported_input": "GET /items"},
            "precondition": "The request supplies an existing item.",
            "observable_impact": "The response contains an incorrect value.",
            "failure_scenario": "A supported API request returns an incorrect value.",
            "evidence_refs": ["bundle:hunks:item"], "unknowns": [],
            "critical_assumptions": [], "falsifying_checks": ["Call the supported endpoint."],
            "cheapest_falsifying_check": "Call the supported endpoint once.",
            "lens": "Core Review",
        }
        claim.update(overrides)
        return claim

    def test_get_change_supports_file_and_dotted_symbol(self) -> None:
        symbol = query.get_change(str(self.root), self.base, symbol="app.py:Worker.run", with_context=1, view="full")
        self.assertEqual(symbol["enclosing_symbol"], "Worker")
        self.assertIn("value + 1", symbol["before"])
        self.assertIn("value + 2", symbol["after"])
        self.assertIn("-        return value + 1", symbol["hunk"])

        whole_file = query.get_change(str(self.root), self.base, file="large.py", view="full")
        self.assertTrue(whole_file["truncated"])
        self.assertIn("before", whole_file["truncated_fields"])
        self.assertIn("after", whole_file["truncated_fields"])

        file_diff = git_analyzer.get_file_diff(str(self.root), "app.py", self.base, 1)
        self.assertIn("app.py", file_diff)
        self.assertNotIn("other.py", file_diff)

        compact = query.get_change(str(self.root), self.base, file="large.py", max_lines=10, max_chars=1_000)
        self.assertEqual(compact["view"], "hunk")
        self.assertNotIn("before", compact)
        self.assertLessEqual(compact["payload_bytes"], 2_000)

        batched = query.get_changes(str(self.root), self.base, ["app.py", "other.py"], max_lines=20)
        self.assertEqual(batched["returned_items"], 2)
        self.assertEqual([item["file"] for item in batched["changes"]], ["app.py", "other.py"])

    def test_find_symbol_handles_dotted_names_and_empty_results(self) -> None:
        result = query.find_symbol(str(self.root), "Worker.run")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["matches"][0]["kind"], "method")

        empty = query.find_symbol(str(self.root), "does_not_exist")
        self.assertEqual(empty["matches"], [])
        self.assertIn("reason", empty)

    def test_get_source_caps_and_memoizes_ranges(self) -> None:
        query._source_cache.clear()
        first = query.get_source(str(self.root), "large.py", line_range=[1, 450], context=0, max_lines=10)
        second = query.get_source(str(self.root), "large.py", line_range=[1, 450], context=0, max_lines=10)
        self.assertTrue(first["truncated"])
        self.assertEqual(first, second)
        self.assertEqual(len(query._source_cache), 1)

        around = query.get_source(str(self.root), "app.py", around_symbol="Worker.run", context=0)
        self.assertIn("def run", around["source"])

        beyond_eof = query.get_source(str(self.root), "app.py", line_range=[100, 110])
        self.assertIn("reason", beyond_eof)
        whole_file = query.get_source(str(self.root), "app.py", max_lines=2)
        self.assertEqual(whole_file["requested_range"], [1, 6])
        self.assertTrue(whole_file["truncated"])

    def test_find_siblings_compares_top_level_structure(self) -> None:
        result = query.find_siblings(str(self.root), self.base, "versions/v3/serializers.py")
        sibling = next(item for item in result["siblings"] if item["file"] == "versions/v4/serializers.py")
        self.assertIn({"name": "ListingSerializer", "kind": "class"}, sibling["shared_structure"])
        self.assertIn("legacy", sibling["differences_summary"])
        self.assertIn("normalize", sibling["differences_summary"])

    def test_grep_repo_preserves_true_total_under_cap(self) -> None:
        result = query.grep_repo(str(self.root), "MATCH_ME", langs=["py"], cap=1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["total_matches"], 3)
        self.assertTrue(result["truncated"])

        empty = query.grep_repo(str(self.root), "[", langs=["py"])
        self.assertIn("reason", empty)

    def test_verify_anchor_accepts_exact_and_corrects_drift(self) -> None:
        exact = query.verify_anchor(str(self.root), "app.py", 3, "        return value + 2")
        self.assertTrue(exact["ok"])

        shifted = query.verify_anchor(str(self.root), "app.py", 1, "return value + 2")
        self.assertFalse(shifted["ok"])
        self.assertEqual(shifted["suggested_line"], 3)

        missing = query.verify_anchor(str(self.root), "app.py", 1, "never appears")
        self.assertIsNone(missing["suggested_line"])
        self.assertIn("reason", missing)

    def test_list_changes_surfaces_changed_files(self) -> None:
        result = change_graph.list_changes(str(self.root), self.base)
        self.assertIn("app.py", result["changed_files"])
        self.assertIn("large.py", result["changed_files"])

    def test_list_changes_marks_docstrings_and_documentation_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "docs@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Docs Test"], cwd=root, check=True)
            (root / "module.py").write_text('def unchanged():\n    """old docs"""\n    return 1\n', encoding="utf-8")
            (root / "README.md").write_text("old docs\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            (root / "module.py").write_text('def unchanged():\n    """new docs"""\n    return 1\n', encoding="utf-8")
            (root / "README.md").write_text("new docs\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "docs"], cwd=root, check=True, capture_output=True)

            result = change_graph.list_changes(str(root), "HEAD~1")
            self.assertEqual(set(result["documentation_files"]), {"README.md", "module.py"})
            self.assertTrue(result["changes"][0]["touches_docs"])

    def test_probe_returns_bounded_deterministic_results(self) -> None:
        success = run_probe(str(self.root), [sys.executable, "-c", "print('ok')"])
        self.assertEqual(set(success), {"stdout", "stderr", "exit_code", "timed_out"})
        self.assertEqual(success["stdout"], "ok\n")
        self.assertEqual(success["exit_code"], 0)
        self.assertFalse(success["timed_out"])

        failure = run_probe(str(self.root), [sys.executable, "-c", "import sys; print('no', file=sys.stderr); sys.exit(7)"])
        self.assertEqual(failure["exit_code"], 7)
        self.assertIn("no", failure["stderr"])

        timeout = run_probe(str(self.root), [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
        self.assertEqual(timeout["exit_code"], 124)
        self.assertTrue(timeout["timed_out"])

        truncated = run_probe(str(self.root), [sys.executable, "-c", "print('x' * 20000)"])
        self.assertLessEqual(len(truncated["stdout"].encode()), MAX_PROBE_OUTPUT_BYTES)
        self.assertIn("truncated", truncated["stdout"])

        invalid_directory = run_probe(str(self.root), [sys.executable, "-c", "print('never')"], working_directory="../")
        self.assertEqual(invalid_directory["exit_code"], 2)
        self.assertIn("invalid probe request", invalid_directory["stderr"])

        invalid_stage = run_probe(str(self.root), [sys.executable, "-c", "print('never')"], stage="lens")
        self.assertEqual(invalid_stage["exit_code"], 2)
        self.assertIn("probe stage", invalid_stage["stderr"])

    def test_verifier_result_handling_keeps_audit_and_requires_verdict(self) -> None:
        confirmed = {"file": "app.py", "line": 3, "summary": "confirmed", "severity": "blocking"}
        refuted = {"file": "app.py", "line": 4, "summary": "refuted", "severity": "warning"}
        unverified = {"file": "app.py", "line": 5, "summary": "unverified", "severity": "warning"}
        refining = {"file": "app.py", "line": 6, "summary": "refining", "severity": "refining"}
        results = [
            {"candidate_id": review_results.finding_id(confirmed), "verdict": "CONFIRMED", "evidence": ["source"]},
            {"candidate_id": review_results.finding_id(refuted), "verdict": "REFUTED", "evidence": ["counterexample"]},
        ]

        processed = review_results.apply_verdicts([confirmed, refuted, unverified, refining], results)
        self.assertEqual([item["summary"] for item in processed["findings"]], ["confirmed", "refining"])
        self.assertEqual(processed["findings"][0]["verdict"], "CONFIRMED")
        self.assertNotIn("verdict", processed["findings"][1])
        self.assertEqual(processed["verification_output"], results)

    def test_finding_validation_root_cause_dedup_and_verification_batch(self) -> None:
        base = self._claim(summary="validation is swallowed", root_cause="swallowed validation")
        duplicate = self._claim(
            summary="validation is swallowed", root_cause="swallowed validation",
            changed_cause_anchor={"file": "other.py", "line": 2, "side": "new",
                                  "anchor_snippet": "    return 'new'"},
            lens="Consistency & Docs",
        )
        self.assertTrue(review_results.validate_finding(base)["valid"])
        self.assertFalse(review_results.validate_finding({**base, "changed_cause_anchor": {
            **base["changed_cause_anchor"], "anchor_snippet": "one\ntwo",
        }})["valid"])
        self.assertFalse(review_results.validate_finding({**base, "observable_impact": ""})["valid"])
        merged = review_results.deduplicate_findings([base, duplicate])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["affected_site_anchors"]), 2)
        batch = review_results.build_verification_batch(merged)
        self.assertEqual(batch["candidates"][0]["max_lookup_calls"], 6)

    def test_batch_anchor_and_lookup_budgets(self) -> None:
        anchors = query.verify_anchors(str(self.root), [{"file": "app.py", "line": 3, "anchor_snippet": "        return value + 2"}])
        self.assertTrue(anchors["results"][0]["ok"])
        review_plan._plans.clear()
        review_plan._lookup_usage.clear()
        review_plan._verification_batches.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        core_lane = self._lane_id(plan, "Core Review")
        self.assertTrue(plan["routing"][core_lane]["run"])
        core_bundle = review_plan.get_review_bundle(plan["review_id"], core_lane)
        self.assertGreaterEqual(core_bundle["investigative_response_budget"], 4)
        self.assertEqual(core_bundle["reserved_response_budget"], 2)
        self.assertLessEqual(core_bundle["response_budget"], 10)
        for _ in range(4):
            self.assertTrue(review_plan.consume_lookup_budget(plan["review_id"], core_lane)["ok"])
        self.assertFalse(review_plan.consume_lookup_budget(plan["review_id"], core_lane)["ok"])
        verification = review_plan.begin_verification(plan["review_id"], [{"candidate_id": "x"}])
        for _ in range(6):
            self.assertTrue(review_plan.consume_lookup_budget(plan["review_id"], "Verification", verification_id=verification["verification_id"], candidate_id="x")["ok"])
        self.assertFalse(review_plan.consume_lookup_budget(plan["review_id"], "Verification", verification_id=verification["verification_id"], candidate_id="x")["ok"])

    def test_docs_drift_routing_and_fact_bundle_are_documentation_only(self) -> None:
        inventory = {
            "documentation_files": ["README.md"],
            "changes": [
                {"file": "module.py", "symbol": "documented", "touches_docs": True},
                {"file": "service.py", "symbol": "execute", "touches_docs": False},
            ],
        }
        per_file = {
            "README.md": {"file": "README.md", "hunk": "docs"},
            "module.py": {"file": "module.py", "hunk": "docstring"},
            "service.py": {"file": "service.py", "hunk": "code"},
        }
        bundle = routing.build_docs_drift_bundle(inventory, per_file)
        self.assertTrue(bundle["applicable"])
        self.assertEqual(bundle["files"], ["README.md"])
        self.assertEqual([item["file"] for item in bundle["file_changes"]], ["README.md"])

        code_only = routing.build_docs_drift_bundle(
            {"documentation_files": [], "changes": [{"file": "service.py", "touches_docs": False}]},
            per_file,
        )
        self.assertFalse(code_only["applicable"])
        self.assertEqual(code_only["file_changes"], [])

    def test_portable_inventory_and_tech_markers(self) -> None:
        """Each supported portable stack returns reviewable rows, not an empty Python result."""
        for name, path, before, after, tech_flag in (
            ("typescript", "src/api.ts", "export function old() {}\n", "export function changed(value: string) {}\n", "typescript"),
            ("go", "cmd/main.go", "package main\nfunc main() {}\n", "package main\nfunc main() { println(\"x\") }\n", "go"),
            ("terraform", "infra/main.tf", "resource \"x\" \"a\" {}\n", "resource \"x\" \"a\" { name = \"b\" }\n", "terraform"),
            ("bundle", "databricks.yml", "bundle:\n  name: old\n", "bundle:\n  name: new\n", "databricks_bundle"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "portable@example.com"], cwd=root, check=True)
                subprocess.run(["git", "config", "user.name", "Portable"], cwd=root, check=True)
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(before)
                subprocess.run(["git", "add", "."], cwd=root, check=True)
                subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
                (root / path).write_text(after)
                subprocess.run(["git", "commit", "-am", "change"], cwd=root, check=True, capture_output=True)
                inventory = change_graph.list_changes(str(root), "HEAD~1")
                self.assertTrue(inventory["found"])
                self.assertTrue(inventory["changes"])
                self.assertTrue(getattr(tech_detector.detect(root), tech_flag))

    def test_phase6_coherence_resolves_calls_but_keeps_test_definitions_and_text_as_leads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "phase6@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Phase Six"], cwd=root, check=True)
            (root / "base.py").write_text(
                "def legacy():\n    return 1\n\nclass Parent:\n    def removed(self):\n        return 1\n"
            )
            (root / "caller.py").write_text("from base import legacy\n\ndef use():\n    return legacy()\n")
            (root / "child.py").write_text(
                "from base import Parent\n\nclass Child(Parent):\n    def use(self):\n        return self.removed()\n"
            )
            (root / "test_base.py").write_text("def legacy():\n    return 2\n")
            (root / "labels.py").write_text("LABEL = 'legacy'\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            (root / "base.py").write_text("class Parent:\n    pass\n")
            subprocess.run(["git", "add", "base.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "remove old api"], cwd=root, check=True, capture_output=True)

            coherence = change_graph.change_coherence(str(root), "HEAD~1")
            orphaned = {(item["symbol"], item["caller_file"], item["resolved_via"], item["confidence"])
                        for item in coherence["orphaned_references"]}
            self.assertIn(("legacy", "caller.py", "imported_call", "high"), orphaned)
            self.assertIn(("Parent.removed", "child.py", "inherited_method", "high"), orphaned)
            self.assertNotIn("test_base.py", {item["caller_file"] for item in coherence["orphaned_references"]})
            leads = {(item["caller_file"], item["confidence"]) for item in coherence["low_confidence_leads"]}
            self.assertIn(("labels.py", "low"), leads)
            self.assertIn(("test_base.py", "low"), leads)
            self.assertTrue(all(item["supporting_source_span"] for item in coherence["orphaned_references"]))

    def test_phase6_capabilities_and_selective_verifier_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "phase6@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Phase Six"], cwd=root, check=True)
            (root / "valid.py").write_text("def ok():\n    return 1\n")
            (root / "main.go").write_text("package main\nfunc main() {}\n")
            (root / "artifact.bin").write_bytes(b"\x00\x01base")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            (root / "valid.py").write_text("def broken(:\n")
            (root / "main.go").write_text("package main\nfunc main() { println(1) }\n")
            (root / "artifact.bin").write_bytes(b"\x00\x01changed")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "mixed parse"], cwd=root, check=True, capture_output=True)
            inventory = change_graph.list_changes(str(root), "HEAD~1")
            capabilities = {item["file"]: item["capability"] for item in inventory["file_capabilities"]}
            self.assertEqual(capabilities["valid.py"], "hunk-only")
            self.assertEqual(capabilities["main.go"], "hunk-only")
            self.assertEqual(capabilities["artifact.bin"], "unsupported")

        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        empty = review_plan.begin_verification(plan["review_id"], [])
        self.assertFalse(empty["dispatch_required"])
        self.assertIsNone(empty["verification_id"])

    def test_base_resolution_routing_and_stable_shards(self) -> None:
        self.assertEqual(git_analyzer.resolve_base_ref(str(self.root))[0], "master")
        base, reason = git_analyzer.resolve_base_ref(str(self.root), "HEAD~1")
        self.assertEqual(base, "HEAD~1")
        self.assertIsNone(reason)
        routes = routing.route_review_lanes(
            {"changed_files": ["infra/main.tf", "README.md"], "changes": []}, {"has_iac": True}
        )
        self.assertTrue(routes["Operations"]["run"])
        self.assertIn("Impact", routes["Operations"]["activated_checks"])
        self.assertNotIn("Contract", routes["Core Review"]["activated_checks"])
        changes = [{"file": f"src/{i:03}.ts", "symbol": str(j)} for i in range(40) for j in range(20)]
        shards = routing.shard_assignment(sorted({row["file"] for row in changes}), changes)
        self.assertTrue(shards["sharded"])
        flattened = [path for shard in shards["shards"] for path in shard["assigned_files"]]
        self.assertEqual(flattened, sorted(set(flattened)))
        self.assertFalse(routing.shard_assignment(["a.ts"], [{"file": "a.ts", "symbol": "x"}])["sharded"])
        one_file = [{"file": "generated.ts", "symbol": str(i)} for i in range(400)]
        split_file = routing.shard_assignment(["generated.ts"], one_file)
        self.assertTrue(split_file["sharded"])
        self.assertEqual([row["symbol"] for shard in split_file["shards"] for row in shard["symbols"]], [str(i) for i in range(400)])

    def test_semantic_routing_does_not_treat_models_filename_as_database_work(self) -> None:
        inventory = {
            "changed_files": ["app/models.py", "app/tests/test_models.py", "app/router.py"],
            "changes": [
                {"file": "app/models.py", "symbol": "FilterConfig", "kind": "class", "change": "added", "is_public": True},
                {"file": "app/router.py", "symbol": "recommend", "kind": "function", "change": "modified", "is_public": True},
            ],
        }
        hunks = {
            "app/models.py": "+class FilterConfig: pass",
            "app/tests/test_models.py": "+def test_filter(): pass",
            "app/router.py": "+def recommend(request): return client.invoke(request.query_params)",
        }
        tech = {"has_api_surface": True, "has_database": True, "has_async_workers": False}
        lanes = routing.route_review_lanes(inventory, tech, hunks)
        self.assertTrue(lanes["Core Review"]["run"])
        self.assertIn("Contract", lanes["Core Review"]["activated_checks"])
        self.assertIn("AppSec", lanes["Core Review"]["activated_checks"])
        self.assertNotIn("Performance & DB", lanes["Operations"]["activated_checks"])
        self.assertEqual(lanes["Operations"]["assigned_files"], ["app/router.py"])

    def test_prepare_review_returns_compact_lane_bundles(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        core_lane = self._lane_id(plan, "Core Review")
        self.assertTrue(core_lane.startswith("lane_"))
        self.assertNotIn("bundles", plan)
        self.assertLess(plan["payload_bytes"], 30_000)

        bundle = review_plan.get_review_bundle(plan["review_id"], core_lane)
        self.assertEqual(bundle["review_id"], plan["review_id"])
        self.assertEqual(bundle["lane_id"], core_lane)
        self.assertEqual(bundle["lane_display_name"], "Core Review")
        self.assertIn(bundle["sections"]["hunks"]["status"], {"complete", "truncated"})
        self.assertGreater(bundle["payload_bytes"], 0)

    def test_review_identity_and_source_ignore_dirty_worktree(self) -> None:
        review_plan._plans.clear()
        first = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        path = self.root / "app.py"
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text("def changed_worktree():\n    return 'snapshot two'\n", encoding="utf-8")
            second = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
            self.assertEqual(first["review_id"], second["review_id"])
            self.assertTrue(second["revision"]["worktree_dirty"])
            snapshot = review_plan.review_snapshot(second["review_id"])
            source = query.get_source(snapshot.source_root, "app.py", line_range=[1, 6], context=0)
            self.assertIn("class Worker", source["source"])
            self.assertNotIn("changed_worktree", source["source"])
        finally:
            path.write_text(original, encoding="utf-8")

    def test_consolidation_rejects_future_only_claim_and_preserves_questions(self) -> None:
        future_only = self._claim(
            summary="future concern", root_cause="future", severity="refining",
            severity_reason={"code": "future-only", "rationale": "Only a later model can trigger it."},
            reachability={"kind": "future-only", "supported_input": "A hypothetical future model"},
        )
        current = self._claim(
            summary="current issue", root_cause="current", severity="refining",
            severity_reason={"code": "maintenance-cost", "rationale": "Current behavior is harder to maintain."},
        )
        result = review_results.consolidate(
            str(self.root), [{"findings": [future_only, current], "unanswered": [{"question_id": "q1", "question": "Is CI configured?"}]}]
        )
        self.assertEqual([finding["summary"] for finding in result["findings"]], ["current issue"])
        self.assertTrue(result["unanswered"][0]["question_id"].startswith("q:"))
        self.assertTrue(any(item["disposition"] == "future-only" for item in result["candidate_dispositions"]))

    def test_bundle_digest_identity_and_cross_lane_rejection(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        core_lane = self._lane_id(plan, "Core Review")
        bundle = review_plan._plan_for(plan["review_id"])["bundles"][core_lane]
        self.assertEqual(bundle["bundle_digest"], review_plan.bundle_digest(bundle))
        self.assertEqual(review_plan.canonical_json({"a": 1, "b": 2}), review_plan.canonical_json({"b": 2, "a": 1}))
        changed = {**bundle, "bundle_digest": "sha256:wrong"}
        self.assertFalse(review_plan.validate_lane_result(plan["review_id"], changed)["ok"])
        wrong_lane = {**bundle, "lane_id": "lane_not_a_lane"}
        self.assertFalse(review_plan.validate_lane_result(plan["review_id"], wrong_lane)["ok"])
        for field in ("review_id", "base_sha", "head_sha"):
            invalid = {**bundle, field: "wrong"}
            self.assertFalse(review_plan.validate_lane_result(plan["review_id"], invalid)["ok"])
        self.assertNotEqual(review_plan.bundle_digest({**bundle, "reason": "x"}), bundle["bundle_digest"])

    def test_question_normalization_resolutions_and_state_retention(self) -> None:
        first = review_results.consolidate(str(self.root), [{"lane": "Core Review", "findings": [],
            "unanswered": ["Does cryptography remain installed?", "Is the lockfile current?"]}], review_id="r")
        self.assertEqual(len(first["unanswered"]), 2)
        self.assertTrue(first["normalization_warnings"])
        q1, q2 = [item["question_id"] for item in first["unanswered"]]
        second = review_results.resolve_questions(first, [{"question_id": q1, "evidence": "lock summary", "outcome": "resolved-no-finding", "resolving_phase": "core"}])
        self.assertTrue(second["ok"])
        self.assertEqual([item["question_id"] for item in second["state"]["unanswered"]], [q2])
        bad = review_results.resolve_questions(first, [{"question_id": "missing", "evidence": "x", "outcome": "resolved-no-finding"}])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["state"], first)
        self.assertTrue(any(item["field"] == "question_id" for item in bad["errors"]))

    def test_scoped_grep_excludes_cache_and_dependency_lock_routes_to_core(self) -> None:
        (self.root / ".mypy_cache").mkdir(exist_ok=True)
        (self.root / ".mypy_cache" / "bad.py").write_text("MATCH_SCOPE\n", encoding="utf-8")
        target = self.root / "core" / "src" / "app" / "accounts" / "tests"
        target.mkdir(parents=True, exist_ok=True)
        (target / "test_a.py").write_text("MATCH_SCOPE\n", encoding="utf-8")
        result = query.grep_repo(str(self.root), "MATCH_SCOPE", path="core/src/app/accounts/tests")
        self.assertEqual([item["file"] for item in result["matches"]], ["core/src/app/accounts/tests/test_a.py"])
        lanes = routing.route_review_lanes({"changed_files": ["uv.lock"], "changes": []}, {}, {"uv.lock": "+version = '2.13.0'"})
        self.assertIn("Dependency Compatibility", lanes["Core Review"]["activated_checks"])

    def test_changed_anchor_dependency_summary_and_dispatch_metrics(self) -> None:
        changed = query.verify_changed_anchors(str(self.root), self.base, [{
            "changed_cause_anchor": {"file": "app.py", "line": 3, "side": "new",
                                     "anchor_snippet": "        return value + 2"},
        }])
        self.assertTrue(changed["results"][0]["ok"])
        stale = query.verify_changed_anchors(str(self.root), self.base, [{
            "changed_cause_anchor": {"file": "app.py", "line": 1, "side": "new",
                                     "anchor_snippet": "class Worker:"},
        }])
        self.assertFalse(stale["results"][0]["ok"])
        self.assertEqual(stale["results"][0]["reason_code"], "diff-anchor-invalid")
        summary = review_plan._dependency_summary(["uv.lock"], {"uv.lock": " name = 'PyJWT'\n-version = '2.12.0'\n+version = '2.13.0'\n name = 'cryptography'\n"})
        self.assertEqual(summary["resolved_versions"][0]["package"], "PyJWT")
        self.assertIn("cryptography", summary["required_packages_seen"])
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        active = [lane for lane, decision in plan["routing"].items() if decision["run"]]
        self.assertFalse(review_plan.record_dispatch_cycle(plan["review_id"], active, {lane: "haiku" for lane in active})["ok"])
        observations = [{
            "lane_id": lane_id, "phase": "lane", "model_id": "haiku", "response_count": 1,
            "tool_calls": ["get_change"], "tool_observation_exact": True,
            "aggregate_waits": int(index == 0), "poll_wakeups": 0,
        } for index, lane_id in enumerate(active)]
        self.assertTrue(review_plan.ingest_host_observations(
            plan["review_id"], observations, provenance="verified",
        )["ok"])
        metrics = review_plan.get_metrics(plan["review_id"])
        self.assertEqual(metrics["compliance"]["aggregate_wait"], "pass")
        self.assertEqual(metrics["compliance"]["model_policy"], "pass")

    def test_pr_snapshots_handle_stacked_heads_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("config", "user.email", "snapshot@example.com")
            git("config", "user.name", "Snapshot Test")
            (root / "common.py").write_text("BASE = True\n", encoding="utf-8")
            (root / "test_other.py").write_text("def other_setting():\n    return 'base'\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            (root / "parent.py").write_text("PARENT = 'a'\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "parent pr")
            parent_sha = git("rev-parse", "HEAD")
            parent = review_plan.prepare_review(
                str(root), mode="pr", base_ref=base_sha,
                expected_head_sha=parent_sha, expected_changed_paths=["parent.py"],
            )
            self.assertEqual(parent["status"], "prepared")
            self.assertEqual(parent["revision"]["changed_paths"], ["parent.py"])
            self.assertEqual(parent["revision"]["base_sha"], base_sha)
            self.assertEqual(parent["revision"]["merge_base_sha"], base_sha)
            self.assertEqual(parent["revision"]["head_sha"], parent_sha)
            self.assertEqual(parent["revision"]["effective_range"], f"{base_sha}..{parent_sha}")

            (root / "test_settings.py").write_text(
                "def child_setting():\n    return 'CHILD_MARK'\n", encoding="utf-8",
            )
            git("add", ".")
            git("commit", "-m", "child pr")
            child_sha = git("rev-parse", "HEAD")
            git("branch", "parent-target", parent_sha)
            child = review_plan.prepare_review(
                str(root), mode="pr", base_ref="parent-target",
                expected_head_sha=child_sha, expected_changed_paths=["test_settings.py"],
            )
            self.assertEqual(child["revision"]["changed_paths"], ["test_settings.py"])
            child_plan = review_plan._plan_for(child["review_id"])
            self.assertIn("test_settings.py", json.dumps(child_plan["bundles"]))
            self.assertNotIn("parent.py", json.dumps(child_plan["bundles"]))
            checked_git = git_analyzer._git_checked

            def divergent_inventory(git_root: str, *args: str, **kwargs: object) -> str | bytes:
                if args[:2] == ("diff", "--name-only"):
                    return "wrong.py\n"
                return checked_git(git_root, *args, **kwargs)

            with mock.patch("tools.git_analyzer._git_checked", side_effect=divergent_inventory):
                divergent = review_plan.prepare_review(
                    str(root), mode="pr", base_ref="parent-target",
                    expected_base_sha=parent_sha, expected_head_sha=child_sha,
                )
            self.assertEqual(divergent["code"], "inventory-mismatch")

            def fail_pinned_diff(git_root: str, *args: str, **kwargs: object) -> str | bytes:
                if len(args) >= 2 and args[:2] == ("diff", "-U0"):
                    raise git_analyzer.SnapshotError("git-failed", "simulated post-prepare Git failure")
                return checked_git(git_root, *args, **kwargs)

            with mock.patch("tools.git_analyzer._git_checked", side_effect=fail_pinned_diff):
                failed_build = review_plan.prepare_review(
                    str(root), mode="branch", base_ref=parent_sha,
                    expected_head_sha=child_sha,
                )
            self.assertEqual(failed_build["status"], "invalid-base")
            self.assertEqual(failed_build["code"], "git-failed")
            child_snapshot = review_plan.review_snapshot(child["review_id"])
            pinned = query.get_source(
                child_snapshot.source_root, "test_settings.py", line_range=[1, 2], context=0,
            )
            self.assertIn("CHILD_MARK", pinned["source"])

            (root / "test_settings.py").write_text("DIRTY = True\n", encoding="utf-8")
            still_pinned = query.get_source(
                child_snapshot.source_root, "test_settings.py", line_range=[1, 2], context=0,
            )
            self.assertIn("CHILD_MARK", still_pinned["source"])
            affected = query.verify_changed_anchors(
                str(root), parent_sha, [{
                    "changed_cause_anchor": {"file": "test_settings.py", "line": 1, "side": "new",
                                             "anchor_snippet": "def child_setting():"},
                    "affected_site_anchors": [{
                        "file": "test_settings.py", "line": 2,
                        "anchor_snippet": "    return 'CHILD_MARK'",
                    }],
                }], head=child_sha, source_root=child_snapshot.source_root,
            )
            self.assertTrue(affected["results"][0]["ok"])
            self.assertTrue(affected["results"][0]["affected_site_results"][0]["ok"])

            server = _build_server()

            async def prepare_through_mcp() -> dict:
                response = await server.call_tool("prepare_review", {
                    "mode": "pr", "base_ref": "parent-target",
                    "expected_base_sha": parent_sha, "expected_head_sha": child_sha,
                    "expected_changed_paths": ["test_settings.py"],
                })
                return json.loads(response.content[0].text)

            with mock.patch.dict(os.environ, {"GW_REVIEW_ROOT": str(root)}):
                prepared_via_mcp = asyncio.run(prepare_through_mcp())
            self.assertEqual(prepared_via_mcp["review_id"], child["review_id"])
            self.assertTrue(prepared_via_mcp["revision"]["worktree_dirty"])
            child_core_lane = self._lane_id(child, "Core Review")

            async def lookup(operation: str, arguments: dict, *, verification_id: str = "",
                             candidate_id: str = "") -> dict:
                response = await server.call_tool("review_lookup", {
                    "review_id": child["review_id"], "lane_id": child_core_lane,
                    "operation": operation, "question": f"Check {operation}",
                    "arguments": arguments, "verification_id": verification_id,
                    "candidate_id": candidate_id,
                })
                return json.loads(response.content[0].text)

            async def initial_lookups() -> list[dict]:
                return await asyncio.gather(
                    lookup("get_change", {"file": "test_settings.py"}),
                    lookup("find_symbol", {"name": "child_setting"}),
                    lookup("get_source", {"file": "test_settings.py", "line_range": [1, 2]}),
                    lookup("grep_repo", {"pattern": "CHILD_MARK", "langs": ["py"]}),
                )

            lane_lookups = asyncio.run(initial_lookups())
            verification = review_plan.begin_verification(
                child["review_id"], [{"candidate_id": "stacked-child"}],
            )

            async def verification_lookups() -> list[dict]:
                return await asyncio.gather(
                    lookup("find_siblings", {"file": "test_settings.py"},
                           verification_id=verification["verification_id"], candidate_id="stacked-child"),
                    lookup("verify_anchor", {"file": "test_settings.py", "line": 1,
                                             "expected_snippet": "def child_setting():"},
                           verification_id=verification["verification_id"], candidate_id="stacked-child"),
                    lookup("changed_file", {"file": "test_settings.py"},
                           verification_id=verification["verification_id"], candidate_id="stacked-child"),
                )

            all_lookups = lane_lookups + asyncio.run(verification_lookups())
            self.assertTrue(all(item.get("quota_consumed") for item in all_lookups))
            self.assertTrue(all("DIRTY" not in json.dumps(item) for item in all_lookups))
            self.assertIn("CHILD_MARK", json.dumps(all_lookups[0]))
            self.assertIn("child_setting", json.dumps(all_lookups[1]))
            self.assertIn("CHILD_MARK", json.dumps(all_lookups[2]))
            self.assertIn("CHILD_MARK", json.dumps(all_lookups[3]))
            self.assertTrue(all_lookups[5]["result"]["ok"])
            self.assertTrue(all_lookups[6]["result"]["changed"])

            async def snapshot_tools() -> list[dict]:
                calls = (
                    ("list_changes", {"review_id": child["review_id"]}),
                    ("trace", {"review_id": child["review_id"],
                               "symbol": "test_settings.py:child_setting"}),
                    ("change_coherence", {"review_id": child["review_id"]}),
                    ("probe", {"review_id": child["review_id"],
                               "command": f"{sys.executable} -c \"print(open('test_settings.py').read())\""}),
                    ("get_contract_changes", {"review_id": child["review_id"],
                                              "modified_file_paths": ["test_settings.py"]}),
                    ("get_tech_profile", {"review_id": child["review_id"]}),
                )
                responses = await asyncio.gather(*(
                    server.call_tool(name, arguments) for name, arguments in calls
                ))
                return [json.loads(response.content[0].text) for response in responses]

            bound_tools = asyncio.run(snapshot_tools())
            self.assertTrue(all("DIRTY" not in json.dumps(item) for item in bound_tools))
            self.assertIn("CHILD_MARK", bound_tools[3]["stdout"])
            git("branch", "-f", "parent-target", base_sha)
            self.assertIsNotNone(review_plan.get_review_bundle(child["review_id"], child_core_lane).get("bundle_digest"))
            moved = review_plan.prepare_review(
                str(root), mode="pr", base_ref="parent-target",
                expected_base_sha=parent_sha, expected_head_sha=child_sha,
            )
            self.assertEqual(moved["status"], "invalid-base")
            self.assertEqual(moved["code"], "base-moved")
            self.assertIsNone(moved["review_id"])
            self.assertEqual(moved["routing"], {})

            mismatch = review_plan.prepare_review(
                str(root), mode="pr", base_ref=parent_sha,
                expected_head_sha=child_sha, expected_changed_paths=["wrong.py"],
            )
            self.assertEqual(mismatch["status"], "invalid-base")
            self.assertEqual(mismatch["code"], "changed-path-mismatch")
            self.assertIsNone(mismatch["review_id"])
            wrong_head = review_plan.prepare_review(
                str(root), mode="pr", base_ref=parent_sha, expected_head_sha=parent_sha,
            )
            self.assertEqual(wrong_head["code"], "head-mismatch")
            unavailable = review_plan.prepare_review(
                str(root), mode="pr", base_ref="refs/heads/does-not-exist",
                expected_head_sha=child_sha,
            )
            self.assertEqual(unavailable["code"], "unavailable-ref")
            self.assertEqual(review_plan.prepare_review(str(root), mode="pr")["code"], "missing-base")
            self.assertEqual(
                review_plan.prepare_review(str(root), mode="pr", base_ref=parent_sha)["code"],
                "missing-head",
            )
            materialized = Path(child_snapshot.source_root)
            self.assertTrue(review_plan.discard_review(child["review_id"]))
            self.assertFalse(materialized.exists())
            self.assertIsNone(review_plan.review_snapshot(child["review_id"]))
            self.assertEqual(review_plan.get_metrics(child["review_id"])["code"], "UNKNOWN_REVIEW")

        with tempfile.TemporaryDirectory() as non_repo:
            failed = review_plan.prepare_review(
                non_repo, mode="pr", base_ref="main", expected_head_sha="deadbeef",
            )
            self.assertEqual(failed["status"], "invalid-base")
            self.assertEqual(failed["code"], "git-failed")

    def test_deleted_code_can_anchor_to_old_diff_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("config", "user.email", "deleted@example.com")
            git("config", "user.name", "Deleted Anchor")
            (root / "removed.py").write_text("def removed():\n    return 'old'\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            (root / "removed.py").unlink()
            git("add", "-A")
            git("commit", "-m", "delete")
            head_sha = git("rev-parse", "HEAD")
            result = query.verify_changed_anchors(str(root), base_sha, [{
                "changed_cause_anchor": {"file": "removed.py", "line": 2,
                                         "anchor_snippet": "    return 'old'", "side": "old"},
            }], head=head_sha)
            self.assertTrue(result["results"][0]["ok"])
            self.assertEqual(result["results"][0]["cause_anchor_result"]["side"], "old")
            with mock.patch(
                "tools.git_analyzer._git_checked",
                side_effect=git_analyzer.SnapshotError("git-failed", "simulated pinned diff failure"),
            ):
                with self.assertRaises(git_analyzer.SnapshotError):
                    git_analyzer.get_diff_hunks(str(root), base_sha, head=head_sha)

    def test_review_tool_schemas_require_snapshot_identity(self) -> None:
        async def schemas() -> dict:
            return {tool.name: tool.input_schema for tool in await _build_server().list_tools()}

        tools = asyncio.run(schemas())
        for name in {
            "list_changes", "trace", "change_coherence", "probe",
            "get_contract_changes", "get_tech_profile", "get_review_bundle",
            "get_lane_dispatch", "acknowledge_lane_dispatch", "get_evidence_item",
            "get_evidence_page", "get_transport_page", "review_lookup", "verify_anchors",
            "begin_verification", "consolidate_review_results",
            "resolve_review_questions", "get_review_metrics", "get_review_status",
            "ingest_dispatch_observations", "render_consolidated_review_report",
        }:
            self.assertIn("review_id", tools[name].get("required", []), name)
        self.assertIn("mode", tools["prepare_review"].get("required", []))
        resolution_schema = tools["resolve_review_questions"]["$defs"]["QuestionResolution"]
        self.assertEqual(set(resolution_schema["required"]), {
            "question_id", "outcome", "evidence", "resolving_phase",
        })
        self.assertEqual(set(resolution_schema["properties"]["outcome"]["enum"]),
                         {"resolved-no-finding", "resolved-new-candidate"})
        for name in {"get_review_bundle", "get_lane_dispatch", "acknowledge_lane_dispatch",
                     "get_evidence_item", "get_evidence_page", "review_lookup"}:
            self.assertIn("lane_id", tools[name].get("required", []), name)

    def test_phase3_registered_typed_lookup_tools_are_snapshot_scoped_and_documented(self) -> None:
        setup = (Path(__file__).parents[1] / "SETUP.md").read_text(encoding="utf-8")

        async def invoke() -> tuple[set[str], list[dict]]:
            server = _build_server()
            names = {tool.name for tool in await server.list_tools()}
            calls = [
                ("get_change", {"symbol": "app.py:Worker.run"}),
                ("find_symbol", {"name": "Worker"}),
                ("get_source", {"file": "app.py", "line_range": [1, 3]}),
                ("find_siblings", {"file": "app.py"}),
                ("grep_repo", {"pattern": "Worker"}),
                ("verify_anchor", {"file": "app.py", "line": 3, "expected_snippet": "        return value + 2"}),
                ("changed_file", {"file": "app.py"}),
            ]
            results = []
            for tool_name, arguments in calls:
                review_plan._plans.clear()
                review_plan._lookup_usage.clear()
                prepared = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
                lane_id = next(lane for lane, decision in prepared["routing"].items() if decision["run"])
                response = await server.call_tool(tool_name, {
                    "review_id": prepared["review_id"], "lane_id": lane_id,
                    "question": "Retrieve one concrete pinned fact.", **arguments,
                })
                results.append(json.loads(response.content[0].text))
            return names, results

        names, results = asyncio.run(invoke())
        expected = set(dispatch_adapter.LANE_TOOLS)
        self.assertTrue(expected <= names)
        self.assertTrue(all(result["quota_consumed"] for result in results), results)
        self.assertTrue(all("remaining_lookup_calls" in result for result in results))
        for tool_name in sorted(expected):
            self.assertIn(f"`{tool_name}`", setup)
        self.assertIn(dispatch_adapter.LOOKUP_SCHEMA_VERSION, setup)

    def test_phase2_pr14660_bundle_is_bounded_idempotent_and_retrievable(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        lane_id = self._lane_id(plan, "Core Review")
        first = review_plan.get_review_bundle(plan["review_id"], lane_id)
        second = review_plan.get_review_bundle(plan["review_id"], lane_id)
        self.assertEqual(review_plan.canonical_json(first), review_plan.canonical_json(second))
        lane_metrics = review_plan.get_metrics(plan["review_id"])["lanes"][lane_id]
        self.assertTrue(lane_metrics["fetched"])
        self.assertFalse(lane_metrics["acknowledged"])
        self.assertEqual(lane_metrics["fetch_count"], 2)
        server = _build_server()

        async def repeated_mcp_fetches():
            arguments = {"review_id": plan["review_id"], "lane_id": lane_id}
            return await asyncio.gather(
                server.call_tool("get_review_bundle", arguments),
                server.call_tool("get_review_bundle", arguments),
            )

        with mock.patch.dict(os.environ, {"GW_REVIEW_ROOT": str(self.root)}):
            mcp_first, mcp_second = asyncio.run(repeated_mcp_fetches())
        self.assertEqual(mcp_first.content[0].text.encode("utf-8"),
                         mcp_second.content[0].text.encode("utf-8"))
        self.assertLessEqual(
            transport.envelope_bytes(transport.json_text(first)), transport.MAX_MCP_RESPONSE_BYTES,
        )
        self.assertNotIn("Core Review", lane_id)
        self.assertEqual(review_plan.get_review_bundle(plan["review_id"], "Core Review")["reason"],
                         "unknown opaque lane_id")
        for section_name, section in first["sections"].items():
            self.assertIn(section["status"], {"complete", "empty", "not_applicable", "truncated", "failed"})
            page = review_plan.get_evidence_page(plan["review_id"], lane_id, section_name, limit=20)
            self.assertEqual(page["total_items"], section["total_items"])
            for item in page["items"]:
                exact = review_plan.get_evidence_item(plan["review_id"], lane_id, item["item_id"])
                self.assertEqual(exact["payload"], item["payload"])

    def test_phase2_ev721_overflows_are_bounded_and_losslessly_pageable(self) -> None:
        review_id = "EV721"
        payloads = [{"rows": [f"bundle-{number}-" + "x" * 2_000 for _ in range(50)]}
                    for number in range(2)]
        for payload in payloads:
            text = transport.respond(payload, endpoint="get_review_bundle", review_id=review_id)
            descriptor = json.loads(text)
            self.assertEqual(descriptor["code"], "RESPONSE_COMPACTED")
            self.assertLessEqual(transport.envelope_bytes(text), transport.MAX_MCP_RESPONSE_BYTES)
            pages, cursor = [], descriptor["cursor"]
            while cursor:
                page = transport.get_page(review_id, cursor)
                pages.append(page["content"])
                cursor = page["next_cursor"]
            self.assertEqual("".join(pages), transport.json_text(payload))

    def test_phase2_ev704_tech_overflow_is_bounded_by_actual_mcp_envelope(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        huge = tech_detector.TechProfile(
            fastapi=True, evidence={"fastapi": [f"path/{number}/" + "x" * 500 for number in range(300)]},
        )
        server = _build_server()

        async def invoke():
            with mock.patch("mcp_server.tech_detector.detect", return_value=huge):
                return await server.call_tool("get_tech_profile", {
                    "review_id": plan["review_id"], "compact": False,
                })

        response = asyncio.run(invoke())
        serialized = response.model_dump_json(by_alias=True, exclude_none=True)
        self.assertLessEqual(len(serialized.encode("utf-8")), transport.MAX_MCP_RESPONSE_BYTES)
        self.assertEqual(json.loads(response.content[0].text)["code"], "RESPONSE_COMPACTED")

    def test_phase2_pr14720_dispatch_requires_exact_prompt_digest(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        lane_id = self._lane_id(plan, "Core Review")
        bundle = review_plan.get_review_bundle(plan["review_id"], lane_id)
        dispatch = review_plan.build_lane_dispatch(bundle, "Review only current failures")
        self.assertEqual(dispatch["delivery_status"], "unverified")
        altered = dispatch["prompt_payload"] + " "
        rejected = review_plan.acknowledge_lane_dispatch(
            plan["review_id"], lane_id, altered, dispatch["prompt_payload_digest"],
        )
        self.assertFalse(rejected["ok"])
        accepted = review_plan.acknowledge_lane_dispatch(
            plan["review_id"], lane_id, dispatch["prompt_payload"], dispatch["prompt_payload_digest"],
        )
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["embedded_payload_bytes"], dispatch["prompt_payload_bytes"])

    def test_phase2_ranked_coherence_preserves_omitted_caller_snippets(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        lane_id = self._lane_id(plan, "Core Review")
        evidence = {
            "weak": [{"confidence": "low", "caller_file": f"weak_{number}.py",
                      "caller_snippet": "weak(" + "x" * 800 + ")"} for number in range(12)],
            "direct": [{"confidence": "high", "resolution": "direct", "caller_file": "direct.py",
                        "caller_snippet": "direct_call()"}],
        }
        section = review_plan._compact_section(plan["review_id"], lane_id, "coherence", evidence)
        self.assertEqual(section["status"], "truncated")
        self.assertEqual(section["items"][0]["value"]["caller_file"], "direct.py")
        recovered, cursor = [], section["cursor"]
        while cursor:
            page = review_plan.get_evidence_page(plan["review_id"], lane_id, "coherence", cursor, 3)
            recovered.extend(page["items"])
            cursor = page["cursor"]
        self.assertTrue(any("caller_snippet" in item["payload"]["value"] for item in recovered))

    def test_phase4_pr14660_unchanged_sites_validate_then_claim_is_refuted(self) -> None:
        claim = self._claim(
            affected_site_anchors=[
                {"file": "resources.py", "line": 1,
                 "anchor_snippet": "def map_resource(value):"},
                {"file": "test_models.py", "line": 1,
                 "anchor_snippet": "def test_model_contract():"},
            ],
            causal_link={"mechanism": "The changed computation feeds both unchanged consumers.",
                         "evidence_refs": ["trace:resolved-callers"]},
        )
        anchors = query.verify_changed_anchors(str(self.root), self.base, [claim])
        self.assertTrue(anchors["results"][0]["ok"])
        self.assertTrue(all(item["ok"] for item in anchors["results"][0]["affected_site_results"]))
        candidate_id = review_results.finding_id(claim)
        result = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [claim]}],
            verification_results=[{"candidate_id": candidate_id, "verdict": "REFUTED",
                                   "evidence": ["The consumers do not use the changed return path."]}],
            base=self.base,
        )
        self.assertEqual(result["findings"], [])
        self.assertTrue(any(item["disposition"] == "refuted" for item in result["candidate_dispositions"]))

    def test_phase4_anchor_failures_have_distinct_reason_codes(self) -> None:
        wrong_text = self._claim(changed_cause_anchor={
            "file": "app.py", "line": 3, "side": "new", "anchor_snippet": "not the source",
        })
        outside_diff = self._claim(changed_cause_anchor={
            "file": "app.py", "line": 1, "side": "new", "anchor_snippet": "class Worker:",
        })
        bad_site = self._claim(
            affected_site_anchors=[{"file": "missing.py", "line": 1, "anchor_snippet": "missing"}],
            causal_link={"mechanism": "The value is passed to the missing consumer.",
                         "evidence_refs": ["trace:consumer"]},
        )
        results = query.verify_changed_anchors(str(self.root), self.base, [wrong_text, outside_diff, bad_site])["results"]
        self.assertEqual([item["reason_code"] for item in results], [
            "source-anchor-invalid", "diff-anchor-invalid", "affected-source-invalid",
        ])
        unsupported = self._claim(affected_site_anchors=[{
            "file": "other.py", "line": 2, "anchor_snippet": "    return 'new'",
        }])
        consolidated = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [unsupported]}], base=self.base,
        )
        self.assertEqual(consolidated["candidate_dispositions"][0]["disposition"],
                         "unsupported-causal-link")

    def test_phase4_supported_request_is_current_and_future_or_low_impact_is_not_promoted(self) -> None:
        supported = self._claim(reachability={
            "kind": "supported-api-request", "supported_input": "POST /filters with a documented payload",
        })
        self.assertTrue(review_results.validate_finding(supported)["valid"])
        supported_result = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [supported]}], base=self.base,
        )
        self.assertEqual(supported_result["candidate_dispositions"][0]["disposition"], "unresolved")
        self.assertEqual(len(supported_result["verification_candidates"]), 1)
        future = self._claim(
            summary="hypothetical validation path", root_cause="future model validation",
            severity="refining", severity_reason={"code": "future-only", "rationale": "No current model reaches it."},
            reachability={"kind": "future-only", "supported_input": "A hypothetical stricter model"},
        )
        intentional = self._claim(
            summary="intentional shadow subset", root_cause="intentional subset",
            severity="refining", severity_reason={"code": "low-impact", "rationale": "The subset is intentional."},
        )
        result = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [future, intentional]}], base=self.base,
        )
        self.assertEqual(result["findings"], [])
        self.assertEqual({item["disposition"] for item in result["candidate_dispositions"]},
                         {"future-only", "low-impact"})
        self.assertEqual([item["summary"] for item in result["optional_notes"]], ["intentional shadow subset"])

    def test_phase4_extra_reachability_metadata_does_not_change_validation(self) -> None:
        claim = self._claim(reachability={
            "kind": "supported-api-request", "supported_input": "GET /items", "network_scope": "internal",
        })
        self.assertTrue(review_results.validate_finding(claim)["valid"])

    def test_phase4_resolution_transaction_preserves_state_and_metrics_on_error(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        review_id = plan["review_id"]
        state = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [],
                              "unanswered": [{"question": "Is deployment private?",
                                              "why_unresolved": "No configuration evidence",
                                              "suggested_resolution": "Inspect the deployment configuration"}]}],
            review_id=review_id,
        )
        review_plan._consolidations[review_id] = state
        before_state = review_plan.canonical_json(state)
        before_metrics = deepcopy(review_plan._metrics[review_id])
        invalid = review_results.resolve_questions(state, [{
            "question_id": "missing", "outcome": "resolved-no-finding", "evidence": "guess",
        }])
        self.assertFalse(invalid["ok"])
        self.assertEqual(review_plan.canonical_json(invalid["state"]), before_state)
        self.assertEqual(review_plan._metrics[review_id], before_metrics)
        question_id = state["unanswered"][0]["question_id"]
        server = _build_server()

        async def mixed_resolution() -> dict:
            response = await server.call_tool("consolidate_review_results", {
                "review_id": review_id, "lane_results": [], "verification_results": None,
                "resolutions": [{"question_id": "missing", "outcome": "resolved-no-finding",
                                 "evidence": "guess", "resolving_phase": "verification"}],
            })
            return json.loads(response.content[0].text)

        rejected_mixed = asyncio.run(mixed_resolution())
        self.assertEqual(rejected_mixed["code"], "RESOLUTION_ONLY_REQUIRED")
        self.assertEqual(review_plan.canonical_json(review_plan._consolidations[review_id]), before_state)
        self.assertEqual(review_plan._metrics[review_id], before_metrics)

        async def resolve(resolutions: list[dict]) -> dict:
            response = await server.call_tool("resolve_review_questions", {
                "review_id": review_id, "resolutions": resolutions,
            })
            return json.loads(response.content[0].text)

        invalid_mcp = asyncio.run(resolve([{
            "question_id": "missing", "outcome": "resolved-no-finding", "evidence": "guess",
            "resolving_phase": "verification",
        }]))
        self.assertFalse(invalid_mcp["ok"])
        self.assertTrue(invalid_mcp["state_unchanged"])
        self.assertEqual(review_plan.canonical_json(review_plan._consolidations[review_id]), before_state)
        self.assertEqual(review_plan._metrics[review_id], before_metrics)
        valid = asyncio.run(resolve([{
            "question_id": question_id, "outcome": "resolved-no-finding",
            "evidence": "Deployment configuration inspected", "resolving_phase": "verification",
        }]))
        self.assertTrue(valid["ok"])
        self.assertEqual(review_plan._consolidations[review_id]["unanswered"], [])
        self.assertEqual(review_plan.get_metrics(review_id)["unanswered_resolved"][0]["question_id"], question_id)

    def test_phase4_verifier_receives_complete_claim_and_ignores_self_reported_independence(self) -> None:
        claim = self._claim(independent_evidence=True)
        batch = review_results.build_verification_batch([claim])
        self.assertEqual(len(batch["candidates"]), 1)
        candidate = batch["candidates"][0]
        for field in {"changed_cause_anchor", "affected_site_anchors", "reachability", "precondition",
                      "observable_impact", "failure_scenario", "evidence_refs", "unknowns",
                      "cheapest_falsifying_check"}:
            self.assertIn(field, candidate)
        recorded = self._claim(independent_evidence=[{
            "reference": "tool:verify_anchor:123", "provenance": "recorded-tool",
        }])
        self.assertEqual(review_results.build_verification_batch([recorded])["candidates"], [])

    def test_verifier_results_require_registered_source_or_typed_receipt_provenance(self) -> None:
        review_plan._plans.clear()
        review_plan._verification_batches.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        review_id = plan["review_id"]
        candidate = review_results.build_verification_batch([self._claim()])["candidates"][0]
        batch = review_plan.begin_verification(review_id, [candidate])
        verification_id = batch["verification_id"]
        issued = review_plan.deterministic_source_verification(
            review_id, verification_id, candidate["candidate_id"],
        )
        self.assertEqual(issued["verdict"], "PLAUSIBLE")
        accepted, rejected = review_plan.registered_verification_results(review_id, [issued])
        self.assertEqual(accepted, [issued])
        self.assertEqual(rejected, [])
        forged = {**issued, "verdict": "CONFIRMED"}
        accepted, rejected = review_plan.registered_verification_results(review_id, [forged])
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        receipt = review_plan.record_verification_lookup(
            review_id, verification_id, candidate["candidate_id"], "get_source", {"source": "pinned"},
        )
        registered = review_plan.register_verifier_result(
            review_id, verification_id, candidate["candidate_id"], "CONFIRMED", [receipt],
        )
        self.assertEqual(registered["provenance"], "registered-typed-tool-verifier")

    def test_ev752_verifier_dispatch_uses_schema_generated_candidate_contract(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        candidate = review_results.build_verification_batch([self._claim()])["candidates"][0]
        batch = review_plan.begin_verification(plan["review_id"], [candidate])
        dispatch = review_plan.build_verifier_dispatch(plan["review_id"], batch["verification_id"])
        payload = json.loads(dispatch["prompt_payload"])
        dispatched = payload["candidates"][0]
        self.assertEqual(dispatched["candidate_id"], candidate["candidate_id"])
        self.assertTrue(dispatched["cause_anchor_id"].startswith("anchor_"))
        self.assertIn("warning", dispatched["valid_severity_reason_codes"])
        self.assertNotIn("review_lookup", dispatch_adapter.VERIFIER_TOOLS)

        class Host:
            supports_hard_tool_allowlist = True
            supports_model_observation = True
            supports_response_observation = True
            supports_wait_observation = True
            supports_exact_tool_observation = True

            def launch_restricted(self, request):
                return dispatch_adapter.LaunchObservation(
                    lane_id=request.lane_id, phase=request.phase, model_id="haiku", response_count=1,
                    tool_calls=("get_source",), rejected_tool_calls=0, aggregate_waits=1,
                    poll_wakeups=0, prompt_digest=request.prompt_digest,
                )

        request = dispatch_adapter.LaunchRequest(
            review_id=plan["review_id"], lane_id="Verification", phase="verifier", model="haiku",
            prompt_payload=dispatch["prompt_payload"], prompt_digest=dispatch["prompt_payload_digest"],
            schema_version=dispatch["schema_version"], allowed_tools=dispatch_adapter.VERIFIER_TOOLS,
            response_budget=6, verification_id=batch["verification_id"], candidate_ids=(candidate["candidate_id"],),
        )
        observation = dispatch_adapter.DispatchAdapter(Host()).launch(request)
        self.assertTrue(observation.tool_observation_exact)

    def test_phase4_candidate_disposition_ledger_keeps_every_outcome_with_evidence(self) -> None:
        wrong_source = self._claim(
            summary="wrong source", root_cause="wrong source",
            changed_cause_anchor={"file": "app.py", "line": 3, "side": "new",
                                  "anchor_snippet": "not present"},
        )
        future = self._claim(
            summary="future", root_cause="future", severity="refining",
            severity_reason={"code": "future-only", "rationale": "Future model only."},
            reachability={"kind": "future-only", "supported_input": "Future model"},
        )
        low = self._claim(
            summary="editorial", root_cause="editorial", severity="refining",
            severity_reason={"code": "low-impact", "rationale": "Editorial only."},
            finding_kind="editorial",
        )
        retained = self._claim(
            summary="maintainability", root_cause="shared duplicate", severity="refining",
            severity_reason={"code": "maintenance-cost", "rationale": "Current maintenance cost."},
        )
        duplicate = self._claim(
            summary="maintainability duplicate", root_cause="shared duplicate", severity="refining",
            severity_reason={"code": "maintenance-cost", "rationale": "Current maintenance cost."},
            changed_cause_anchor={"file": "other.py", "line": 2, "side": "new",
                                  "anchor_snippet": "    return 'new'"},
        )
        unresolved = self._claim(summary="needs verification", root_cause="unresolved")
        refuted = self._claim(summary="refuted claim", root_cause="refuted")
        result = review_results.consolidate(
            str(self.root), [{"lane": "Core Review", "findings": [
                {}, wrong_source, future, low, retained, duplicate, unresolved, refuted,
            ]}],
            verification_results=[{
                "candidate_id": review_results.finding_id(refuted), "verdict": "REFUTED",
                "evidence": ["counterexample"],
            }],
            base=self.base,
        )
        dispositions = {item["disposition"] for item in result["candidate_dispositions"]}
        self.assertTrue({"schema-invalid", "source-anchor-invalid", "future-only", "low-impact",
                         "deduplicated", "refuted", "unresolved", "retained"}.issubset(dispositions))
        self.assertTrue(all(item["evidence"] is not None for item in result["candidate_dispositions"]))

    def test_phase5_restricted_adapter_requires_capable_host_and_never_passes_bash(self) -> None:
        class Host:
            supports_hard_tool_allowlist = True
            supports_model_observation = True
            supports_response_observation = True
            supports_wait_observation = True
            supports_exact_tool_observation = True

            def __init__(self) -> None:
                self.request = None

            def launch_restricted(self, request):
                self.request = request
                return dispatch_adapter.LaunchObservation(
                    lane_id=request.lane_id, phase=request.phase, model_id="haiku",
                    response_count=1, tool_calls=tuple(request.allowed_tools), rejected_tool_calls=0,
                    aggregate_waits=1, poll_wakeups=0, prompt_digest=request.prompt_digest,
                )

        host = Host()
        request = dispatch_adapter.LaunchRequest(
            review_id="r", lane_id="lane_x", phase="lane", model="haiku", prompt_payload="{}",
            prompt_digest="sha256:x", schema_version=dispatch_adapter.LOOKUP_SCHEMA_VERSION,
            allowed_tools=dispatch_adapter.LANE_TOOLS, response_budget=6,
        )
        observation = dispatch_adapter.DispatchAdapter(host).launch(request)
        self.assertEqual(set(observation.tool_calls), dispatch_adapter.LANE_TOOLS)
        self.assertNotIn("Bash", host.request.allowed_tools)
        recorded = dispatch_adapter.DispatchAdapter(host).launch_and_record(
            request, lambda _review_id, _events, provenance: {"ok": True, "provenance": provenance},
        )
        self.assertEqual(recorded["provenance"], "verified")
        with self.assertRaises(ValueError):
            dispatch_adapter.DispatchAdapter(host).launch(
                dispatch_adapter.LaunchRequest(**{**request.__dict__, "allowed_tools": frozenset({"Bash"})})
            )
        with self.assertRaisesRegex(ValueError, "schema version"):
            dispatch_adapter.DispatchAdapter(host).launch(
                dispatch_adapter.LaunchRequest(**{**request.__dict__, "schema_version": "wrong"})
            )

        class BashReportingHost(Host):
            def launch_restricted(self, request):
                observation = super().launch_restricted(request)
                return dispatch_adapter.LaunchObservation(
                    **{**observation.__dict__, "tool_calls": ("get_change", "Bash")}
                )

        with self.assertRaisesRegex(ValueError, "restricted allowlist"):
            dispatch_adapter.DispatchAdapter(BashReportingHost()).launch(request)

        class IncompleteHost(Host):
            supports_hard_tool_allowlist = False

        with self.assertRaisesRegex(RuntimeError, "hard tool allowlist"):
            dispatch_adapter.DispatchAdapter(IncompleteHost(), strict=True).launch(request)

    def test_phase5_host_observations_drive_degraded_and_complete_status(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        review_id = plan["review_id"]
        active = [lane_id for lane_id, decision in plan["routing"].items() if decision["run"]]
        for lane_id in active:
            bundle = review_plan.get_review_bundle(review_id, lane_id)
            dispatch = review_plan.build_lane_dispatch(bundle)
            self.assertTrue(review_plan.acknowledge_lane_dispatch(
                review_id, lane_id, dispatch["prompt_payload"], dispatch["prompt_payload_digest"],
            )["ok"])
        events = [{
            "lane_id": lane_id, "phase": "lane", "model_id": "haiku", "response_count": 1,
            "tool_calls": ["get_change"], "tool_observation_exact": True,
            "aggregate_waits": int(index == 0), "poll_wakeups": 0,
        } for index, lane_id in enumerate(active)]
        self.assertTrue(review_plan.ingest_host_observations(review_id, events, provenance="verified")["ok"])
        review_plan._consolidations[review_id] = review_results.consolidate(
            str(self.root), [], review_id=review_id, base=self.base,
        )
        self.assertEqual(review_plan.get_review_status(review_id)["review_status"], "complete")

        review_plan._plans.clear()
        model_plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        model_lane = next(lane_id for lane_id, decision in model_plan["routing"].items() if decision["run"])
        review_plan.ingest_host_observations(model_plan["review_id"], [{
            "lane_id": model_lane, "phase": "lane", "model_id": "sonnet", "response_count": 1,
            "tool_calls": ["get_change"], "tool_observation_exact": True,
            "aggregate_waits": 1, "poll_wakeups": 0,
        }], provenance="self-reported")
        model_status = review_plan.get_review_status(model_plan["review_id"])
        self.assertEqual(model_status["review_status"], "degraded")
        self.assertIn("observed model violates configured policy", model_status["degraded_reasons"])

    def test_phase5_over_budget_and_rejected_only_lookups_are_degraded(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        review_id = plan["review_id"]
        lane_id = next(lane for lane, decision in plan["routing"].items() if decision["run"])
        review_plan.ingest_host_observations(review_id, [{
            "lane_id": lane_id, "phase": "lane", "model_id": "haiku", "response_count": 25,
            "tool_calls": ["get_change"], "tool_observation_exact": True,
            "aggregate_waits": 7, "poll_wakeups": 7,
        }], provenance="verified")
        metrics = review_plan.get_metrics(review_id)
        self.assertEqual(metrics["actual_model_responses"], 25)
        self.assertEqual(metrics["compliance"]["response_budget"], "fail")
        self.assertEqual(metrics["aggregate_wait_count"], 7)
        self.assertEqual(metrics["poll_wakeup_count"], 7)
        self.assertEqual(review_plan.get_review_status(review_id)["review_status"], "degraded")

        review_plan._plans.clear()
        rejected_plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        rejected_lane = next(lane for lane, decision in rejected_plan["routing"].items() if decision["run"])
        for _ in range(12):
            review_plan.record_lookup_rejection(rejected_plan["review_id"], rejected_lane, "get_change")
        status = review_plan.get_review_status(rejected_plan["review_id"])
        self.assertEqual(status["review_status"], "degraded")
        self.assertIn("all attempted lookups were rejected", status["degraded_reasons"])

    def test_phase5_authoritative_render_ignores_caller_counts_and_mcp_observations_are_self_reported(self) -> None:
        review_plan._plans.clear()
        plan = review_plan.prepare_review(str(self.root), mode="branch", base_ref=self.base)
        review_id = plan["review_id"]
        review_plan._consolidations[review_id] = review_results.consolidate(
            str(self.root), [], review_id=review_id, base=self.base,
        )
        server = _build_server()

        async def invoke():
            tools = {tool.name: tool.input_schema for tool in await server.list_tools()}
            report = await server.call_tool("render_consolidated_review_report", {"review_id": review_id})
            lane_id = next(lane for lane, decision in plan["routing"].items() if decision["run"])
            observed = await server.call_tool("ingest_dispatch_observations", {
                "review_id": review_id,
                "observations": [{"lane_id": lane_id, "phase": "lane", "model_id": "haiku",
                                  "response_count": 1, "tool_calls": ["get_change"],
                                  "tool_observation_exact": True,
                                  "aggregate_waits": 1, "poll_wakeups": 0}],
            })
            return tools, report.content[0].text, json.loads(observed.content[0].text)

        schemas, report, observed = asyncio.run(invoke())
        self.assertEqual(schemas["render_consolidated_review_report"]["required"], ["review_id"])
        self.assertIn(f"Reviewed {plan['summary']['changed_file_count']} files", report)
        self.assertEqual(observed["provenance"], "self-reported")
        self.assertEqual(review_plan.get_review_status(review_id)["review_status"], "degraded")

    def test_compact_tech_profile_caps_evidence(self) -> None:
        profile = tech_detector.TechProfile(fastapi=True, evidence={"fastapi": ["z.py", "a.py", "b.py", "c.py"]})
        compact = profile.to_compact_dict(max_evidence_per_flag=2)
        self.assertEqual(compact["evidence"]["fastapi"]["paths"], ["a.py", "b.py"])
        self.assertEqual(compact["evidence"]["fastapi"]["total"], 4)
        self.assertTrue(compact["evidence"]["fastapi"]["truncated"])

    def test_base_resolver_prefers_origin_head_then_branch_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for branch in ("main", "master"):
                subprocess.run(["git", "init", "-b", branch], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "base@example.com"], cwd=root, check=True)
                subprocess.run(["git", "config", "user.name", "Base"], cwd=root, check=True)
                (root / "x").write_text("x")
                subprocess.run(["git", "add", "."], cwd=root, check=True)
                subprocess.run(["git", "commit", "-m", "x"], cwd=root, check=True, capture_output=True)
                break
            # No origin/HEAD: branch fallback is deterministic.
            self.assertEqual(git_analyzer.resolve_base_ref(str(root))[0], "main")
            self._git_at(root, "update-ref", "refs/remotes/origin/main", "HEAD")
            self._git_at(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            self.assertEqual(git_analyzer.resolve_base_ref(str(root))[0], "refs/remotes/origin/main")
            self.assertEqual(git_analyzer.resolve_base_ref(str(root), "HEAD")[0], "HEAD")

    @staticmethod
    def _git_at(root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()

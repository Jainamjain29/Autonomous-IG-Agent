"""Architectural isolation tests enforcing strict modular boundaries."""
import ast
import os
import unittest
from pathlib import Path

LEGACY_MODULES = {
    "app",
    "master_loop",
    "agent_brain",
    "flow_automator",
    "assembly_line",
    "ig_service",
    "database",
    "tunnel",
}

KNOWN_AGENTS = {"content", "video_gen", "sync_merge", "qc", "posting"}


class TestArchitecturalIsolation(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]

    def test_agents_never_import_each_other(self):
        """No agent inside agents/<name>/ may import from agents.<other_name>."""
        agents_dir = self.repo_root / "agents"
        if not agents_dir.exists():
            self.skipTest("agents directory not found")

        violations = []
        for agent_name in KNOWN_AGENTS:
            agent_path = agents_dir / agent_name
            if not agent_path.is_dir():
                continue

            for py_file in agent_path.rglob("*.py"):
                with open(py_file, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=str(py_file))

                for node in ast.walk(tree):
                    imported_modules = []
                    if isinstance(node, ast.Import):
                        imported_modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_modules = [node.module]

                    for mod in imported_modules:
                        for other_agent in KNOWN_AGENTS:
                            if other_agent != agent_name:
                                if mod == f"agents.{other_agent}" or mod.startswith(f"agents.{other_agent}."):
                                    violations.append(
                                        f"{py_file.name} in {agent_name} imports {mod}"
                                    )

        self.assertEqual(
            violations,
            [],
            f"Cross-agent import violations detected: {violations}",
        )

    def test_core_never_imports_legacy_modules_or_agents(self):
        """Core modules must never statically import legacy scripts or agent implementations."""
        core_dir = self.repo_root / "core"
        self.assertTrue(core_dir.exists(), "core directory must exist")

        violations = []
        for py_file in core_dir.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for node in ast.walk(tree):
                imported_modules = []
                if isinstance(node, ast.Import):
                    imported_modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules = [node.module]

                for mod in imported_modules:
                    base_mod = mod.split(".")[0]
                    if base_mod in LEGACY_MODULES:
                        violations.append(
                            f"{py_file.relative_to(self.repo_root)} imports legacy module '{mod}'"
                        )
                    if base_mod == "agents":
                        violations.append(
                            f"{py_file.relative_to(self.repo_root)} statically imports agent module '{mod}'"
                        )

        self.assertEqual(
            violations,
            [],
            f"Core boundary violations detected: {violations}",
        )


if __name__ == "__main__":
    unittest.main()

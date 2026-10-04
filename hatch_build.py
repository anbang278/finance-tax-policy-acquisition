"""Embed source provenance in artifacts without writing generated files into the checkout."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

REPOSITORY = "anbang278/finance-tax-policy-acquisition"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        identity = {
            "repository": REPOSITORY,
            "version": self.metadata.version,
            "commit": None,
            "dirty": None,
        }
        stored = root / "src/ftr/data/build-identity.json"
        if stored.is_file():
            identity = json.loads(stored.read_text(encoding="utf-8"))
        if (root / ".git").exists():
            try:
                commit = subprocess.run(
                    ["git", "--no-optional-locks", "-C", str(root), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2,
                    check=False,
                )
                status = subprocess.run(
                    ["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2,
                    check=False,
                )
                if commit.returncode == status.returncode == 0:
                    identity.update(commit=commit.stdout.strip(), dirty=bool(status.stdout))
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._temporary = tempfile.TemporaryDirectory(prefix="ftr-build-identity-")
        path = Path(self._temporary.name) / "build-identity.json"
        path.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
        target = (
            "ftr/data/build-identity.json"
            if self.target_name == "wheel"
            else "src/ftr/data/build-identity.json"
        )
        build_data["force_include"][str(path)] = target

    def finalize(self, version, build_data, artifact_path):
        self._temporary.cleanup()

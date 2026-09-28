"""Configuration-driven, evidence-preserving technology detection."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

_CONFIG = Path(__file__).parent.parent / "config" / "tech-markers.json"
_SKIP = {".git", "node_modules", ".venv", "vendor", "dist", "build"}


@dataclass
class TechProfile:
    # Existing fields stay stable for MCP callers.
    django: bool = False
    drf: bool = False
    fastapi: bool = False
    react: bool = False
    typescript: bool = False
    celery: bool = False
    pulumi: bool = False
    mysql: bool = False
    postgres: bool = False
    redis: bool = False
    opensearch: bool = False
    elasticsearch: bool = False
    terraform: bool = False
    databricks_bundle: bool = False
    go: bool = False
    has_api_surface: bool = False
    has_database: bool = False
    has_async_workers: bool = False
    has_deployment_config: bool = False
    has_iac: bool = False
    evidence: dict[str, list[str]] = field(default_factory=dict)

    def _mark(self, flag: str, source: Path, root: Path) -> None:
        if not hasattr(self, flag):
            return
        setattr(self, flag, True)
        rel = str(source.relative_to(root)) if source.is_relative_to(root) else str(source)
        if rel not in self.evidence.setdefault(flag, []):
            self.evidence[flag].append(rel)

    def to_dict(self) -> dict:
        return {**{key: value for key, value in self.__dict__.items() if key != "evidence"}, "evidence": self.evidence}

    def to_compact_dict(self, max_evidence_per_flag: int = 3) -> dict:
        """Return routing facts without serialising every matching repository file.

        A large monorepo can have thousands of files with a matching extension.
        Routing needs representative evidence, not that exhaustive list.  Keep the
        original ``to_dict`` for callers that explicitly need full provenance.
        """
        cap = max(0, int(max_evidence_per_flag))
        evidence = {
            flag: {
                "paths": sorted(paths)[:cap],
                "total": len(paths),
                "truncated": len(paths) > cap,
            }
            for flag, paths in sorted(self.evidence.items())
        }
        return {
            **{key: value for key, value in self.__dict__.items() if key != "evidence"},
            "evidence": evidence,
        }


def _dependencies(file: Path) -> set[str]:
    try:
        if file.name == "package.json":
            data = json.loads(file.read_text(encoding="utf-8"))
            return {key.lower() for group in ("dependencies", "devDependencies", "peerDependencies") for key in data.get(group, {})}
        if file.name == "pyproject.toml":
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            deps = list(data.get("project", {}).get("dependencies", []))
            deps.extend(data.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys())
            return {re.split(r"[<>=\[; ]", str(item).lower())[0] for item in deps}
        if file.name == "go.mod":
            return {line.split()[1].lower() for line in file.read_text(encoding="utf-8").splitlines() if line.strip().startswith("require ") and len(line.split()) > 1}
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        pass
    return set()


def detect(root: str | Path) -> TechProfile:
    root = Path(root)
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))
    profile = TechProfile()
    files: list[Path] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        files.extend(Path(directory) / name for name in names)
    deps_by_file = {path: _dependencies(path) for path in files if path.name in {"pyproject.toml", "package.json", "go.mod"}}
    for flag, marker in config["markers"].items():
        for path in files:
            if path.name in marker.get("filenames", []) or path.suffix.lower() in marker.get("extensions", []):
                profile._mark(flag, path, root)
            tokens = marker.get("manifest_markers", {}).get(path.name, [])
            if tokens:
                try:
                    if any(token in path.read_text(errors="ignore") for token in tokens):
                        profile._mark(flag, path, root)
                except OSError:
                    pass
        wanted = set(marker.get("dependencies", []))
        for manifest, deps in deps_by_file.items():
            if any(dep == candidate or dep.endswith("/" + candidate) for candidate in wanted for dep in deps):
                profile._mark(flag, manifest, root)
    for rollup, members in config["rollups"].items():
        active = [member for member in members if getattr(profile, member, False)]
        setattr(profile, rollup, bool(active))
        if active:
            profile.evidence[rollup] = [path for member in active for path in profile.evidence.get(member, [])]
    return profile

"""Tests for tools/deploy/prepare_atoms.py.

These run the real script against throwaway git repositories, covering the
packaging contract (managed paths, manifest schema, provenance pinning) and the
safety rails (refusals, untouched native files).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "deploy" / "prepare_atoms.py"


def git(*args, cwd, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture()
def dev_repo(tmp_path):
    repo = tmp_path / "dev"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    (repo / "service").mkdir()
    (repo / "service" / "__init__.py").write_text("SERVICE = 1\n")
    (repo / "service" / "api.py").write_text("def app(): ...\n")
    (repo / "frontend").mkdir()
    (repo / "frontend" / "index.html").write_text("<html></html>\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "DEMO.md").write_text("# demo\n")
    (repo / "supabase" / "migrations").mkdir(parents=True)
    (repo / "supabase" / "migrations" / "0001_baseline.sql").write_text("select 1;\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): assert True\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    # Excluded junk: must never be packaged.
    (repo / "README.md").write_text("readme\n")
    (repo / "api").mkdir()
    (repo / "api" / "index.py").write_text("vercel shim\n")
    script = repo / "service" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "source state", cwd=repo)
    return repo


@pytest.fixture()
def target_repo(tmp_path):
    repo = tmp_path / "deploy"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    (repo / ".atoms").mkdir()
    (repo / ".private").mkdir()
    (repo / "app" / "deploy" / "restopilot-integration").mkdir(parents=True)
    (repo / "app" / "backend" / "restopilot_app" / "service").mkdir(parents=True)
    (repo / "app" / "frontend" / "plugins").mkdir(parents=True)
    (repo / "app" / "frontend" / "plugins" / "restopilot-dashboard.ts").write_text(
        "// native plugin - never touch\n"
    )
    (repo / ".private" / "archive.bundle").write_text("authorized archive\n")
    (repo / "restopilot-release.json").write_text("{}\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "native setup", cwd=repo)
    return repo


def run_tool(*args, expect_success=True):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )
    if expect_success:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def load_manifest(target: Path) -> dict:
    return json.loads((target / "restopilot-release.json").read_text())


def test_packages_included_paths_only(dev_repo, target_repo):
    head = git("rev-parse", "HEAD", cwd=dev_repo)
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply")

    app = target_repo / "app" / "deploy" / "restopilot-integration"
    packaged = {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()}
    assert packaged == {
        "service/__init__.py",
        "service/api.py",
        "service/run.sh",
        "frontend/index.html",
        "docs/DEMO.md",
        "supabase/migrations/0001_baseline.sql",
        "tests/test_x.py",
        "pytest.ini",
        "requirements.txt",
    }
    # Generated backend mirrors service/ flattened.
    backend = target_repo / "app" / "backend" / "restopilot_app" / "service"
    assert {p.name for p in backend.rglob("*") if p.is_file()} == {
        "__init__.py",
        "api.py",
        "run.sh",
    }
    # Excluded junk and native files untouched.
    assert (target_repo / "app" / "frontend" / "plugins" / "restopilot-dashboard.ts").read_text().startswith("// native")
    assert not (app / "api").exists()


def test_manifest_schema_and_hashes(dev_repo, target_repo):
    head = git("rev-parse", "HEAD", cwd=dev_repo)
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply")

    manifest = load_manifest(target_repo)
    assert manifest["schema_version"] == 1
    assert manifest["source_commit"] == head
    assert manifest["source_repository"] == "https://github.com/fredericsetievi/AI-Agent-Resto-Pilot"
    assert manifest["application_path"] == "app/deploy/restopilot-integration"
    assert manifest["vendored_service_path"] == "app/backend/restopilot_app/service"
    assert "restopilot-release.json" not in manifest["files"]  # manifest never lists itself
    assert len(manifest["files"]) == 13  # 9 app + 3 backend + SOURCE_COMMIT

    entry = manifest["files"]["app/deploy/restopilot-integration/service/api.py"]
    assert entry["mode"] == "0o644"
    assert entry["sha256"] == hashlib.sha256(b"def app(): ...\n").hexdigest()
    exec_entry = manifest["files"]["app/backend/restopilot_app/service/run.sh"]
    assert exec_entry["mode"] == "0o755"
    sc = manifest["files"]["app/deploy/SOURCE_COMMIT"]
    assert sc["sha256"] == hashlib.sha256((head + "\n").encode()).hexdigest()
    assert (target_repo / "app" / "deploy" / "SOURCE_COMMIT").read_text() == head + "\n"


def test_rerun_is_a_no_op(dev_repo, target_repo):
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply")
    before = git("status", "--porcelain", cwd=target_repo)
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply")
    assert git("status", "--porcelain", cwd=target_repo) == before


def test_updates_existing_package_and_stages(dev_repo, target_repo):
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply", "--stage")
    git("commit", "-q", "-m", "package v1", cwd=target_repo)

    (dev_repo / "service" / "api.py").write_text("def app(): return 2\n")
    git("add", "-A", cwd=dev_repo)
    git("commit", "-q", "-m", "change api", cwd=dev_repo)

    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply", "--stage")
    staged = git("status", "--porcelain", cwd=target_repo)
    assert staged  # managed changes staged, ready for review
    staged_names = {line[3:] for line in staged.splitlines()}
    assert "app/deploy/restopilot-integration/service/api.py" in staged_names
    assert "restopilot-release.json" in staged_names
    # Native plugin never restaged.
    assert not any("restopilot-dashboard.ts" in name for name in staged_names)


def test_stale_files_reported_but_untouched(dev_repo, target_repo):
    leftover = target_repo / "app" / "deploy" / "restopilot-integration" / "landing"
    leftover.mkdir()
    (leftover / "index.html").write_text("old export\n")
    git("add", "-A", cwd=target_repo)
    git("commit", "-q", "-m", "with leftover", cwd=target_repo)

    result = run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply")
    assert "stale" in result.stdout
    assert (leftover / "index.html").read_text() == "old export\n"


def test_refuses_wrong_target(dev_repo, tmp_path):
    not_atoms = tmp_path / "not-atoms"
    not_atoms.mkdir()
    (not_atoms / "README.md").write_text("hello\n")
    result = run_tool(
        "--source", str(dev_repo), "--target", str(not_atoms), expect_success=False
    )
    assert "does not look like a restopilot-atoms-deploy clone" in result.stderr


def test_refuses_dirty_target_outside_managed_paths(dev_repo, target_repo):
    (target_repo / "unrelated.txt").write_text("uncommitted\n")
    result = run_tool(
        "--source", str(dev_repo), "--target", str(target_repo), "--apply", expect_success=False
    )
    assert "uncommitted changes outside the managed paths" in result.stderr
    # --allow-dirty proceeds.
    run_tool("--source", str(dev_repo), "--target", str(target_repo), "--apply", "--allow-dirty")


def test_refuses_dev_repo_as_target(dev_repo):
    # Without the packaging script present, the target-marker rail fires first.
    result = run_tool("--source", str(dev_repo), "--target", str(dev_repo), expect_success=False)
    assert "does not look like a restopilot-atoms-deploy clone" in result.stderr

    # Once the script exists (as in the real dev repo), the dedicated guard fires.
    guard = dev_repo / "tools" / "deploy"
    guard.mkdir(parents=True)
    (guard / "prepare_atoms.py").write_text("# guard\n")
    result = run_tool("--source", str(dev_repo), "--target", str(dev_repo), expect_success=False)
    assert "not the development repository" in result.stderr


def test_dry_run_writes_nothing(dev_repo, target_repo):
    head = git("rev-parse", "HEAD", cwd=dev_repo)
    run_tool("--source", str(dev_repo), "--target", str(target_repo))
    assert git("status", "--porcelain", cwd=target_repo) == ""
    assert (target_repo / "app" / "deploy" / "SOURCE_COMMIT").exists() is False

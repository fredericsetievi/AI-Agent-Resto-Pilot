"""Real Git fixtures exercise release isolation without any network or live data."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/deploy/prepare_atoms.py"
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location("prepare_atoms", SCRIPT)
    packaging = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packaging)
else:
    # The allowlisted hosted application copy intentionally has no tools/.
    pytest.skip("Development-only packaging tooling", allow_module_level=True)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def commit(root):
    git(root, "add", "--all")
    git(root, "-c", "user.name=Packaging Test", "-c", "user.email=packaging@example.invalid",
        "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def repos(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    for root in (source, target):
        root.mkdir()
        git(root, "init", "-q")
    for name in ("requirements.txt", "pytest.ini", "service/api.py", "frontend/index.html",
                 "frontend/i18n.js", "landing/index.html", "landing/styles.css", "docs/guide.md", "tests/test_api.py",
                 "supabase/migrations/0001_baseline.sql"):
        write(source, name, "source " + name)
    # Tracked files outside the export allowlist must never be copied.
    write(source, ".env", "private fixture")
    write(source, "private-history/archive", "private fixture")
    sha = commit(source)
    for name in ("start_app_v2.sh", "frontend/vite.config.ts",
                 "frontend/plugins/restopilot-dashboard.ts", "frontend/restopilot-adapter/api-base.js",
                 "backend/routers/restopilot.py", "backend/restopilot_app/sync_from_source.sh"):
        write(target, "app/" + name, "native " + name)
    write(target, ".gitignore", "*.ignored\n")
    write(target, ".private/history.bundle", "authorized archived history")
    commit(target)
    return source, target, sha


def test_pinned_export_preserves_scaffold_secrets_and_uncommitted_source(repos):
    source, target, sha = repos
    write(source, "service/api.py", "uncommitted code")
    write(source, "docs/CHAT_HANDOFF_2026-11-05.md", "local handoff")
    write(target, "private.ignored", "private local target")
    before = {p.relative_to(target): p.read_bytes() for p in (target / "app").rglob("*") if p.is_file()}
    plan = packaging.prepare(source, target, sha)
    assert not plan["applied"]
    assert not (target / "restopilot-release.json").exists()
    result = packaging.prepare(source, target, sha, apply=True)
    assert result["applied"]
    imported = target / "app/deploy/restopilot-integration"
    assert (imported / "service/api.py").read_text() == "source service/api.py"
    assert (imported / "landing/index.html").read_text() == "source landing/index.html"
    assert (imported / "landing/styles.css").read_text() == "source landing/styles.css"
    assert (target / "app/backend/restopilot_app/service/api.py").read_bytes() == (imported / "service/api.py").read_bytes()
    assert not (imported / "docs/CHAT_HANDOFF_2026-11-05.md").exists()
    assert not (target / ".env").exists()
    for name, data in before.items():
        assert (target / name).read_bytes() == data
    assert (target / "private.ignored").read_text() == "private local target"
    assert (target / ".private/history.bundle").read_text() == "authorized archived history"
    assert json.loads((target / "restopilot-release.json").read_text())["source_commit"] == sha
    assert (target / "app/deploy/SOURCE_COMMIT").read_text().strip() == sha
    commit(target)
    assert packaging.prepare(source, target, sha)["write"] == []


def test_dirty_target_is_rejected_without_overwriting(repos):
    source, target, sha = repos
    write(target, "app/start_app_v2.sh", "someone else's work")
    with pytest.raises(ValueError, match="Destination has local changes"):
        packaging.prepare(source, target, sha, apply=True)
    assert (target / "app/start_app_v2.sh").read_text() == "someone else's work"


def test_removed_managed_files_require_review_and_native_files_survive(repos):
    source, target, sha = repos
    packaging.prepare(source, target, sha, apply=True)
    commit(target)
    (source / "docs/guide.md").unlink()
    sha = commit(source)
    removed = "app/deploy/restopilot-integration/docs/guide.md"
    assert packaging.prepare(source, target, sha)["delete"] == [removed]
    with pytest.raises(ValueError, match="inspect the dry run"):
        packaging.prepare(source, target, sha, apply=True)
    assert (target / removed).exists()
    packaging.prepare(source, target, sha, apply=True, allow_delete=True)
    assert not (target / removed).exists()
    assert (target / "app/start_app_v2.sh").exists()


def test_destination_symlink_cannot_write_outside_clone(repos, tmp_path):
    source, target, sha = repos
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "app/deploy").symlink_to(outside, target_is_directory=True)
    commit(target)
    with pytest.raises(ValueError, match="symlink"):
        packaging.prepare(source, target, sha, apply=True)
    assert list(outside.iterdir()) == []


def test_secret_or_symlink_inside_source_allowlist_is_rejected(repos):
    source, target, sha = repos
    write(source, "service/.env", "private fixture")
    sha = commit(source)
    with pytest.raises(ValueError, match="Private/generated"):
        packaging.prepare(source, target, sha, apply=True)
    (source / "service/.env").unlink()
    (source / "service/link.py").symlink_to("api.py")
    sha = commit(source)
    with pytest.raises(ValueError, match="regular files"):
        packaging.prepare(source, target, sha, apply=True)


def test_tampered_manifest_cannot_delete_native_scaffold(repos):
    source, target, sha = repos
    write(target, "restopilot-release.json", json.dumps({"files": {"app/start_app_v2.sh": {}}}))
    commit(target)
    with pytest.raises(ValueError, match="unmanaged path"):
        packaging.prepare(source, target, sha, apply=True, allow_delete=True)
    assert (target / "app/start_app_v2.sh").exists()


def test_native_app_at_repository_root_and_ignored_stale_vendor(repos):
    source, target, sha = repos
    for path in sorted((target / "app").rglob("*")):
        if path.is_file():
            destination = target / path.relative_to(target / "app")
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.rename(destination)
    # Empty directories are irrelevant to Git and app-root detection.
    commit(target)
    write(target, ".gitignore", "backend/restopilot_app/service/\n")
    commit(target)
    write(target, "backend/restopilot_app/service/stale.py", "obsolete module")
    plan = packaging.prepare(source, target, sha)
    assert plan["delete"] == ["backend/restopilot_app/service/stale.py"]
    packaging.prepare(source, target, sha, apply=True, allow_delete=True)
    assert not (target / "backend/restopilot_app/service/stale.py").exists()
    assert (target / "deploy/restopilot-integration/service/api.py").exists()
    assert (target / "backend/restopilot_app/service/api.py").exists()


def test_staging_uses_exact_paths_and_includes_ignored_vendor_only(repos):
    source, target, sha = repos
    write(target, ".gitignore", "app/backend/restopilot_app/service/\n*.ignored\n")
    commit(target)
    write(target, "private.ignored", "local private file")
    with pytest.raises(ValueError, match="requires --apply"):
        packaging.prepare(source, target, sha, stage=True)
    report = packaging.prepare(source, target, sha, apply=True, stage=True)
    assert report["staged"]
    staged = set(git(target, "diff", "--cached", "--name-only").decode().splitlines())
    assert "app/backend/restopilot_app/service/api.py" in staged
    assert "restopilot-release.json" in staged
    assert "app/deploy/restopilot-integration/landing/index.html" in staged
    assert "app/deploy/restopilot-integration/landing/styles.css" in staged
    assert "private.ignored" not in staged
    assert all(n in {"restopilot-release.json", "app/deploy/SOURCE_COMMIT"} or n.startswith((
        "app/deploy/restopilot-integration/", "app/backend/restopilot_app/service/")) for n in staged)

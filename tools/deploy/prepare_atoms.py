#!/usr/bin/env python3
"""Package a reviewed development commit into the private Atoms deployment clone.

Reads files from a pinned commit of the development repository (never the working
tree), copies them into the managed paths of an ``restopilot-atoms-deploy`` clone,
and records the exact provenance in ``restopilot-release.json`` plus
``app/deploy/SOURCE_COMMIT``.

Managed paths (the ONLY paths this tool may create, modify or delete):

- ``app/deploy/restopilot-integration/``   (application copy: service, frontend, docs, pytest.ini)
- ``app/backend/restopilot_app/service/``  (generated backend mirror of ``service/``)
- ``restopilot-release.json``              (release manifest: per-file mode + sha256)
- ``app/deploy/SOURCE_COMMIT``             (pinned source commit)

Everything else in the deployment clone (``.atoms/``, the native launcher and
proxy, the dashboard asset plugin, the API shim, ASR forwarding, ``.private/``
archives) is native Atoms setup and is never touched.

By default the tool runs in dry-run mode and only prints the plan.
``--apply`` performs the writes; ``--stage`` additionally stages the managed
paths with git. It never commits or pushes, and it refuses to run against a
dirty target unless ``--allow-dirty`` is given.

Usage:
    python tools/deploy/prepare_atoms.py --target /path/to/restopilot-atoms-deploy
    python tools/deploy/prepare_atoms.py --target ... --apply --stage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_REPOSITORY = "https://github.com/fredericsetievi/AI-Agent-Resto-Pilot"
APPLICATION_PATH = "app/deploy/restopilot-integration"
VENDORED_SERVICE_PATH = "app/backend/restopilot_app/service"
SOURCE_COMMIT_PATH = "app/deploy/SOURCE_COMMIT"
MANIFEST_PATH = "restopilot-release.json"
SCHEMA_VERSION = 1

# Development-repository paths copied into the deployment application copy.
INCLUDE_PREFIXES = ("service/", "frontend/", "docs/", "supabase/", "tests/")
INCLUDE_FILES = ("pytest.ini", "requirements.txt")

# Deployment-clone paths this tool may write or delete.
MANAGED_PREFIXES = (APPLICATION_PATH + "/", VENDORED_SERVICE_PATH + "/")


def run_git(args, *, cwd, binary=False):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed in {cwd}:\n{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout if binary else result.stdout.decode().strip()


def resolve_source_commit(source: Path, commit: str) -> str:
    return run_git(["rev-parse", "--verify", commit + "^{commit}"], cwd=source)


def source_files(source: Path, commit: str) -> dict[str, tuple[str, bytes]]:
    """Return {dev_relative_path: (git_mode, content)} for the packaged set at commit."""
    raw = run_git(["ls-tree", "-r", "-z", commit], cwd=source, binary=True)
    files: dict[str, tuple[str, bytes]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        _mode, obj_type, _sha = meta.decode().split()
        if obj_type != "blob":
            continue
        path = name.decode()
        if not (path.startswith(INCLUDE_PREFIXES) or path in INCLUDE_FILES):
            continue
        content = run_git(["cat-file", "blob", f"{commit}:{path}"], cwd=source, binary=True)
        files[path] = ("0o" + _mode[3:], content)
    if not files:
        raise SystemExit("No packaged files found at the source commit; refusing to continue.")
    return files


def managed_target_paths(dev_path: str) -> list[str]:
    targets = [f"{APPLICATION_PATH}/{dev_path}"]
    if dev_path.startswith("service/"):
        # The generated backend mirror flattens: dev service/api.py -> app/backend/restopilot_app/service/api.py
        targets.append(f"{VENDORED_SERVICE_PATH}/{dev_path[len('service/'):]}")
    return targets


def plan_changes(target: Path, files: dict[str, tuple[str, bytes]]) -> tuple[dict, dict[str, list[str]]]:
    desired: dict[str, tuple[str, bytes]] = {}
    for dev_path, (mode, content) in sorted(files.items()):
        for target_path in managed_target_paths(dev_path):
            desired[target_path] = (mode, content)

    adds, updates, unchanged, stale = [], [], [], []
    for path, (mode, content) in sorted(desired.items()):
        existing = target / path
        if not existing.is_file():
            adds.append(path)
        elif existing.read_bytes() == content:
            unchanged.append(path)
        else:
            updates.append(path)

    # Files inside the application copy that the manifest no longer covers are
    # reported but left untouched, mirroring the original packaging behaviour.
    app_root = target / APPLICATION_PATH
    if app_root.is_dir():
        for existing in app_root.rglob("*"):
            if existing.is_file() and existing.relative_to(target).as_posix() not in desired:
                stale.append(existing.relative_to(target).as_posix())
    stale.sort()
    return desired, {"add": adds, "update": updates, "unchanged": unchanged, "stale (left untouched)": stale}


def build_manifest(files: dict[str, tuple[str, bytes]], commit: str) -> bytes:
    entries: dict[str, dict[str, str]] = {}
    for dev_path, (mode, content) in sorted(files.items()):
        for target_path in managed_target_paths(dev_path):
            entries[target_path] = {
                "mode": mode,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    entries[SOURCE_COMMIT_PATH] = {
        "mode": "0o644",
        "sha256": hashlib.sha256((commit + "\n").encode()).hexdigest(),
    }
    manifest = {
        "application_path": APPLICATION_PATH,
        "files": entries,
        "schema_version": SCHEMA_VERSION,
        "source_commit": commit,
        "source_repository": SOURCE_REPOSITORY,
        "vendored_service_path": VENDORED_SERVICE_PATH,
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"


def check_target(target: Path) -> None:
    required = [
        target / MANIFEST_PATH,
        target / APPLICATION_PATH,
        target / VENDORED_SERVICE_PATH,
        target / ".atoms",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit(
            "Refusing to continue: target does not look like a restopilot-atoms-deploy "
            f"clone (missing: {', '.join(missing)})."
        )
    # -z + --untracked-files=all: keep the XY status column intact on the first
    # record (a global strip would eat its leading space) and report untracked
    # files individually instead of collapsing whole directories to "?? dir/".
    raw = run_git(["status", "--porcelain", "--untracked-files=all", "-z"], cwd=target, binary=True)
    status_lines = [entry.decode() for entry in raw.split(b"\0") if entry]
    if status_lines:
        dirty_outside = [
            line
            for line in status_lines
            if not (
                line.startswith("?? .private/")
                or line.startswith("?? .import-stage/")
                or any(line[3:].startswith(prefix) for prefix in MANAGED_PREFIXES)
                or line[3:] in (MANIFEST_PATH, SOURCE_COMMIT_PATH)
            )
        ]
        if dirty_outside:
            raise SystemExit(
                "Refusing to continue: the target clone has uncommitted changes outside "
                "the managed paths:\n  " + "\n  ".join(dirty_outside[:10])
            )


def apply_plan(target: Path, desired: dict, manifest_bytes: bytes, commit: str, stage: bool) -> None:
    for path, (mode, content) in sorted(desired.items()):
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(int(mode, 8))
    (target / MANIFEST_PATH).write_bytes(manifest_bytes)
    (target / SOURCE_COMMIT_PATH).write_bytes((commit + "\n").encode())
    if stage:
        managed = [APPLICATION_PATH, VENDORED_SERVICE_PATH, MANIFEST_PATH, SOURCE_COMMIT_PATH]
        run_git(["add", "-A", "--", *managed], cwd=target)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source", type=Path, default=None, help="Development repository (default: this script's repo)"
    )
    parser.add_argument("--commit", default="HEAD", help="Development commit to package (default: HEAD)")
    parser.add_argument("--target", type=Path, required=True, help="restopilot-atoms-deploy clone")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run)")
    parser.add_argument("--stage", action="store_true", help="Also git-add the managed paths (implies --apply)")
    parser.add_argument("--allow-dirty", action="store_true", help="Permit a dirty target clone")
    args = parser.parse_args()

    if args.stage:
        args.apply = True
    source = (args.source or Path(__file__).resolve().parents[2]).resolve()
    target = args.target.resolve()
    if (target / "tools" / "deploy" / "prepare_atoms.py").exists():
        raise SystemExit(
            "Refusing to continue: target must be the deployment clone, not the development repository."
        )

    commit = resolve_source_commit(source, args.commit)
    files = source_files(source, commit)
    desired, changes = plan_changes(target, files)
    manifest_bytes = build_manifest(files, commit)

    if not args.allow_dirty:
        check_target(target)

    total = sum(len(v) for v in changes.values())
    print(f"Source commit : {commit}")
    print(f"Packaged files: {len(files)} -> {total} target paths")
    for kind in ("add", "update", "unchanged", "stale (left untouched)"):
        entries = changes[kind]
        print(f"  {kind:9s}: {len(entries)}")
        for path in entries[:15]:
            print(f"      {path}")
        if len(entries) > 15:
            print(f"      ... and {len(entries) - 15} more")
    if not args.apply:
        print("Dry run only; pass --apply (optionally --stage) to write the changes.")
        return
    apply_plan(target, desired, manifest_bytes, commit, args.stage)
    print(f"Applied. SOURCE_COMMIT={commit}")
    if args.stage:
        print("Staged the managed paths in the target clone. Review, then commit and push.")


if __name__ == "__main__":
    main()

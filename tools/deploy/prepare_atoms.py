#!/usr/bin/env python3
"""Export a pinned application commit into an existing native Atoms Git clone.

Dry-run by default. Does not commit, push, execute migrations or publish.
Only Python's standard library and Git are required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys


SOURCE_REPOSITORY = "https://github.com/fredericsetievi/AI-Agent-Resto-Pilot"
DIRECTORIES = ("service", "frontend", "landing", "tests", "docs", "supabase/migrations")
FILES = ("requirements.txt", "pytest.ini")
MANIFEST = "restopilot-release.json"


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def selected(name: str) -> bool:
    return name in FILES or any(name.startswith(d + "/") for d in DIRECTORIES)


def safe_name(name: str) -> None:
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or ".." in parts or "\\" in name:
        raise ValueError(f"Unsafe path: {name}")
    if any(p.startswith(".") or p in {"tmp", "data", "node_modules", "__pycache__"}
           for p in parts):
        raise ValueError(f"Private/generated path in application export: {name}")
    if PurePosixPath(name).suffix.lower() in {
        ".sqlite", ".sqlite3", ".db", ".pyc", ".log", ".pem", ".key", ".p12", ".pfx",
    }:
        raise ValueError(f"Private/generated file in application export: {name}")


def checked_path(root: Path, name: str) -> Path:
    safe_name(name)
    result = root / name
    current = result
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Refusing destination symlink: {name}")
        current = current.parent
    return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_commit(source: Path, ref: str) -> tuple[str, dict[str, tuple[bytes, int]]]:
    sha = git(source, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    files = {}
    for record in git(source, "ls-tree", "-rz", sha).split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not selected(name):
            continue
        safe_name(name)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Application export must contain regular files: {name}")
        files[name] = (git(source, "cat-file", "blob", oid), int(mode[-3:], 8))
    for required in (*FILES, "service/api.py", "frontend/index.html", "frontend/i18n.js"):
        if required not in files:
            raise ValueError(f"Pinned source is missing {required}")
    return sha, files


def prepare(source: Path, target: Path, ref: str, apply: bool = False,
            allow_delete: bool = False, stage: bool = False) -> dict:
    if stage and not apply:
        raise ValueError("--stage requires --apply")
    source = Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    target = target.resolve()
    if source == target or target in source.parents:
        raise ValueError("Use a separate clone of the Atoms-created repository")
    if Path(git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve() != target:
        raise ValueError("--target must be the root of the Atoms Git clone")
    # Nested repositories remain valid (e.g. this repository's ignored tmp/).
    if git(target, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Destination has local changes; preserve/commit them before packaging")
    sha, files = export_commit(source, ref)
    candidates = [root for root in (target / "app", target)
                  if (root / "backend/restopilot_app/sync_from_source.sh").is_file()]
    if len(candidates) != 1:
        raise ValueError("Expected one native Atoms app root (app/ or repository root)")
    app = candidates[0]
    for name in ("start_app_v2.sh", "frontend/vite.config.ts",
                 "frontend/plugins/restopilot-dashboard.ts", "frontend/restopilot-adapter/api-base.js",
                 "backend/routers/restopilot.py"):
        if not (app / name).is_file():
            raise ValueError(f"Native scaffold is missing {name}; do not use deploy/atoms as a replacement")
    prefix = app.relative_to(target).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    imported = prefix + "deploy/restopilot-integration/"
    vendored = prefix + "backend/restopilot_app/service/"
    source_marker = prefix + "deploy/SOURCE_COMMIT"

    def managed(name: str) -> bool:
        return (name == source_marker or
                (name.startswith(imported) and selected(name[len(imported):])) or name.startswith(vendored))

    desired = {imported + name: value for name, value in files.items()}
    desired.update({vendored + name[len("service/"):]: value
                    for name, value in files.items() if name.startswith("service/")})
    desired[source_marker] = ((sha + "\n").encode(), 0o644)
    tracked = {name.decode() for name in git(target, "ls-files", "-z").split(b"\0") if name}
    previous = {name for name in tracked if managed(name)}
    # Native repositories often ignore the generated backend service. Include
    # its existing files as well, so deleted modules cannot survive a release.
    for scope in (imported, vendored):
        directory = checked_path(target, scope.rstrip("/"))
        if directory.exists():
            for path in directory.rglob("*"):
                name = path.relative_to(target).as_posix()
                if managed(name) and (path.is_file() or path.is_symlink()):
                    previous.add(name)
    manifest_path = checked_path(target, MANIFEST)
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        for name in old["files"]:
            if not managed(name):
                raise ValueError(f"Manifest contains an unmanaged path: {name}")
            previous.add(name)
    # Validate the complete plan before any writes, including deletions.
    for name in previous | desired.keys():
        path = checked_path(target, name)
        if path.exists() and not path.is_file():
            raise ValueError(f"Destination is not a regular file: {name}")
    removed = sorted(previous - desired.keys())
    changed = sorted(name for name, (data, mode) in desired.items()
                     if not (target / name).exists() or (target / name).read_bytes() != data
                     or ((target / name).stat().st_mode & 0o777) != mode)
    manifest = {
        "schema_version": 1,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": sha,
        "application_path": imported.rstrip("/"),
        "vendored_service_path": vendored.rstrip("/"),
        "files": {name: {"sha256": digest(data), "mode": oct(mode)}
                  for name, (data, mode) in sorted(desired.items())},
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if not manifest_path.exists() or manifest_path.read_bytes() != manifest_data:
        changed.append(MANIFEST)
    report = {"source_commit": sha, "target": str(target), "application_files": len(files),
              "vendored_files": sum(n.startswith("service/") for n in files),
              "write": changed, "delete": removed, "applied": False}
    if not apply:
        return report
    if removed and not allow_delete:
        raise ValueError("Plan removes managed files; inspect the dry run, then use --allow-delete")
    for name in removed:
        checked_path(target, name).unlink()
    for name, (data, mode) in desired.items():
        path = checked_path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    manifest_path.write_bytes(manifest_data)
    for name, (data, mode) in desired.items():
        if checked_path(target, name).read_bytes() != data:
            raise ValueError(f"Verification failed: {name}")
    if stage:
        # Force-add only exact reviewed regular-file paths, never whole folders.
        # Generated vendor paths can be ignored by the native scaffold.
        paths = sorted(desired.keys() | {MANIFEST} | (set(removed) & tracked))
        subprocess.run(["git", "-C", str(target), "--literal-pathspecs", "add", "-f",
                        "--pathspec-from-file=-", "--pathspec-file-nul"],
                       input=b"\0".join(p.encode() for p in paths) + b"\0",
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    report["applied"] = True
    report["staged"] = stage
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path, help="Separate Atoms-connected Git clone")
    parser.add_argument("--source-ref", default="HEAD", help="Reviewed source commit (default: HEAD)")
    parser.add_argument("--apply", action="store_true", help="Write the reviewed plan (default: dry run)")
    parser.add_argument("--allow-delete", action="store_true", help="Permit reviewed managed-file removals")
    parser.add_argument("--stage", action="store_true", help="Stage exact managed paths after --apply; no commit/push")
    args = parser.parse_args()
    try:
        report = prepare(Path(__file__).resolve().parents[2], args.target, args.source_ref,
                         args.apply, args.allow_delete, args.stage)
    except (ValueError, OSError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        # Git stderr can include credential-bearing URLs; keep diagnostics local and sanitized.
        message = "Git operation failed; check repository paths and commit" if isinstance(
            exc, subprocess.CalledProcessError) else str(exc)
        print(f"Packaging stopped: {message}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

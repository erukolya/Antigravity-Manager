#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_URL = "https://github.com/lbjlaq/Antigravity-Manager.git"
OVERLAY_PATHS = [
    ".github/scripts/apply-fork-customizations.py",
    ".github/scripts/sync-upstream.py",
    ".github/workflows/sync-upstream.yml",
    ".fork/patch-manifest.json",
    ".fork/upstream-state.json",
]


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bump-revision", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    if run("git", "status", "--porcelain", capture=True):
        raise RuntimeError("working tree must be clean before upstream sync")

    fork_head = run("git", "rev-parse", "HEAD", capture=True)
    current_branch = run("git", "branch", "--show-current", capture=True)
    if current_branch != "main":
        raise RuntimeError(f"sync must run from main, got {current_branch!r}")

    remotes = run("git", "remote", capture=True).splitlines()
    if "upstream" not in remotes:
        run("git", "remote", "add", "upstream", UPSTREAM_URL)
    else:
        run("git", "remote", "set-url", "upstream", UPSTREAM_URL)

    run("git", "fetch", "--no-tags", "upstream", "main")
    upstream_head = run("git", "rev-parse", "FETCH_HEAD", capture=True)

    if not args.bump_revision:
        ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", upstream_head, fork_head],
                cwd=ROOT,
            ).returncode
            == 0
        )
        if ancestor:
            print("Upstream main is already contained in fork main; nothing to sync.")
            return

    with tempfile.TemporaryDirectory(prefix="antigravity-fork-overlay-") as tmp:
        overlay_root = Path(tmp)
        for rel in OVERLAY_PATHS:
            content = subprocess.check_output(
                ["git", "show", f"{fork_head}:{rel}"], cwd=ROOT
            )
            target = overlay_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        try:
            run("git", "checkout", "--detach", upstream_head)

            for rel in OVERLAY_PATHS:
                source = overlay_root / rel
                target = ROOT / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            cmd = ["python3", ".github/scripts/apply-fork-customizations.py"]
            if args.bump_revision:
                cmd.append("--bump-revision")
            run(*cmd)

            run("cargo", "check", "--manifest-path", "src-tauri/Cargo.toml")

            sandbox = subprocess.run(
                [
                    "git",
                    "grep",
                    "-n",
                    "daily-cloudcode-pa.sandbox.googleapis.com",
                    "--",
                    "src-tauri/src",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
            )
            if sandbox.returncode == 0:
                raise RuntimeError(
                    "sandbox endpoint still present in active runtime:\n" + sandbox.stdout
                )
            if sandbox.returncode not in (0, 1):
                raise RuntimeError("git grep failed while validating runtime")

            run("git", "add", "-A")
            tree = run("git", "write-tree", capture=True)
            message = f"chore(sync): merge upstream {upstream_head[:12]} with fork overlay"
            commit = run(
                "git",
                "commit-tree",
                tree,
                "-p",
                fork_head,
                "-p",
                upstream_head,
                "-m",
                message,
                capture=True,
            )
            run("git", "update-ref", "refs/heads/main", commit, fork_head)
            run("git", "checkout", "main")

            if not args.no_push:
                run("git", "push", "origin", "main")

            print(f"Synthetic merge created: {commit}")
            print(f"parent1 fork: {fork_head}")
            print(f"parent2 upstream: {upstream_head}")
        except Exception:
            subprocess.run(["git", "reset", "--hard"], cwd=ROOT)
            subprocess.run(["git", "checkout", "main"], cwd=ROOT)
            raise


if __name__ == "__main__":
    main()

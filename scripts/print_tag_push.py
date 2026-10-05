#!/usr/bin/env python3
"""Print git tag and push commands for NotaNext releases.

Reads the canonical version from the root VERSION file (or CLI argument),
inspects the current git status and branch, and outputs the exact git
commands to tag and push the release.

Usage:
    python3 scripts/print_tag_push.py           # Show commands formatted for manual execution
    python3 scripts/print_tag_push.py --oneline # Output single combined command for copy-paste
    python3 scripts/print_tag_push.py --push-only # Output only the git push command
    python3 scripts/print_tag_push.py --tag-only  # Output only the git tag command
    python3 scripts/print_tag_push.py 1.3.0     # Explicit version override
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"

VERSION_FILES = [
    "VERSION",
    "bot.py",
    "docker-compose.yml",
    "README.md",
    "docs/CHANGELOG.md",
    "docs/USER-SPEC.md",
]


def get_version(override: str | None = None) -> str:
    """Get canonical semver version string without leading 'v'."""
    if override:
        return override.strip().lstrip("v")
    if not VERSION_FILE.exists():
        print(f"Error: {VERSION_FILE} not found.", file=sys.stderr)
        sys.exit(1)
    ver = VERSION_FILE.read_text().strip()
    if not ver:
        print(f"Error: {VERSION_FILE} is empty.", file=sys.stderr)
        sys.exit(1)
    return ver


def get_current_branch() -> str:
    """Return active git branch name, falling back to 'master'."""
    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        )
        branch = res.stdout.strip()
        return branch or "master"
    except Exception:
        return "master"


def check_tag_exists(tag_name: str) -> bool:
    """Check if an annotated or lightweight git tag exists locally."""
    try:
        res = subprocess.run(
            ["git", "tag", "-l", tag_name],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        )
        return bool(res.stdout.strip())
    except Exception:
        return False


def get_changed_files() -> list[str]:
    """Return modified or untracked repository files."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        )
        lines = res.stdout.strip().splitlines()
        files = []
        for line in lines:
            if len(line) >= 4:
                files.append(line[3:].strip())
        return files
    except Exception:
        return []


def is_head_bump_commit(version: str) -> bool:
    """Check if HEAD commit message matches version bump pattern."""
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        )
        subject = res.stdout.strip()
        return f"bump version to {version}" in subject or subject == f"v{version}"
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print git tag and push commands for NotaNext release workflow."
    )
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Optional version override (e.g. 1.3.0). Defaults to VERSION file.",
    )
    parser.add_argument(
        "--oneline",
        "-1",
        action="store_true",
        help="Print only the single chained command suitable for copy-paste.",
    )
    parser.add_argument(
        "--tag-only",
        action="store_true",
        help="Print only the git tag command.",
    )
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Print only the git push command.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote name (default: origin).",
    )
    args = parser.parse_args()

    version = get_version(args.version)
    tag_name = f"v{version}"
    branch = get_current_branch()
    remote = args.remote

    tag_exists = check_tag_exists(tag_name)
    head_is_bump = is_head_bump_commit(version)
    changed = get_changed_files()

    # Identify if version files have uncommitted changes
    has_uncommitted_version_files = any(f in changed for f in VERSION_FILES)

    cmd_add = f"git add {' '.join(VERSION_FILES)}"
    cmd_commit = f'git commit -m "chore: bump version to {version}"'
    cmd_tag = f'git tag -a {tag_name} -m "Release {tag_name}"'
    cmd_push = f"git push {remote} {branch} && git push {remote} {tag_name}"

    # Build one-liner based on repository state
    chain = []
    if has_uncommitted_version_files or (not head_is_bump and changed):
        chain.extend([cmd_add, cmd_commit])
    if not tag_exists:
        chain.append(cmd_tag)
    chain.append(cmd_push)
    oneline_cmd = " && ".join(chain)

    if args.oneline:
        print(oneline_cmd)
        return

    if args.tag_only:
        print(cmd_tag)
        return

    if args.push_only:
        print(cmd_push)
        return

    # Formatted human output
    print("=" * 64)
    print(f"  NotaNext Git Tag & Push Helper — Release {tag_name}")
    print("=" * 64)
    print(f"  Target Version : {version}")
    print(f"  Git Tag        : {tag_name}")
    print(f"  Git Branch     : {branch}")
    print(f"  Remote         : {remote}")
    print(f"  Tag Exists?    : {'Yes' if tag_exists else 'No (will be created)'}")
    print(f"  Uncommitted?   : {'Yes' if has_uncommitted_version_files else 'No (clean or committed)'}")
    print("-" * 64)

    print("\nStep-by-step commands to run manually:\n")
    step = 1
    if has_uncommitted_version_files or (not head_is_bump and changed):
        print(f"  {step}. Stage version files:")
        print(f"     {cmd_add}\n")
        step += 1
        print(f"  {step}. Commit release changes:")
        print(f"     {cmd_commit}\n")
        step += 1

    if not tag_exists:
        print(f"  {step}. Create annotated tag:")
        print(f"     {cmd_tag}\n")
        step += 1
    else:
        print(f"  (Note: Tag {tag_name} already exists locally)\n")

    print(f"  {step}. Push commit and tag to trigger CI build & release:")
    print(f"     {cmd_push}\n")

    print("-" * 64)
    print("One-liner (copy-paste in one go):\n")
    print(f"  {oneline_cmd}\n")
    print("=" * 64)


if __name__ == "__main__":
    main()

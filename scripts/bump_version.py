#!/usr/bin/env python3
"""Automated version control & synchronization tool for NotaNext.

Single Source of Truth: VERSION file at the repository root.

Usage:
    python3 scripts/bump_version.py current
    python3 scripts/bump_version.py check
    python3 scripts/bump_version.py patch [--dry-run] [--git-commit] [--git-tag]
    python3 scripts/bump_version.py minor [--dry-run] [--git-commit] [--git-tag]
    python3 scripts/bump_version.py major [--dry-run] [--git-commit] [--git-tag]
    python3 scripts/bump_version.py set 1.3.0 [--dry-run] [--git-commit] [--git-tag]
"""

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"
BOT_PY = ROOT_DIR / "bot.py"
DOCKER_COMPOSE = ROOT_DIR / "docker-compose.yml"
README_MD = ROOT_DIR / "README.md"
CHANGELOG_MD = ROOT_DIR / "docs" / "CHANGELOG.md"
USER_SPEC_MD = ROOT_DIR / "docs" / "USER-SPEC.md"

VERSION_FILES = [
    "VERSION",
    "bot.py",
    "docker-compose.yml",
    "README.md",
    "docs/CHANGELOG.md",
    "docs/USER-SPEC.md",
]

SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def read_version() -> str:
    """Read the canonical version from the single source of truth (VERSION file)."""
    if not VERSION_FILE.exists():
        raise FileNotFoundError(f"Missing single source of truth file: {VERSION_FILE}")
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        ver = f.read().strip()
    if not ver:
        raise ValueError(f"Empty version in {VERSION_FILE}")
    return ver


def parse_semver(version_str: str) -> tuple[int, int, int, str]:
    """Parse a semver string into (major, minor, patch, prerelease)."""
    match = SEMVER_PATTERN.match(version_str.strip())
    if not match:
        raise ValueError(
            f"Invalid semver: {version_str!r}. Expected format: MAJOR.MINOR.PATCH (e.g. 1.2.0)"
        )
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease or ""


def calculate_next_version(current: str, bump_type: str) -> str:
    """Calculate the next semver version string."""
    major, minor, patch, _ = parse_semver(current)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Unknown bump type: {bump_type}")


def update_file(path: Path, new_content: str, dry_run: bool = False) -> None:
    """Write updated content to a file, respecting dry-run."""
    if dry_run:
        print(f"  [dry-run] Would update: {path.relative_to(ROOT_DIR)}")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"  ✓ Updated: {path.relative_to(ROOT_DIR)}")


def sync_version(new_version: str, dry_run: bool = False, message: str = "") -> None:
    """Synchronize all version references across the repository to new_version."""
    parse_semver(new_version)  # Validate semver format
    current_version = read_version()

    print(f"\nBumping version: {current_version} → {new_version} {'(dry-run)' if dry_run else ''}\n")

    # 1. VERSION file (Single Source of Truth)
    update_file(VERSION_FILE, f"{new_version}\n", dry_run)

    # 2. bot.py (fallback constant)
    if BOT_PY.exists():
        with open(BOT_PY, "r", encoding="utf-8") as f:
            content = f.read()
        updated = re.sub(
            r'DEFAULT_VERSION = "[^"]+"',
            f'DEFAULT_VERSION = "{new_version}"',
            content,
        )
        if updated == content:
            updated = re.sub(
                r'VERSION = "[^"]+"',
                f'VERSION = "{new_version}"',
                content,
            )
        update_file(BOT_PY, updated, dry_run)

    # 3. docker-compose.yml — `image:` is a plain tag, not wrapped in a
    #    DOCKER_IMAGE override (that indirection was removed; see CLAUDE.md).
    if DOCKER_COMPOSE.exists():
        with open(DOCKER_COMPOSE, "r", encoding="utf-8") as f:
            content = f.read()
        updated = re.sub(
            r"(image:\s*ghcr\.io/zr0aces/notanext:)\S+",
            rf"\g<1>{new_version}",
            content,
        )
        updated = re.sub(
            r'(VERSION:\s*")[^"]*(")',
            rf"\g<1>{new_version}\g<2>",
            updated,
        )
        update_file(DOCKER_COMPOSE, updated, dry_run)

    # 4. README.md
    if README_MD.exists():
        with open(README_MD, "r", encoding="utf-8") as f:
            content = f.read()
        updated = re.sub(
            r"(docker pull ghcr\.io/zr0aces/notanext:)\d+\.\d+\.\d+",
            rf"\g<1>{new_version}",
            content,
        )
        update_file(README_MD, updated, dry_run)

    # 5. docs/USER-SPEC.md
    if USER_SPEC_MD.exists():
        with open(USER_SPEC_MD, "r", encoding="utf-8") as f:
            content = f.read()
        updated = re.sub(
            r"(Version )\d+\.\d+\.\d+(\.?)",
            rf"\g<1>{new_version}\g<2>",
            content,
        )
        update_file(USER_SPEC_MD, updated, dry_run)

    # 6. docs/CHANGELOG.md
    if CHANGELOG_MD.exists():
        with open(CHANGELOG_MD, "r", encoding="utf-8") as f:
            content = f.read()
        header = f"## [{new_version}]"
        if header not in content:
            today = datetime.date.today().isoformat()
            new_entry = f"## [{new_version}] – {today}\n\n### Changed\n- Version bump to `{new_version}`.\n\n---\n\n"
            # Insert after the changelog title / header
            if content.startswith("# Changelog\n"):
                lines = content.split("\n")
                # find the first section header
                insert_idx = -1
                for i, line in enumerate(lines):
                    if line.startswith("## ["):
                        insert_idx = i
                        break
                if insert_idx != -1:
                    lines.insert(insert_idx, new_entry.rstrip("\n"))
                    updated = "\n".join(lines)
                else:
                    updated = content + "\n\n" + new_entry
            else:
                updated = new_entry + content
            update_file(CHANGELOG_MD, updated, dry_run)
        else:
            print(f"  ℹ CHANGELOG.md already has section for {new_version}")


def check_consistency() -> bool:
    """Verify all version references across the repo match the single source of truth."""
    version = read_version()
    errors = []

    print(f"Checking version consistency against single source of truth (VERSION: {version})...\n")

    # Check bot.py — the literal sync_version() actually writes
    if BOT_PY.exists():
        with open(BOT_PY, "r", encoding="utf-8") as f:
            text = f.read()
        if f'DEFAULT_VERSION = "{version}"' not in text:
            errors.append(f"bot.py's DEFAULT_VERSION does not match {version!r}")

    # Check docker-compose.yml — both the pulled image tag and the build arg
    if DOCKER_COMPOSE.exists():
        with open(DOCKER_COMPOSE, "r", encoding="utf-8") as f:
            text = f.read()
        if f"ghcr.io/zr0aces/notanext:{version}" not in text:
            errors.append(f"docker-compose.yml's image tag does not match {version!r}")
        if f'VERSION: "{version}"' not in text:
            errors.append(f"docker-compose.yml's VERSION build arg does not match {version!r}")

    # Check README.md
    if README_MD.exists():
        with open(README_MD, "r", encoding="utf-8") as f:
            text = f.read()
        if f"notanext:{version}" not in text:
            errors.append(f"README.md does not reference {version!r}")

    # Check docs/CHANGELOG.md
    if CHANGELOG_MD.exists():
        with open(CHANGELOG_MD, "r", encoding="utf-8") as f:
            text = f.read()
        if f"## [{version}]" not in text:
            errors.append(f"docs/CHANGELOG.md does not contain header '## [{version}]'")

    # Check docs/USER-SPEC.md
    if USER_SPEC_MD.exists():
        with open(USER_SPEC_MD, "r", encoding="utf-8") as f:
            text = f.read()
        if f"Version {version}" not in text:
            errors.append(f"docs/USER-SPEC.md does not contain 'Version {version}'")

    if errors:
        print("❌ Version consistency check FAILED:")
        for err in errors:
            print(f"  - {err}")
        return False
    else:
        print("✅ All files match the single source of truth version.")
        return True


def check_staged_index() -> list[str]:
    """Return list of staged files in git index."""
    res = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT_DIR,
    )
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def git_commit(version: str) -> None:
    """Create a git commit for the new version containing only version files."""
    commit_msg = f"chore: bump version to {version}"
    print(f"\nCreating git commit for v{version}...")
    subprocess.run(["git", "add", *VERSION_FILES], check=True, cwd=ROOT_DIR)
    subprocess.run(["git", "commit", "-m", commit_msg], check=True, cwd=ROOT_DIR)


def git_tag(version: str) -> None:
    """Create an annotated git tag for the new version."""
    tag_name = f"v{version}"
    tag_msg = f"Release {tag_name}"
    print(f"Creating git tag {tag_name}...")
    subprocess.run(["git", "tag", "-a", tag_name, "-m", tag_msg], check=True, cwd=ROOT_DIR)
    print(f"✓ Tagged {tag_name}")


def display_git_instructions(version: str, committed: bool = False, tagged: bool = False) -> None:
    """Display git tag and push command instructions to update git and trigger releases."""
    tag_name = f"v{version}"
    print("\n" + "─" * 60)
    if tagged and committed:
        print("🚀 Release commit and tag created successfully!")
        print("Run the following command to push to remote and trigger GitHub Actions build:")
        print("\n    git push origin master --tags")
        print(f"    # or: git push origin master && git push origin {tag_name}\n")
    elif committed:
        print("📌 Release commit created. Next steps to tag and push:")
        print(f"\n    git tag -a {tag_name} -m \"Release {tag_name}\"")
        print("    git push origin master --tags")
        print(f"    # or: git push origin master && git push origin {tag_name}\n")
    else:
        print("📌 Next steps to commit, tag, and push version update to git:")
        files_str = " ".join(VERSION_FILES)
        print(f"\n    git add {files_str}")
        print(f"    git commit -m \"chore: bump version to {version}\"")
        print(f"    git tag -a {tag_name} -m \"Release {tag_name}\"")
        print("    git push origin master --tags")
        print(f"    # or: git push origin master && git push origin {tag_name}\n")
    print("─" * 60)


def main() -> None:
    epilog = """\
hints & examples:
  python3 scripts/bump_version.py              # Default: bumps patch (e.g. 1.2.0 -> 1.2.1)
  python3 scripts/bump_version.py --dry-run    # Preview default patch bump without writing
  python3 scripts/bump_version.py minor        # Bump minor version (e.g. 1.2.0 -> 1.3.0)
  python3 scripts/bump_version.py major        # Bump major version (e.g. 1.2.0 -> 2.0.0)
  python3 scripts/bump_version.py set 1.3.0    # Set explicit version
  python3 scripts/bump_version.py check        # Verify consistency across all repository files
  python3 scripts/bump_version.py current      # Print current version from VERSION file
  python3 scripts/bump_version.py patch --git-commit --git-tag  # Bump patch and create git tag vX.Y.Z
"""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Preview changes without modifying files.")
    common.add_argument("--git-commit", action="store_true", default=argparse.SUPPRESS, help="Create a git commit for the version bump.")
    common.add_argument("--git-tag", action="store_true", default=argparse.SUPPRESS, help="Create an annotated git tag (vX.Y.Z).")

    parser = argparse.ArgumentParser(
        description="Automated version management tool for NotaNext (Single Source of Truth).\n"
                    "Default action: 'patch' (bumps patch version if no command is specified).",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", default=False, help="Preview changes without modifying files.")
    parser.add_argument("--git-commit", action="store_true", default=False, help="Create a git commit for the version bump.")
    parser.add_argument("--git-tag", action="store_true", default=False, help="Create an annotated git tag (vX.Y.Z).")

    subparsers = parser.add_subparsers(dest="command", help="Version command (default: patch)")

    # current
    subparsers.add_parser("current", help="Show current version from VERSION file.")

    # check
    subparsers.add_parser("check", help="Verify all repository files are synchronized with VERSION.")

    # patch / minor / major
    for bump_type in ("patch", "minor", "major"):
        subparsers.add_parser(
            bump_type,
            parents=[common],
            help=f"Bump {bump_type} version component{' [DEFAULT]' if bump_type == 'patch' else ''}."
        )

    # set <version>
    p_set = subparsers.add_parser("set", parents=[common], help="Set an explicit semver version string (e.g. 1.3.0).")
    p_set.add_argument("version", help="Explicit semver version string.")

    args = parser.parse_args()

    cmd = args.command or "patch"
    dry_run = getattr(args, "dry_run", False)
    do_commit = getattr(args, "git_commit", False)
    do_tag = getattr(args, "git_tag", False)

    if cmd == "current":
        print(read_version())
        return

    if cmd == "check":
        ok = check_consistency()
        sys.exit(0 if ok else 1)

    # If --git-tag is requested, it implies --git-commit
    if do_tag:
        do_commit = True

    # Validate git index before making any changes if git operations are requested
    if not dry_run and do_commit:
        staged = check_staged_index()
        version_files_set = set(VERSION_FILES)
        unrelated = [f for f in staged if f not in version_files_set]
        if unrelated:
            print(
                f"❌ Error: Git index contains unrelated staged files: {', '.join(unrelated)}.\n"
                "Refusing automatic version commit/tag to avoid committing unrelated changes.\n"
                "Commit or stash your changes first, or run without --git-commit/--git-tag.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Handle bumps
    current_ver = read_version()
    if cmd in ("patch", "minor", "major"):
        new_ver = calculate_next_version(current_ver, cmd)
    elif cmd == "set":
        new_ver = args.version
    else:
        parser.print_help()
        return

    sync_version(new_ver, dry_run=dry_run)

    committed = False
    tagged = False
    if not dry_run and do_commit:
        git_commit(new_ver)
        committed = True
        if do_tag:
            git_tag(new_ver)
            tagged = True

    display_git_instructions(new_ver, committed=committed, tagged=tagged)


if __name__ == "__main__":
    main()

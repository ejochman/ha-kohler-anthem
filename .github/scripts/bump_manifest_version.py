"""Increment the Home Assistant manifest version — or keep one set by hand.

With ``--keep-unreleased`` (the automatic release path), a manifest version that has no git
tag yet is printed unchanged: somebody set it on purpose, and it is released as written.
Only a version that is already tagged — the last release — is bumped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_VERSION = re.compile(r"^\d+(?:\.\d+)+$")


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _tags() -> list[str]:
    """Every release tag in the repository: plain dotted numbers, like ``0.24``."""
    out = subprocess.run(
        ["git", "tag", "--list"], check=True, capture_output=True, text=True
    ).stdout
    return [tag for tag in out.split() if _VERSION.match(tag)]


def _unreleased(version: str, tags: list[str]) -> bool:
    """True when ``version`` has no tag yet and is newer than every tag.

    A hand-set version that is not newer than the last release is a mistake — releasing it
    would publish an older number after a newer one — so it stops the release instead.
    """
    if version in tags:
        return False
    newest = max(tags, key=_key, default=None)
    if newest is not None and _key(version) <= _key(newest):
        raise ValueError(
            f"manifest.json says {version}, which is not newer than the latest release "
            f"{newest}. Set a higher version, or put back {newest} to have it bumped."
        )
    return True


def _bump_minor(version: str) -> str:
    """Return the next major.minor version, ignoring any patch segment.

    The minor segment supports 00-99 and is zero-padded to two digits (e.g. ``0.89``).
    Bumping ``0.99`` rolls the minor over to ``00`` and increments the major, yielding
    ``1.00``.
    """
    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"Expected semantic version with at least major.minor parts, got: {version}"
        )

    major, minor = (int(part) for part in parts[:2])
    if minor >= 99:
        major += 1
        minor = 0
    else:
        minor += 1
    return f"{major}.{minor:02d}"


def _bump_major(version: str) -> str:
    """Return the next major version with the minor reset to ``00``.

    Used for deliberate breaking-change releases via the release workflow's
    manual dispatch (``bump: major``); the automatic release-on-green path
    always bumps minor.
    """
    parts = version.split(".")
    if not parts or not parts[0].isdigit():
        raise ValueError(
            f"Expected semantic version with at least major.minor parts, got: {version}"
        )
    return f"{int(parts[0]) + 1}.00"


def main() -> int:
    """Update the manifest file in place and print the new version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("custom_components/kohler_anthem/manifest.json"),
    )
    parser.add_argument(
        "--bump",
        choices=("minor", "major"),
        default="minor",
        help="Bump type: minor (default, the automatic release path) or major",
    )
    parser.add_argument(
        "--keep-unreleased",
        action="store_true",
        help="Leave a manifest version with no git tag yet unchanged instead of bumping it",
    )
    args = parser.parse_args()
    manifest_path = args.manifest
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    current_version = manifest["version"]
    if args.keep_unreleased and _unreleased(current_version, _tags()):
        print(current_version)
        return 0
    bump = _bump_major if args.bump == "major" else _bump_minor
    next_version = bump(current_version)
    updated_text, replacements = re.subn(
        r'("version"\s*:\s*")([^"]+)(")',
        rf"\g<1>{next_version}\g<3>",
        manifest_text,
        count=1,
    )
    if replacements != 1:
        raise ValueError("Could not locate the manifest version field")

    manifest_path.write_text(updated_text, encoding="utf-8")
    print(next_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

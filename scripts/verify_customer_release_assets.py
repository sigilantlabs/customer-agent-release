#!/usr/bin/env python3
"""Fail a customer release when GitHub did not attach all signed assets."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


EXPECTED = frozenset({"install", "README.txt", "release.json", "release.sig"})


def verify(repo: str, tag: str, *, attempts: int = 5, delay: float = 2.0) -> bool:
    for attempt in range(attempts):
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo, "--json", "assets",
             "--jq", ".assets[].name"],
            capture_output=True, text=True, check=False,
        )
        assets = result.stdout.splitlines() if result.returncode == 0 else []
        if len(assets) == len(EXPECTED) and set(assets) == EXPECTED:
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if verify(args.repo, args.tag):
        return 0
    print(
        "Customer release has missing or unexpected assets; expected exactly "
        "install, README.txt, release.json, and release.sig",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

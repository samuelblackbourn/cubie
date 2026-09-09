#!/usr/bin/env python3
"""Fail if the companion endpoint's payload shape has drifted from what Cubie
consumes.

## Why this exists

`pa/office.py` parses `GET /api/companion/status`, and `pa/mood.py` decides from
its fields -- what is waiting, who is working, whether anyone is needed. A field
renamed upstream does not raise anything: the parse simply stops finding it, the
mood collapses to whatever the missing value defaults to, and Cubie quietly stops
reflecting the office while looking perfectly healthy. That is the failure this
guard exists to make loud.

It was written for the `bridge/` prototype, which consumed the same endpoint and
has since been removed; the character stack inherited both the payload and the
exposure. If anything the guard matters more now, because the mood is AMBIENT --
nobody asks for it, so nobody notices it going quiet.

Ported from `totem-k10/scripts/check_companion_contract.py`, which guards the same
endpoint for the K10. Same discipline, one addition -- see "Staleness" below.

Upstream source, in order of preference:
  1. $VO_COMPANION_STATUS  -- path to a local companionStatus.ts
  2. $VO_REPO/server/src/companion/companionStatus.ts
  3. gh api repos/<VO_SLUG>/contents/...  (Virtual-Office is private, so an
     unauthenticated raw.githubusercontent fetch cannot work)

## Staleness -- the lesson this copy adds

The K10's version trusts whatever is in `$VO_REPO`'s working tree. That tree can
be *behind its own remote*, and comparing against it then reports "in sync" while
proving nothing -- which is exactly what happened on 2026-09-05: a checkout 2.5
weeks stale passed cleanly while `main` had grown two fields.

So when `$VO_REPO` is a git checkout, this reports the commit it compared against,
and **exits 2 if that checkout is behind its remote**. A stale comparison is a
"could not check", not a pass. This is the same reasoning that made exit 2 distinct
in the first place: the dangerous answer is not "it changed", it is "I did not
really look".

Exit codes: 0 = in sync, 1 = drift, 2 = could not reach or trust upstream.

Regex extraction rather than a TS parse, on purpose -- dependency-free, and it
reads the declaration source, which is what a reviewer would diff.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "contract" / "companion-status.json"
UPSTREAM_PATH = "server/src/companion/companionStatus.ts"
VO_SLUG = os.environ.get("VO_SLUG", "samuelblackbourn/Virtual-Office")

IFACE = re.compile(r"export interface CompanionStatus \{(.*?)\n\}", re.S)
FIELD = re.compile(r"^  (\w+)\??:", re.M)
MOOD = re.compile(r"export type CompanionMood =([^;]+);", re.S)
TASK_STATUS = re.compile(r"export type CompanionFounderTaskStatus =([^;]+);", re.S)
QUOTED = re.compile(r"'([a-z0-9_-]+)'")


def fail_drift(msg: str) -> None:
    print(f"check-companion-contract: DRIFT -- {msg}", file=sys.stderr)
    sys.exit(1)


def fail_infra(msg: str) -> None:
    print(f"check-companion-contract: could not check -- {msg}", file=sys.stderr)
    sys.exit(2)


def git(repo: str, *args: str) -> str | None:
    """Run a read-only git command in `repo`, or None if it is not a checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip()


def describe_checkout(repo: str) -> str:
    """Name the commit compared against, and refuse a checkout behind its remote.

    Comparing against stale bytes and reporting "in sync" is worse than any drift
    this script can find, because it is indistinguishable from success.
    """
    head = git(repo, "rev-parse", "--short", "HEAD")
    if head is None:
        return ""  # not a git checkout; nothing to verify staleness against
    upstream = git(repo, "rev-parse", "--abbrev-ref", "HEAD@{upstream}") or "origin/main"
    behind = git(repo, "rev-list", "--count", f"HEAD..{upstream}")
    if behind and behind.isdigit() and int(behind) > 0:
        fail_infra(
            f"{repo} is at {head}, {behind} commit(s) behind {upstream}. "
            f"Comparing against a stale checkout proves nothing -- "
            f"run `git -C {repo} fetch && git -C {repo} checkout {upstream}` first."
        )
    return f" @ {head}"


def load_upstream() -> tuple[str, str]:
    explicit = os.environ.get("VO_COMPANION_STATUS")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            fail_infra(f"$VO_COMPANION_STATUS is set to {path}, which is not a file")
        return f"local file {path}", path.read_text()

    repo = os.environ.get("VO_REPO")
    if repo:
        path = Path(repo) / UPSTREAM_PATH
        if not path.is_file():
            fail_infra(f"$VO_REPO is set but {path} does not exist")
        return f"local checkout {path}{describe_checkout(repo)}", path.read_text()

    if not shutil.which("gh"):
        fail_infra(
            "no upstream source: set $VO_COMPANION_STATUS or $VO_REPO, or install "
            "the gh CLI (Virtual-Office is private, so a raw fetch cannot work)"
        )
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{VO_SLUG}/contents/{UPSTREAM_PATH}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        fail_infra(f"gh api failed: {(exc.stderr or '').strip()}")
    try:
        payload = json.loads(out)
        # GitHub wraps base64 at 60 chars; b64decode tolerates newlines only if
        # we strip them first. The sibling repo lost time to exactly this.
        text = base64.b64decode(payload["content"].replace("\n", "")).decode()
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        fail_infra(f"could not decode the gh api response: {exc}")
    return f"gh api {VO_SLUG}@{payload.get('sha', '?')[:12]}", text


def extract(text: str) -> dict:
    iface = IFACE.search(text)
    if not iface:
        fail_drift("could not find `export interface CompanionStatus` upstream at all")
    fields = set(FIELD.findall(iface.group(1)))
    if not fields:
        fail_drift("CompanionStatus was found but no fields could be read from it")

    mood = MOOD.search(text)
    if not mood:
        fail_drift("could not find `export type CompanionMood` upstream")
    moods = QUOTED.findall(mood.group(1))

    status = TASK_STATUS.search(text)
    if not status:
        fail_drift("could not find `export type CompanionFounderTaskStatus` upstream")
    statuses = QUOTED.findall(status.group(1))

    caps = {}
    for name in (
        "COMPANION_MAX_PENDING",
        "COMPANION_MAX_ONE_LINE",
        "COMPANION_MAX_FOUNDER_TASKS",
        "COMPANION_MAX_TITLE",
    ):
        m = re.search(rf"export const {name} = (\d+);", text)
        if not m:
            fail_drift(f"upstream no longer exports {name}")
        caps[name] = int(m.group(1))
    return {"fields": fields, "moods": moods, "statuses": statuses, "caps": caps}


def main() -> None:
    if not CONTRACT.is_file():
        fail_infra(f"{CONTRACT} is missing")
    contract = json.loads(CONTRACT.read_text())
    origin, text = load_upstream()
    up = extract(text)

    want_fields = set(contract["fields"])
    missing = sorted(want_fields - up["fields"])
    added = sorted(up["fields"] - want_fields)
    problems = []
    if missing:
        problems.append(f"fields the firmware parses but upstream no longer has: {missing}")
    if added:
        problems.append(f"new upstream fields not in the contract: {added}")
    if up["moods"] != contract["mood_values"]:
        problems.append(f"mood values changed: {contract['mood_values']} -> {up['moods']}")
    if up["statuses"] != contract["founder_task_status_values"]:
        problems.append(
            f"founder task statuses changed: "
            f"{contract['founder_task_status_values']} -> {up['statuses']}"
        )
    for name, want in contract["caps"].items():
        if up["caps"][name] != want:
            problems.append(f"{name} changed: {want} -> {up['caps'][name]}")

    print(f"check-companion-contract: compared against {origin}")
    if problems:
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        fail_drift(
            f"{len(problems)} change(s). Re-read the endpoint, update the firmware's "
            "parser AND contract/companion-status.json together, then re-run."
        )
    print(
        f"check-companion-contract: OK -- {len(want_fields)} fields, "
        f"{len(up['moods'])} moods, {len(up['statuses'])} task statuses, "
        f"{len(up['caps'])} caps all match"
    )


if __name__ == "__main__":
    main()

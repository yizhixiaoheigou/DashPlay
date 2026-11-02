#!/usr/bin/env python3
"""The Android app's versionCode, derived from its version name (used by scripts/android-release.sh).

Android installs an update only when its versionCode is larger, and the app's own update check
(OnlineUpdate.newer) orders versions as MAJOR.MINOR.PATCH, then dev < alpha < beta < rc < the release,
then the pre-release number as a number. The code follows the same order:

    MAJOR * 10_000_000 + MINOR * 100_000 + PATCH * 1_000 + STAGE * 100 + N

with STAGE dev 1, alpha 2, beta 3, rc 4 (N 1-99) and the release 9 (N 0). 0.3.0-beta.1 is 300_301, above the
3001 the old formula gave the same build, so a phone that has it can still upgrade.

    python3 scripts/android_version.py 0.3.0-beta.1   ->  300301
"""
import re
import sys

STAGES = {"dev": 1, "alpha": 2, "beta": 3, "rc": 4}
RELEASE = 9
PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.(\d+))?")


def code(version):
    match = PATTERN.fullmatch(version)
    if not match:
        raise ValueError(f"version must be X.Y.Z or X.Y.Z-(dev|alpha|beta|rc).N, got {version!r}")
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    label, number = match.group(4), match.group(5)
    if label is None:
        stage, n = RELEASE, 0
    elif label in STAGES and 1 <= int(number) <= 99:
        stage, n = STAGES[label], int(number)
    else:
        raise ValueError(f"pre-release must be dev, alpha, beta or rc with a number 1-99, got {version!r}")
    if major > 99 or minor > 99 or patch > 99:
        raise ValueError(f"each of X.Y.Z must be at most 99, got {version!r}")
    return major * 10_000_000 + minor * 100_000 + patch * 1_000 + stage * 100 + n


if __name__ == "__main__":
    try:
        print(code(sys.argv[1]))
    except (IndexError, ValueError) as error:
        sys.exit(f"android_version.py: {error}")

#!/bin/sh
# Apply patches/series to a clean PlayPort checkout at the pinned upstream commit.
# Usage: sh scripts/apply-playport-patches.sh PLAYPORT_DIR
# Some patches were generated without context lines, hence --unidiff-zero.
set -eu
target=${1:?Usage: apply-playport-patches.sh PLAYPORT_DIR}
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
pinned=9a0882dd0ffe48e467b59d58b12d81391df55ade
if [ -d "$target/.git" ]; then
    head=$(git -C "$target" rev-parse HEAD)
    [ "$head" = "$pinned" ] || { echo "PlayPort HEAD is $head, expected $pinned" >&2; exit 1; }
    [ -z "$(git -C "$target" status --porcelain)" ] || { echo "PlayPort checkout is not clean" >&2; exit 1; }
fi
while read -r patch; do
    [ -n "$patch" ] || continue
    (cd "$target" && git apply --unidiff-zero --whitespace=nowarn "$here/patches/$patch")
    echo "applied $patch"
done < "$here/patches/series"

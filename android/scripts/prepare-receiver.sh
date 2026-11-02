#!/bin/sh
# Build Android from the same pinned upstream and ordered patches as N1/OpenWrt.
# Never reads or changes the modified research/playport working tree.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
pinned=9a0882dd0ffe48e467b59d58b12d81391df55ade
# The private tree keeps PlayPort in research/playport; the public README clones it to ./playport.
if [ -n "${DASHPLAY_PLAYPORT_GIT:-}" ]; then upstream=$DASHPLAY_PLAYPORT_GIT
elif [ -d "$repo/research/playport" ]; then upstream=$repo/research/playport
else upstream=$repo/playport
fi
git -C "$upstream" cat-file -e "$pinned^{commit}" 2>/dev/null || {
    echo "PlayPort $pinned not found in $upstream: git clone https://github.com/youcci/playport.git there," >&2
    echo "or set DASHPLAY_PLAYPORT_GIT to a clone that has it" >&2
    exit 1
}
mkdir -p "$here/build"
stage=$(mktemp -d "$here/build/receiver-stage.XXXXXX")
trap 'rm -rf "$stage"' EXIT
git -C "$upstream" archive "$pinned" > "$stage/upstream.tar"
mkdir "$stage/source"
tar -xf "$stage/upstream.tar" -C "$stage/source"
export GIT_CEILING_DIRECTORIES="$stage"
sh "$repo/scripts/apply-playport-patches.sh" "$stage/source"
(cd "$stage/source" && git apply --unidiff-zero --whitespace=nowarn "$here/patches/playport-android.patch")
(cd "$stage/source" && git apply --unidiff-zero --whitespace=nowarn "$here/patches/playport-management.patch")
(cd "$stage/source" && git apply --unidiff-zero --whitespace=nowarn "$here/patches/playport-usb.patch")
(cd "$stage/source" && git apply --unidiff-zero --whitespace=nowarn "$here/patches/playport-phones.patch")
printf '%s\n' "$pinned" > "$stage/source/android-upstream.txt"
# Only this script's generated directory is replaced.
if [ -e "$here/build/receiver" ]; then
    # Gradle creates the empty output directory before the first run (a fresh clone): that one may go too.
    if [ -n "$(ls -A "$here/build/receiver")" ]; then test -f "$here/build/receiver/android-upstream.txt"; fi
    rm -rf "$here/build/receiver"
fi
mv "$stage/source" "$here/build/receiver"
echo 'Android receiver prepared from patches/series + Android platform patch'

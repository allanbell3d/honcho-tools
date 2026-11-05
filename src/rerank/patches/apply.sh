#!/bin/sh
# Usage: apply.sh <honcho-source-tree>
set -eu
[ $# -eq 1 ] || { echo "usage: $0 <honcho-source-tree>" >&2; exit 2; }
patch="$(cd "$(dirname "$0")" && pwd)/summary-output-headroom.patch"
tree=$1
if ! git -C "$tree" apply --check "$patch"; then
  echo "patch does not apply cleanly to $tree" >&2
  exit 1
fi
git -C "$tree" apply "$patch"
echo "applied; files changed:"
git -C "$tree" apply --numstat "$patch" | cut -f3

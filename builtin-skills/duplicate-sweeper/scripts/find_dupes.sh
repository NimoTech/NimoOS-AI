#!/usr/bin/env bash
# Find exact-duplicate files in a directory tree by SHA-256.
#
# Output: groups of duplicate paths separated by blank lines. The first path
# in each group is the "keeper" (sorted lexicographically — typically the
# shortest/shallowest path). Lines after it are duplicates of the keeper.
#
# Usage: find_dupes.sh <root> [min_size_bytes]
#   <root>            directory to scan recursively
#   <min_size_bytes>  skip files smaller than this (default 1048576 = 1 MiB)
#
# Exit codes:
#   0  scan completed (may find zero dupes)
#   1  bad usage / root not a directory
set -euo pipefail

ROOT="${1:-}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
  echo "usage: find_dupes.sh <root> [min_size_bytes]" >&2
  exit 1
fi
MIN_SIZE="${2:-1048576}"

# Walk the tree, hash each large-enough regular file, sort by hash so groups
# cluster together, then emit groups with blank-line separators.
find "$ROOT" -type f -size "+${MIN_SIZE}c" \
     -not -path '*/Recycled/*' \
     -not -path '*/.*' \
     -print0 2>/dev/null |
  xargs -0 -n 64 sha256sum 2>/dev/null |
  sort |
  awk '
    {
      hash = $1
      # sha256sum prefix is "HASH  PATH" — 64 hex + 2 spaces = 66 chars
      path = substr($0, 67)
      if (hash == prev_hash) {
        if (!printed_keeper) {
          print prev_path
          printed_keeper = 1
        }
        print path
      } else {
        if (printed_keeper) print ""
        printed_keeper = 0
        prev_hash = hash
        prev_path = path
      }
    }
    END { if (printed_keeper) print "" }
  '

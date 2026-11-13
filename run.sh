#!/usr/bin/env bash
set -Eeuo pipefail
# Byte-order sorting: matches DuckDB's ORDER BY in the input files (required by sort -m) and is faster.
export LC_ALL=C

FOLDER="${1:-.}"
OUTPUT="$FOLDER/all.txt"

TMP_OUTPUT=$(mktemp "$FOLDER/.all.XXXXXXXX")
trap 'rm -f "$TMP_OUTPUT"' EXIT

FILE_COUNT=$(find "$FOLDER" -maxdepth 1 -type f \
    -name '*.txt' ! -name 'all.txt' -printf '.' | wc -c)

if ((FILE_COUNT == 0)); then
    echo "No input files found."
    exit 1
fi

echo "Merging $FILE_COUNT sorted files..."

find "$FOLDER" -maxdepth 1 -type f \
    -name '*.txt' ! -name 'all.txt' -print0 |
    sort -z |
    xargs -0 -r sort -m -u --batch-size=400 \
    > "$TMP_OUTPUT"

mv -f "$TMP_OUTPUT" "$OUTPUT"
trap - EXIT

echo "Finished!"
echo "Unique lines: $(wc -l < "$OUTPUT")"
echo "Output size: $(du -h "$OUTPUT" | cut -f1)"


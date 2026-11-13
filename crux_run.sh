#!/usr/bin/env bash
# Run the toplist query script for the CrUX country codes you type in, one country at a time:
#   query the chosen data type(s) -> upload to S3 -> notify Discord -> delete local folder.
# After the dates are given you are asked for the country codes and the data type:
#   1) query_name   2) email_related   3) both
# A data type's folder is deleted ONLY after every step for it succeeded, so nothing is lost.
#
# Usage:
#   ./crux_run.sh all                         # all available dates
#   ./crux_run.sh 2025-04-01                  # one specific date
#   ./crux_run.sh 2025-04-01 2025-04-30       # a date range
#
# Re-running the same command resumes: finished country/data-type pairs (listed in
# output/crux_done.txt as "cc/type") are skipped, and the query script skips days that
# are already on disk.

set -uo pipefail   # no "set -e": failures are handled per country below

# ----- Settings (can be overridden with environment variables) -----
QUERY_SCRIPT="${QUERY_SCRIPT:-src/openintel_scraper/toplists_query.py}"
S3_BASE="${S3_BASE:-s3://openintel-clone-097027109922-us-east-1-an/raw/crux}"
WEBHOOK="${DISCORD_WEBHOOK:-https://discord.com/api/webhooks/1570512841587425480/-UIaHJ0dyGWJNet5VZfb03EsSszNyu-Nuq69IgDtFEE5fEvxwVQXNvsym7JrdqRQPlYI}"
DONE_FILE="output/crux_done.txt"

cd "$(dirname "$0")" || exit 1

# ----- Read the date arguments -----
DATE_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
usage() { echo "Usage: $0 all | YYYY-MM-DD | YYYY-MM-DD YYYY-MM-DD"; exit 1; }

if [[ $# -eq 1 && $1 == all ]]; then
    DATE_ANSWERS="all"
elif [[ $# -eq 1 && $1 =~ $DATE_RE ]]; then
    DATE_ANSWERS=$'specific\n'"$1"
elif [[ $# -eq 2 && $1 =~ $DATE_RE && $2 =~ $DATE_RE ]]; then
    DATE_ANSWERS=$'range\n'"$1"$'\n'"$2"
else
    usage
fi

# ----- Ask for country codes, e.g. "nl, de,us" -----
COUNTRIES=()
while (( ${#COUNTRIES[@]} == 0 )); do
    read -r -p "Country codes (comma separated, e.g. nl,de,us): " input || exit 1
    IFS=',' read -r -a codes <<< "${input,,}"   # lowercase, split on commas
    for cc in "${codes[@]}"; do
        cc=${cc//[[:space:]]/}                   # trim spaces
        [[ -z $cc ]] && continue
        if [[ $cc =~ ^[a-z]{2}$ || $cc == global ]]; then
            COUNTRIES+=("$cc")
        else
            echo "  Ignoring invalid country code: $cc"
        fi
    done
done

# ----- Ask for the data type(s) -----
TYPES=()
echo "Data type:"
echo "  1) query_name"
echo "  2) email_related"
echo "  3) both"
while (( ${#TYPES[@]} == 0 )); do
    read -r -p "> " choice || exit 1
    choice=${choice,,}
    choice=${choice//[[:space:]]/}
    case $choice in
        1|query_name)    TYPES=(query_name) ;;
        2|email_related) TYPES=(email_related) ;;
        3|both)          TYPES=(email_related query_name) ;;
        *) echo "  Invalid choice '$choice'. Enter 1, 2 or 3." ;;
    esac
done
TYPES_LABEL=$( (( ${#TYPES[@]} == 2 )) && echo both || echo "${TYPES[0]}" )

echo "Countries to process: ${COUNTRIES[*]}"
echo "Data types: ${TYPES[*]}"

# ----- Check that everything we need exists -----
for cmd in uv aws curl; do
    command -v "$cmd" >/dev/null || { echo "Missing command: $cmd"; exit 1; }
done
for file in run.sh "$QUERY_SCRIPT"; do
    [[ -f $file ]] || { echo "Missing file: $file"; exit 1; }
done
mkdir -p output
touch "$DONE_FILE"

# ----- Helpers -----

# Send a one-line message to Discord. A failed message only prints a warning.
notify() {
    echo ">> $1"
    curl -fsS -o /dev/null -H "Content-Type: application/json" \
        -d "{\"content\": \"$1\"}" "$WEBHOOK" \
        || echo "WARNING: Discord notification failed"
}

# True if folder $1 contains at least one file named like $2.
has_files() {
    [[ -n $(find "$1" -type f -name "$2" 2>/dev/null | head -n 1) ]]
}

# True if country $1 is done for data type $2. A bare "cc" line (older runs) means both are done.
is_done() {
    grep -qx -e "$1/$2" -e "$1" "$DONE_FILE"
}

# Run the query script for country $1 and data type $2 by typing the menu answers for it.
query() {
    printf '%s\n' crux "$DATE_ANSWERS" "$2" "$1" y | uv run "$QUERY_SCRIPT"
}

# Query email_related for country $1, upload the files, then notify.
process_email_related() {
    local cc=$1
    local dir="output/crux/$cc/email_related"

    query "$cc" email_related || return 1
    if has_files "$dir" '*.parquet'; then
        aws s3 cp "$dir/" "$S3_BASE/$cc/email_related" --recursive || return 1
        notify "crux [$cc]: email_related uploaded to S3"
    else
        notify "crux [$cc]: no email_related data for this date selection"
    fi
}

# Query query_name for country $1, merge the files with run.sh, upload all.txt, then notify.
process_query_name() {
    local cc=$1
    local dir="output/crux/$cc/query_name"

    query "$cc" query_name || return 1
    if has_files "$dir" '*.txt'; then
        bash run.sh "$dir" || return 1
        aws s3 cp "$dir/all.txt" "$S3_BASE/$cc/query_name/all.txt" || return 1
        local lines
        lines=$(wc -l < "$dir/all.txt")
        notify "crux [$cc]: query_name merged ($lines unique names) and uploaded to S3"
    else
        notify "crux [$cc]: no query_name data for this date selection"
    fi
}

# All selected data types for one country. A data type's folder is deleted only after
# all of its steps succeeded; any failing step returns 1 and keeps the folder.
process_country() {
    local cc=$1
    local dir="output/crux/$cc"

    for type in "${TYPES[@]}"; do
        if is_done "$cc" "$type"; then
            echo "[$cc] $type already done, skipping"
            continue
        fi
        echo "===== [$cc] $type ====="
        "process_$type" "$cc" || return 1

        # Everything for this type is safely in S3: free up disk space and remember it.
        rm -rf "${dir:?}/$type"
        echo "$cc/$type" >> "$DONE_FILE"
    done
    rmdir "$dir" 2>/dev/null   # only removed when nothing else is left in it
    return 0
}

# ----- Main loop -----
current=""
trap 'notify "crux run interrupted at [$current]; its folder was kept"; exit 130' INT TERM

ok=0
failed=()
for cc in "${COUNTRIES[@]}"; do
    all_done=1
    for type in "${TYPES[@]}"; do
        is_done "$cc" "$type" || { all_done=0; break; }
    done
    if (( all_done )); then
        echo "[$cc] already done, skipping"
        continue
    fi

    current=$cc
    if process_country "$cc"; then
        ok=$((ok + 1))
    else
        failed+=("$cc")
        notify "crux [$cc]: FAILED - local folder kept, re-run to retry"
    fi
done

if (( ${#failed[@]} > 0 )); then
    notify "crux run finished ($TYPES_LABEL): $ok done, ${#failed[@]} failed (${failed[*]})"
    exit 1
fi
notify "crux run finished ($TYPES_LABEL): $ok countries done, no failures"

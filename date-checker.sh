
#!/usr/bin/env bash

folder="$1"
start_date="$2"
end_date="$3"

months=(
    january february march april may june
    july august september october november december
)

for year in $(seq "${start_date:0:4}" "${end_date:0:4}"); do
    year_start="$year-01-01"
    year_end="$year-12-31"

    [[ "$year_start" < "$start_date" ]] && year_start="$start_date"
    [[ "$year_end" > "$end_date" ]] && year_end="$end_date"

    year_found=false

    # Check whether the year has any files in the requested range.
    for ((m=1; m<=12; m++)); do
        month=$(printf "%02d" "$m")
        month_start="$year-$month-01"

        [[ "$month_start" < "$year_start" ]] && continue
        [[ "$month_start" > "$year_end" ]] && continue

        month_end=$(date -d "$month_start +1 month -1 day" +%Y-%m-%d)

        [[ "$month_end" > "$year_end" ]] && month_end="$year_end"

        for ((d=1; d<=31; d++)); do
            day=$(printf "%02d" "$d")
            current="$year-$month-$day"

            [[ "$current" < "$year_start" ]] && continue
            [[ "$current" > "$month_end" ]] && continue

            if [[ -f "$folder/$current.txt" ]]; then
                year_found=true
                break 2
            fi
        done
    done

    if [[ "$year_found" == false ]]; then
        echo "[$year] year not found" >&2
        continue
    fi

    all_months_found=true

    # Check each month.
    for ((m=1; m<=12; m++)); do
        month=$(printf "%02d" "$m")
        month_start="$year-$month-01"

        [[ "$month_start" < "$year_start" ]] && continue
        [[ "$month_start" > "$year_end" ]] && continue

        month_end=$(date -d "$month_start +1 month -1 day" +%Y-%m-%d)

        [[ "$month_end" > "$year_end" ]] && month_end="$year_end"

        missing=()
        month_found=false

        for ((d=1; d<=31; d++)); do
            day=$(printf "%02d" "$d")
            current="$year-$month-$day"

            [[ "$current" < "$year_start" ]] && continue
            [[ "$current" > "$month_end" ]] && continue

            # Skip invalid calendar dates (e.g. February 30).
            [[ "$(date -d "$current" +%Y-%m-%d 2>/dev/null)" != "$current" ]] && continue

            if [[ -f "$folder/$current.txt" ]]; then
                month_found=true
            else
                missing+=("$d")
            fi
        done

        if [[ "$month_found" == false ]]; then
            echo "[$year] ${months[m-1]} month not found" >&2
            all_months_found=false
            continue
        fi

        if [[ ${#missing[@]} -eq 0 ]]; then
            echo "[$year] all days found in ${months[m-1]}" >&2
        else
            echo "[$year] day $(IFS=', '; echo "${missing[*]}") missing in ${months[m-1]}" >&2
        fi
    done

    if [[ "$all_months_found" == true ]]; then
        echo "[$year] all months found" >&2
    fi
done

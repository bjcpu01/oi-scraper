"""Interactive extractor for OpenINTEL toplist data.

Queries the public OpenINTEL bucket directly over s3:// with DuckDB (no raw
objects are downloaded or stored) and writes, per source:

  * query_name     -> .txt of unique query names
  * email_related  -> .parquet of DMARC / email-looking TXT records

one output per day, to output/{source}/[{cc}/]{data_type}/{YYYY-MM-DD}.{ext}.
Days are queried one after the other. A day is written atomically (.tmp, then
rename), so a day that fails leaves no file; the run continues with the next
day. Every run is logged to logs/{source}_{timestamp}.log. Re-running the same
selection resumes: days that already have an output are skipped.
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

# --------------------------------------------------------------------------- #
# Static configuration (rarely changes)
# --------------------------------------------------------------------------- #

BUCKET = "openintel-public"
BASE_PREFIX = "fdns/basis=toplist"
ENDPOINT = os.environ.get("OPENINTEL_ENDPOINT", "object.openintel.nl")
REGION = "nl-utwente-tee"

OUTPUT_DIR = Path("output")
LOG_DIR = Path("logs")
DUCKDB_TMP_DIR = OUTPUT_DIR / ".duckdb_tmp"

# Published data can lag behind by up to this many days.
PUBLISH_LAG_DAYS = 2

# Script-level retry waits (seconds) after DuckDB's own HTTP retries give up.
RETRY_WAITS = [int(x) for x in os.environ.get("OPENINTEL_RETRY_WAITS", "30,60,120").split(",") if x.strip()]

DUCKDB_SETTINGS = {
    "s3_list_concurrency": 8,
    "http_client_pool_capacity": 8,
    "http_retries": 10,
    "http_retry_wait_ms": 1000,
    "http_retry_backoff": 2,
    "http_timeout": 120,
}


@dataclass(frozen=True)
class Source:
    name: str
    start: date
    end: date | None  # None = still active
    status: str
    has_cc: bool = False


SOURCES: dict[str, Source] = {
    s.name: s
    for s in (
        Source("alexa", date(2016, 1, 22), date(2023, 5, 15), "retired"),
        Source("crux", date(2025, 3, 17), None, "active", has_cc=True),
        Source("majestic", date(2025, 3, 17), None, "active"),
        Source("radar", date(2022, 10, 4), None, "active"),
        Source("tranco", date(2022, 8, 11), None, "active"),
        Source("umbrella", date(2019, 1, 14), None, "active"),
    )
}

CRUX_COUNTRY_CODES: frozenset[str] = frozenset("""
ad ae af ag ai al am ao ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm
bn bo bq br bs bt bw by bz ca cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy
cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd ge gf gg
gh gi gl global gm gn gp gq gr gt gu gw gy hk hn hr ht hu id ie il im in io iq
ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt
lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz
na nc ne nf ng ni nl no np nr nz om pa pe pf pg ph pk pl pm pr ps pt pw py qa
re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz
tc td tg th tj tl tm tn to tr tt tv tw tz ua ug us uy uz vc ve vg vi vn vu ws
xk ye yt za zm zw
""".split())

DATA_TYPES = {"query_name": "txt", "email_related": "parquet"}

EMAIL_COLUMNS = "query_type, query_name, response_name, response_type, txt_text, day, month, year, source"
EMAIL_FILTER = r"(query_name LIKE '\_dmarc.%' ESCAPE '\' OR txt_text LIKE '%@%') AND txt_text NOT NULL"

log = logging.getLogger("toplist_query")


# --------------------------------------------------------------------------- #
# Interactive menu
# --------------------------------------------------------------------------- #


class MenuAborted(Exception):
    pass


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise MenuAborted("no more input")


def warn(msg: str) -> None:
    if log.handlers:
        log.warning(msg)
    else:
        print(f"WARNING: {msg}")


def choose(title: str, options: list[str], labels: list[str] | None = None) -> str:
    labels = labels or options
    print(f"\n{title}")
    for i, label in enumerate(labels, 1):
        print(f"  {i}) {label}")
    while True:
        ans = ask("> ").lower()
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        if ans in options:
            return ans
        print(f"  Invalid choice '{ans}'. Enter 1-{len(options)} or a name.")


def parse_date(text: str) -> date | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def ask_date(prompt: str) -> date:
    while True:
        d = parse_date(ask(prompt))
        if d:
            return d
        print("  Invalid date. Use YYYY-MM-DD (e.g. 2024-03-01).")


def source_bounds(src: Source, today: date) -> tuple[date, date, date]:
    """(first date, hard last date, default 'all time' last date)."""
    if src.end:
        return src.start, src.end, src.end
    return src.start, today, today - timedelta(days=PUBLISH_LAG_DAYS)


def choose_dates(src: Source, today: date) -> tuple[date, date, str]:
    lo, hi, default_hi = source_bounds(src, today)
    end_label = src.end.isoformat() if src.end else f"present (data up to ~{default_hi})"
    mode = choose(
        f"Date selection for {src.name} (available {lo} -> {end_label}):",
        ["specific", "range", "all"],
        ["Specific date", "Range (from / to)", "All time"],
    )
    while True:
        if mode == "all":
            start, end = lo, default_hi
        elif mode == "specific":
            start = end = ask_date("Date (YYYY-MM-DD): ")
        else:
            start = ask_date("From (YYYY-MM-DD): ")
            end = ask_date("To   (YYYY-MM-DD): ")
            if start > end:
                print("  'From' must not be after 'To'.")
                continue

        if end < lo or start > hi:
            print(f"  {start} -> {end} does not overlap {src.name}'s data ({lo} -> {hi}). Try again.")
            if mode == "all":  # cannot happen, but avoid an infinite loop
                raise MenuAborted("no data range")
            continue
        if start < lo:
            warn(f"Start {start} is before {src.name} began; using {lo}.")
            start = lo
        if end > hi:
            reason = "retired" if src.end else "in the future"
            warn(f"End {end} is {reason} for {src.name}; using {hi}.")
            end = hi
        if not src.end and end > default_hi:
            warn(f"Data after {default_hi} may not be published yet; missing days will be skipped.")
        return start, end, mode


def parse_country_codes(text: str) -> tuple[list[str], list[str]]:
    if text.strip().lower() == "all":
        return sorted(CRUX_COUNTRY_CODES), []
    valid, invalid = [], []
    for tok in re.split(r"[,\s]+", text.strip().lower()):
        if not tok:
            continue
        bucket = valid if tok in CRUX_COUNTRY_CODES else invalid
        if tok not in bucket:
            bucket.append(tok)
    return valid, invalid


def choose_country_codes() -> list[str]:
    print("\nCountry codes: comma and/or space separated (e.g. 'nl, de us'), or 'all'.")
    while True:
        valid, invalid = parse_country_codes(ask("> "))
        if invalid:
            warn(f"Ignoring invalid country code(s): {', '.join(invalid)}")
        if valid:
            return valid
        print("  No valid country codes given. Try again.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def setup_logging(source: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{source}_{datetime.now():%Y%m%d-%H%M%S}"
    path, i = LOG_DIR / f"{stem}.log", 1
    while path.exists():
        path, i = LOG_DIR / f"{stem}_{i}.log", i + 1
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    log.addHandler(ch)
    log.propagate = False
    return path


def months_in(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def days_in_month(y: int, m: int, start: date, end: date) -> list[date]:
    last = calendar.monthrange(y, m)[1]
    return [d for d in (date(y, m, i) for i in range(1, last + 1)) if start <= d <= end]


def prefix_for(source: str, cc: str | None) -> str:
    p = f"{BASE_PREFIX}/source={source}"
    return f"{p}/country-code={cc}" if cc else p


def s3_month_glob(source: str, cc: str | None, y: int, m: int) -> str:
    return f"s3://{BUCKET}/{prefix_for(source, cc)}/year={y}/month={m:02d}/day=*/*.parquet"


def s3_day_glob(source: str, cc: str | None, d: date) -> str:
    return f"s3://{BUCKET}/{prefix_for(source, cc)}/year={d.year}/month={d.month:02d}/day={d.day:02d}/*.parquet"


def out_dir(source: str, cc: str | None, data_type: str) -> Path:
    base = OUTPUT_DIR / source
    if cc:
        base = base / cc
    return base / data_type


def remove_stale_tmp(source: str) -> None:
    """Delete .tmp files left under output/{source}/ by a hard kill mid-write."""
    for p in (OUTPUT_DIR / source).rglob("*.tmp"):
        if p.is_file():
            p.unlink(missing_ok=True)
            log.info(f"removed stale partial file {p}")


def sql_path(p: Path | str) -> str:
    return str(p).replace("'", "''")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for k, v in DUCKDB_SETTINGS.items():
        con.execute(f"SET {k} = {v}")
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        con.execute(f"SET memory_limit = '{int(mem * 0.7 / 2**20)}MB'")
    except (ValueError, OSError):
        pass
    DUCKDB_TMP_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{sql_path(DUCKDB_TMP_DIR)}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute(
        "CREATE OR REPLACE SECRET openintel (TYPE S3, REGION '{}', ENDPOINT '{}', URL_STYLE 'path')".format(REGION, ENDPOINT)
    )
    return con


def write_txt(con: duckdb.DuckDBPyConnection, query: str, dest: Path) -> int:
    """Stream single-column query results to dest (atomically). Returns line count."""
    tmp = dest.with_name(dest.name + ".tmp")
    n = 0
    try:
        res = con.execute(query)
        with tmp.open("w", encoding="utf-8") as f:
            while rows := res.fetchmany(100_000):
                for (name,) in rows:
                    if name is not None:
                        f.write(f"{name}\n")
                        n += 1
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return n


def write_parquet(con: duckdb.DuckDBPyConnection, query: str, dest: Path) -> int:
    """COPY query to a ZSTD parquet at dest (atomically). Returns row count."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        res = con.execute(f"COPY ({query}) TO '{sql_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)").fetchone()
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return int(res[0]) if res else 0


def day_query(data_type: str, src_glob: str) -> str:
    """Query over one day's parquet files."""
    rp = f"read_parquet('{sql_path(src_glob)}', hive_partitioning = true)"
    if data_type == "query_name":
        return f"SELECT DISTINCT query_name FROM {rp} ORDER BY query_name"
    return f"SELECT {EMAIL_COLUMNS} FROM {rp} WHERE {EMAIL_FILTER}"


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    done: int = 0
    skipped: int = 0
    missing: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"done={self.done} skipped={self.skipped} missing={self.missing} failed={len(self.failed)}"


def with_retries(label: str, fn):
    """Run fn(), retrying on DuckDB errors with script-level backoff."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            return fn()
        except duckdb.InterruptException:
            raise KeyboardInterrupt
        except RuntimeError as e:
            # DuckDB surfaces Ctrl-C during a query as RuntimeError("Query interrupted").
            if "interrupt" in str(e).lower():
                raise KeyboardInterrupt from e
            raise
        except duckdb.Error as e:
            msg = str(e).splitlines()[0][:500]
            if attempt == len(RETRY_WAITS):
                log.error(f"{label}: giving up after {attempt + 1} attempt(s): {msg}")
                raise
            wait = RETRY_WAITS[attempt]
            log.warning(f"{label}: attempt {attempt + 1} failed ({msg}); retrying in {wait}s")
            time.sleep(wait)


class Runner:
    def __init__(self, source: str, start: date, end: date, data_type: str, ccs: list[str | None]):
        self.source, self.start, self.end = source, start, end
        self.data_type, self.ccs = data_type, ccs
        self.ext = DATA_TYPES[data_type]
        self.stats = Stats()
        self.con = connect()

    def available_days(self, cc: str | None, y: int, m: int) -> set[int]:
        glob = s3_month_glob(self.source, cc, y, m)
        rows = with_retries(f"list {y}-{m:02d}", lambda: self.con.execute(f"SELECT file FROM glob('{glob}')").fetchall())
        days = set()
        for (f,) in rows:
            if mt := re.search(r"/day=(\d{2})/", f):
                days.add(int(mt.group(1)))
        return days

    def run(self) -> None:
        for cc in self.ccs:
            tag = f"[{cc}] " if cc else ""
            for y, m in months_in(self.start, self.end):
                self.run_month(cc, tag, y, m, days_in_month(y, m, self.start, self.end))

    def log_missing(self, tag: str, y: int, m: int, missing: list[date]) -> None:
        if missing:
            self.stats.missing += len(missing)
            days = ", ".join(f"{d.day:02d}" for d in missing)
            log.info(f"{tag}{y}-{m:02d}: no data upstream for day(s) {days}")

    def run_unit(self, label: str, query: str, dest: Path, as_txt: bool) -> bool:
        log.info(f"{label}: querying")
        t0 = time.monotonic()
        writer = write_txt if as_txt else write_parquet
        try:
            n = with_retries(label, lambda: writer(self.con, query, dest))
        except duckdb.Error:
            self.stats.failed.append(label)
            return False
        log.info(f"{label}: wrote {n:,} rows -> {dest} ({time.monotonic() - t0:.1f}s)")
        self.stats.done += 1
        return True

    def run_month(self, cc, tag, y, m, requested):
        """Query the requested days of one month, one day at a time."""
        odir = out_dir(self.source, cc, self.data_type)
        todo = []
        for d in requested:
            if (odir / f"{d}.{self.ext}").exists():
                log.info(f"{tag}{d}: skipped (exists)")
                self.stats.skipped += 1
            else:
                todo.append(d)
        if not todo:
            return
        try:
            avail = self.available_days(cc, y, m)
        except duckdb.Error:
            self.stats.failed.extend(f"{tag}{d}" for d in todo)
            return
        self.log_missing(tag, y, m, [d for d in todo if d.day not in avail])
        for d in todo:
            if d.day not in avail:
                continue
            odir.mkdir(parents=True, exist_ok=True)
            q = day_query(self.data_type, s3_day_glob(self.source, cc, d))
            self.run_unit(f"{tag}{d}", q, odir / f"{d}.{self.ext}", as_txt=self.ext == "txt")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    today = date.today()
    names = list(SOURCES)
    labels = [
        f"{s.name:<9} {s.start} -> {s.end or 'present':<10}  ({s.status}{', country codes' if s.has_cc else ''})"
        for s in SOURCES.values()
    ]
    try:
        print("OpenINTEL toplist extractor")
        src = SOURCES[choose("Source:", names, labels)]
        log_path = setup_logging(src.name)
        log.debug(f"log file: {log_path}")
        start, end, date_mode = choose_dates(src, today)
        data_type = choose(
            "Data type:",
            list(DATA_TYPES),
            ["query_name     (unique query names -> .txt)", "email_related  (DMARC / email records -> .parquet)"],
        )
        ccs: list[str | None] = choose_country_codes() if src.has_cc else [None]

        n_days = (end - start).days + 1
        cc_desc = "n/a" if ccs == [None] else (f"all ({len(ccs)})" if len(ccs) == len(CRUX_COUNTRY_CODES) else ", ".join(ccs))
        summary = (
            f"source={src.name} dates={start}..{end} ({date_mode}) data_type={data_type} "
            f"country_codes={cc_desc} outputs<={n_days * len(ccs)}"
        )
        print(f"\nAbout to run: {summary}")
        if ask("Proceed? [y/N] ").lower() not in ("y", "yes"):
            log.info(f"cancelled by user: {summary}")
            return 0
    except MenuAborted as e:
        msg = f"menu aborted: {e}"
        log.info(msg) if log.handlers else print(msg)
        return 1
    except KeyboardInterrupt:
        print()
        log.info("cancelled during menu") if log.handlers else None
        return 130

    log.info(f"run started: {summary}")
    log.info(f"endpoint={ENDPOINT} duckdb={duckdb.__version__} settings={DUCKDB_SETTINGS} retry_waits={RETRY_WAITS}")
    remove_stale_tmp(src.name)
    t0 = time.monotonic()
    runner = Runner(src.name, start, end, data_type, ccs)
    code = 0
    try:
        runner.run()
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        code = 130
    elapsed = timedelta(seconds=int(time.monotonic() - t0))
    log.info(f"run finished in {elapsed}: {runner.stats.summary()}")
    if runner.stats.failed:
        log.error("failed units: " + "; ".join(runner.stats.failed))
        code = code or 1
    log.info(f"log: {log_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())

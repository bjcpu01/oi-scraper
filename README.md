# openintel-scraper

## Viewing logs

Each run of `toplist_query.py` writes a log to `logs/{source}_{timestamp}.log`. To browse them:

1. Open `log_viewer.html` in a browser (double-click it; it needs no server and no install).
2. Click **Open logs folder** and pick `logs/`, or drag the folder onto the page. Files are parsed locally.

The viewer has three tabs:

- **Days**: one row per requested day, with status `done`, `missing` (no upstream data), `failed`, `skipped` or `incomplete` (the run stopped mid-query), plus the retry count and the first error. Click a row to see its log lines.
- **Lines**: every log line. Filter it by level (DEBUG/INFO/WARNING/ERROR).
- **Runs**: one row per log file, with its parameters and totals.

Filter by source, by **With warnings / Missing / Failed / …**, by data type, by data date and by free text. Click a column header to sort, and shift-click to add a secondary sort key. Turn on **Latest outcome per day** to collapse days that were re-run. The current view is kept in the URL hash, so you can bookmark it. **Export CSV** saves the filtered Days or Runs table.

The sidebar lists every loaded log file under a readable name (`tranco_20261108-223435.log` becomes *Tranco 2026 Nov 08 22:34:35*). Files are grouped by source, A→Z, then by the month of the run. Untick a source, a month or a single file to hide it from every tab. The bar above each table summarizes the current view; click a count there to filter by it.

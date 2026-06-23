# TDHCA Vacancy Clearinghouse — Market Analysis Tool

End-to-end MVP: a weekly scraper writes dated snapshots from the TDHCA
Vacancy Clearinghouse into a SQLite DB; a Streamlit dashboard reads from it.
The accumulating snapshots are the value-add over the live TDHCA site.

## Files

| File          | Purpose                                                        |
|---------------|----------------------------------------------------------------|
| `models.py`   | SQLAlchemy 2.0 models + upsert layer (Postgres-portable)       |
| `counties.py` | All 254 Texas counties                                         |
| `scrape.py`   | Scraper: search → detail → upsert dated snapshot               |
| `app.py`      | Streamlit dashboard                                            |

## Quick start

```bash
pip install requests beautifulsoup4 sqlalchemy streamlit pandas plotly

# 1. Scrape (start with a few counties to sanity-check, then go wide)
python scrape.py --counties Cameron Hidalgo Lubbock
python scrape.py                      # all 254 — a full run takes a while

# 2. Launch the dashboard
streamlit run app.py
```

Default DB is `sqlite:///tdhca.db`. Point at Postgres later with
`--db postgresql+psycopg://user:pass@host/db` (same flag on both scripts);
the models are written with portable column types so no schema change is
needed.

## Data-model decisions worth knowing

* **`unit_snapshots` is fed from the search-row bedroom cells**, not the
  detail page. That's deliberate: the search row carries the
  accessible / non-accessible split *and* the per-group vacancy totals, which
  is the weekly signal we're tracking. Per-bedroom unit counts are stored;
  vacancies are stored once per accessibility group under a synthetic `all`
  bedroom bucket, because the site reports one vacancy figure per group, not
  per bedroom. The dashboard's vacancy math uses those `all` rows.
* **`detail_units`** holds the detail page's sqft / rent / unit_type rows
  separately, since that table has no accessible flag and can't be reconciled
  cell-for-cell with the search row. Rent is "where available" — frequently
  empty, as noted in discovery.
* `program_participation`, `ami_tiers`, and `detail_units` are **replaced
  per project on each run** (cheap, avoids dedupe). `properties` is upserted
  (first_seen set once, last_seen bumped). `unit_snapshots` is append-only
  and **idempotent per day** — re-running the same day overwrites rather than
  duplicating.

## Scheduling the weekly run

**cron** (simplest, on a box that's always up):

```cron
# Sundays 03:00, log to a dated file
0 3 * * 0  cd /path/to/tdhca && /usr/bin/python3 scrape.py >> logs/$(date +\%Y\%m\%d).log 2>&1
```

**GitHub Action** (no server; commit the SQLite file back, or push to a
managed Postgres):

```yaml
# .github/workflows/weekly-scrape.yml
name: weekly-scrape
on:
  schedule:
    - cron: "0 3 * * 0"      # Sundays 03:00 UTC
  workflow_dispatch: {}
jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install requests beautifulsoup4 sqlalchemy
      - run: python scrape.py --db "${{ secrets.DATABASE_URL }}"
```

For a hosted DB, set `DATABASE_URL` as a repo secret and skip committing the
file. A **cloud function** (Cloud Run job / Lambda on an EventBridge weekly
schedule) is the same idea — point `--db` at managed Postgres.

Be polite regardless of host: the default 1.5s delay and descriptive
User-Agent are already set in `scrape.py`. Update the contact email in the
UA string before running for real.

## Next: map view via Census geocoding

To add a county/property map and per-capita supply density:

1. **Geocode addresses** with the free Census Bureau Geocoder
   (`https://geocoding.geo.census.gov/geocoder/locations/onelineaddress`,
   no key required). Batch up to 10k addresses per request via the batch
   endpoint. Cache lat/lon on the `properties` row (add `lat`/`lon` columns)
   so you geocode each project once, not every snapshot.
2. **Population denominators**: pull county population from the Census ACS
   5-year API (`https://api.census.gov/data/2022/acs/acs5`, table `B01003`).
   A free API key is recommended for volume. Join on county FIPS to compute
   affordable units per 1,000 residents.
3. **Render**: `st.map` for a quick point map, or Plotly choropleth keyed on
   county FIPS for the density view. Both drop straight into a new dashboard
   tab.

## Notes / caveats

* The parsers are confirmed accurate as of discovery. If TDHCA changes a
  table layout, the sub-table parsers key off header text
  (`"Program File Number Year"`, `"AMI Tier Number of Units"`,
  `"Unit Square Feet Unit Type Rent"`) and may need those strings updated.
* Scope is intentionally TDHCA-funded affordable housing in Texas only — no
  market-rate, sales comps, or ownership data.

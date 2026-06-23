"""
TDHCA Vacancy Clearinghouse scraper.

Run:  python scrape.py                # all 254 counties
      python scrape.py --counties Harris Dallas Bexar
      python scrape.py --db sqlite:///tdhca.db --delay 1.5

Writes a dated snapshot into the DB. Designed to be run weekly (cron /
GitHub Action / cloud function). Polite by default: descriptive UA,
~1.5s delay, single reused session, retry-with-backoff on transient errors.

Parsing
-------
The search-row parser and the detail/sub-table parsers are the verified
versions from project discovery. The one addition is `parse_search_row_units`,
which turns the 12 bedroom cells (6 non-accessible + 6 accessible counts, plus
their vacancy cells) into UnitSnapshot rows — this is the source of the
accessible/non-accessible weekly vacancy signal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

import models
from counties import TEXAS_COUNTIES

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = "https://hrc-ic.tdhca.state.tx.us/hrc"
SEARCH = f"{BASE}/VacancyClearinghouseSearchResults.m"
DETAIL = f"{BASE}/VacancyClearinghouseDetail.m"
DEFAULT_DELAY = 1.5
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # seconds: 2, 4, 8 ...

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scrape.log"),
    ],
)
log = logging.getLogger("tdhca")

session = requests.Session()
session.headers.update({
    "User-Agent": "TDHCA-MarketAnalysis/1.0 (contact: you@example.com)"
})

# Live TDHCA search rows have 19 cells (verified against Cameron County).
# Non-accessible side lists 7 bedroom columns; accessible side lists 6.
BEDROOMS_NONACC = ["efficiency", "1br", "2br", "3br", "4br", "5br", "6br+"]
BEDROOMS_ACC = ["efficiency", "1br", "2br", "3br", "4br", "5br+"]


# --------------------------------------------------------------------------- #
# HTTP with retry/backoff
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            wait = BACKOFF_BASE ** attempt
            log.warning("  %s %s failed (attempt %d/%d): %s — retrying in %.0fs",
                        method, url, attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise last_exc


# --------------------------------------------------------------------------- #
# Parsers (verified)
# --------------------------------------------------------------------------- #
def search_county(county: str) -> list[dict]:
    resp = _request("POST", SEARCH, data={
        "city": "", "county": county, "zip": "", "projectId": "",
    })
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []
    for a in soup.select('a[href*="VacancyClearinghouseDetail"]'):
        m = re.search(r"projectId=(\d+)", a.get("href", ""))
        if not m:
            continue
        tr = a.find_parent("tr")
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        # cell[0] mashes name + full address together; the project link's own
        # text is the clean property name, so use it to split them.
        link_name = a.get_text(" ", strip=True)
        raw0 = cells[0] if cells else ""
        addr = raw0
        if link_name and raw0.startswith(link_name):
            addr = raw0[len(link_name):].strip()
        addr = re.sub(r"\s+", " ", addr.replace("\xa0", " ")).strip()
        phone = cells[18] if len(cells) > 18 else ""
        rows.append({
            "project_id": int(m.group(1)),
            "name": link_name or raw0,
            "address": addr,
            "phone": phone,
            "raw_cells": cells,
        })
    return rows


def parse_search_row_units(row: dict) -> list[dict]:
    """
    Turn the verified 19-cell search row into UnitSnapshot-shaped dicts.

    Cell layout (0-indexed), confirmed against live Cameron County data:
      0  name + address (mashed together)
      1  #30%-income-restricted units
      2  disaster-housing flag
      3  #811 units
      4-10  non-accessible bedroom unit counts (eff,1,2,3,4,5,6+)  [7 cols]
      11    non-accessible vacancies (single combined cell)
      12-17 accessible bedroom unit counts (eff,1,2,3,4,5+)        [6 cols]
      18    phone

    The site reports one vacancy figure for the non-accessible group (cell 11).
    There is no separate accessible-vacancy column in the live layout, so we
    record the group vacancy under a synthetic 'all' bedroom bucket and leave
    per-bedroom vacancies NULL — storing only what the site actually provides.
    """
    cells = row["raw_cells"]
    out = []
    if len(cells) < 19:
        return out

    def _int(x):
        x = re.sub(r"\D", "", x or "")
        return int(x) if x else None

    # Non-accessible bedroom counts: cells 4..10
    for label, c in zip(BEDROOMS_NONACC, cells[4:11]):
        n = _int(c)
        if n:
            out.append({"bedroom_type": label, "accessible": False,
                        "num_units": n, "vacancies": None})
    # Non-accessible group vacancies: cell 11
    nav = _int(cells[11])
    if nav is not None:
        out.append({"bedroom_type": "all", "accessible": False,
                    "num_units": None, "vacancies": nav})
    # Accessible bedroom counts: cells 12..17
    for label, c in zip(BEDROOMS_ACC, cells[12:18]):
        n = _int(c)
        if n:
            out.append({"bedroom_type": label, "accessible": True,
                        "num_units": n, "vacancies": None})
    return out


def _kv(soup, label):
    node = soup.find(string=re.compile(rf"^\s*{re.escape(label)}\s*:?\s*$"))
    if node:
        sib = node.find_parent().find_next_sibling()
        if sib:
            return sib.get_text(" ", strip=True)
    return None


def _is_nodata(row):
    return all(c == "No Data Found" or c == "" for c in row)


def bedroom_bucket(unit_type: str) -> str:
    s = (unit_type or "").lower()
    if "effic" in s or "studio" in s:
        return "efficiency"
    m = re.search(r"(\d+)\s*bed", s)
    if m:
        n = int(m.group(1))
        return f"{n}br" if n < 5 else "5br+"
    return "unknown"


def parse_detail_subtables(soup, project_id):
    program, ami, units = [], [], []
    for tb in soup.find_all("table"):
        htr = tb.find("tr")
        if not htr:
            continue
        head = " ".join(htr.get_text(" ", strip=True).split())
        body = tb.find_all("tr")[1:]
        if "Program File Number Year" in head:
            for tr in body:
                c = [td.get_text(" ", strip=True).strip() for td in tr.find_all("td")]
                if len(c) >= 3 and not _is_nodata(c):
                    program.append({"program": c[0], "file_number": c[1],
                                    "year": int(c[2]) if c[2].isdigit() else None})
        elif "AMI Tier Number of Units" in head:
            for tr in body:
                c = [td.get_text(" ", strip=True).strip() for td in tr.find_all("td")]
                if len(c) >= 2 and c[0].isdigit():
                    ami.append({"ami_pct": int(c[0]),
                                "num_units": int(c[1]) if c[1].isdigit() else None})
        elif "Unit Square Feet Unit Type Rent" in head:
            for tr in body:
                c = [td.get_text(" ", strip=True).strip() for td in tr.find_all("td")]
                if len(c) < 5 or _is_nodata(c):
                    continue
                sqft = re.sub(r"\D", "", c[0])
                rent = re.sub(r"[^\d.]", "", c[2])
                units.append({
                    "sqft": int(sqft) if sqft else None,
                    "unit_type": c[1],
                    "bedroom_type": bedroom_bucket(c[1]),
                    "rent": float(rent) if rent else None,
                    "num_units": int(c[3]) if c[3].isdigit() else None,
                    "vacancies": int(c[4]) if c[4].isdigit() else None,
                })
    return {"program": program, "ami": ami, "units": units}


def _to_int(x):
    if x is None:
        return None
    d = re.sub(r"\D", "", str(x))
    return int(d) if d else None


def fetch_detail(project_id: int) -> dict:
    resp = _request("GET", DETAIL, params={"projectId": project_id})
    soup = BeautifulSoup(resp.text, "html.parser")
    detail = {
        "project_id": project_id,
        "type": _kv(soup, "Type"),
        "building_config": _kv(soup, "Building Configuration"),
        "dwelling_type": _kv(soup, "Dwelling Type"),
        "total_units": _to_int(_kv(soup, "Total Units")),
        "total_program_units": _to_int(_kv(soup, "Total Program Units")),
        "units_811": _to_int(_kv(soup, "Total 811 Units")),
        "mgmt_email": _kv(soup, "Management Company Email"),
        "address_line1": _kv(soup, "Line 1"),
        "address_line2": _kv(soup, "Line 2"),
        "city": _kv(soup, "City"),
        "zip": _kv(soup, "Zip"),
        "county": _kv(soup, "County"),
    }
    detail.update(parse_detail_subtables(soup, project_id))
    return detail


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(db_url: str, counties: list[str], delay: float) -> None:
    engine = models.get_engine(db_url)
    models.init_db(engine)
    today = dt.date.today()

    total_props = 0
    failed_counties = []

    for i, county in enumerate(counties, 1):
        log.info("[%d/%d] County: %s", i, len(counties), county)
        try:
            search_rows = search_county(county)
        except Exception as exc:
            log.error("  search failed for %s: %s", county, exc)
            failed_counties.append(county)
            continue

        log.info("  %d properties found", len(search_rows))
        time.sleep(delay)

        for row in search_rows:
            pid = row["project_id"]
            try:
                detail = fetch_detail(pid)
            except Exception as exc:
                log.error("  detail failed for project_id=%s (%s): %s",
                          pid, county, exc)
                continue

            with models.Session(engine) as session:
                try:
                    prop = {
                        "project_id": pid,
                        "name": row.get("name"),
                        "mgmt_phone": row.get("phone"),
                        **detail,
                    }
                    models.upsert_property(session, prop, today)
                    models.replace_program_participation(session, pid, detail["program"])
                    models.replace_ami_tiers(session, pid, detail["ami"])
                    models.replace_detail_units(session, pid, detail["units"])

                    for u in parse_search_row_units(row):
                        models.upsert_unit_snapshot(
                            session, pid, today,
                            u["bedroom_type"], u["accessible"],
                            u["num_units"], u["vacancies"],
                        )
                    session.commit()
                    total_props += 1
                except Exception as exc:
                    session.rollback()
                    log.error("  DB write failed for project_id=%s: %s", pid, exc)

            time.sleep(delay)

    log.info("Done. %d properties upserted. %d counties failed: %s",
             total_props, len(failed_counties), failed_counties or "none")


def main():
    ap = argparse.ArgumentParser(description="TDHCA Vacancy Clearinghouse scraper")
    ap.add_argument("--db", default="sqlite:///tdhca.db", help="SQLAlchemy DB URL")
    ap.add_argument("--counties", nargs="*", help="Subset of counties (default: all 254)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds between requests")
    args = ap.parse_args()

    counties = args.counties if args.counties else TEXAS_COUNTIES
    run(args.db, counties, args.delay)


if __name__ == "__main__":
    main()

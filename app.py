"""
TDHCA Vacancy Clearinghouse — Streamlit dashboard.

Run:  streamlit run app.py
      streamlit run app.py -- --db sqlite:///tdhca.db

Reads only from the DB written by scrape.py. Five views:
  1. Vacancy rate by county / ZIP (latest snapshot)
  2. Unit-mix distribution (by bedroom type)
  3. Affordable-unit supply density (program units by county)
  4. AMI-tier and program-type breakdowns
  5. Trends — vacancy over time, once 2+ snapshots exist
"""

from __future__ import annotations

import sys

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select, func

import models
from models import (
    Property, UnitSnapshot, AmiTier, ProgramParticipation, DetailUnit,
)

# --------------------------------------------------------------------------- #
# DB URL: allow `streamlit run app.py -- --db <url>`
# --------------------------------------------------------------------------- #
def _db_url() -> str:
    # 1. Command-line flag (local use): streamlit run app.py -- --db <url>
    argv = sys.argv
    if "--db" in argv:
        i = argv.index("--db")
        if i + 1 < len(argv):
            return argv[i + 1]
    # 2. Streamlit secrets (cloud deploy): set DATABASE_URL in app secrets
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    # 3. Fallback: local SQLite file
    return "sqlite:///tdhca.db"


st.set_page_config(page_title="TDHCA Vacancy Clearinghouse", layout="wide")
engine = models.get_engine(_db_url())


@st.cache_data(ttl=600)
def load_frames(db_url: str):
    eng = models.get_engine(db_url)
    with eng.connect() as conn:
        props = pd.read_sql(select(Property), conn)
        snaps = pd.read_sql(select(UnitSnapshot), conn)
        ami = pd.read_sql(select(AmiTier), conn)
        prog = pd.read_sql(select(ProgramParticipation), conn)
        units = pd.read_sql(select(DetailUnit), conn)
    return props, snaps, ami, prog, units


props, snaps, ami, prog, units = load_frames(_db_url())

# --- TEMP DIAGNOSTIC: remove once data shows correctly ---
_url = _db_url()
_kind = "Neon/Postgres" if _url.startswith("postgresql") else ("SQLite (local fallback!)" if _url.startswith("sqlite") else _url[:20])
st.info(f"DEBUG — connected to: {_kind}  |  properties rows read: {len(props)}  |  snapshot rows read: {len(snaps)}")
# --- end diagnostic ---

st.title("TDHCA Vacancy Clearinghouse")
st.caption("Texas affordable-housing supply & vacancy — sourced from TDHCA, snapshotted weekly.")

if props.empty:
    st.warning("No data yet. Run `python scrape.py` to populate the database.")
    st.stop()

# Merge county onto snapshots for geo grouping
snaps = snaps.merge(props[["project_id", "county", "city", "zip"]], on="project_id", how="left")

snapshot_dates = sorted(snaps["snapshot_date"].dropna().unique())
latest = snapshot_dates[-1] if snapshot_dates else None
latest_snaps = snaps[snaps["snapshot_date"] == latest] if latest is not None else snaps.iloc[0:0]

# --------------------------------------------------------------------------- #
# Sidebar filters
# --------------------------------------------------------------------------- #
st.sidebar.header("Filters")
all_counties = sorted(props["county"].dropna().unique())
sel_counties = st.sidebar.multiselect("County", all_counties, default=all_counties)
if latest is not None:
    st.sidebar.write(f"Latest snapshot: **{latest}**")
    st.sidebar.write(f"Snapshots on file: **{len(snapshot_dates)}**")

prop_f = props[props["county"].isin(sel_counties)]
latest_f = latest_snaps[latest_snaps["county"].isin(sel_counties)]

# --------------------------------------------------------------------------- #
# Top-line metrics
# --------------------------------------------------------------------------- #
c1, c2, c3, c4 = st.columns(4)
c1.metric("Properties", f"{len(prop_f):,}")
c2.metric("Total program units", f"{int(prop_f['total_program_units'].fillna(0).sum()):,}")
c3.metric("811 units", f"{int(prop_f['units_811'].fillna(0).sum()):,}")
# vacancy: use 'all' bucket rows (the site's group vacancy totals)
vac_rows = latest_f[latest_f["bedroom_type"] == "all"]
total_vac = int(vac_rows["vacancies"].fillna(0).sum())
total_supply = int(prop_f["total_program_units"].fillna(0).sum())
rate = (total_vac / total_supply * 100) if total_supply else 0
c4.metric("Vacancy rate", f"{rate:.1f}%", help="Vacancies ÷ total program units, latest snapshot")

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Vacancy by geo", "Unit mix", "Supply density", "AMI & programs", "Trends"]
)

# --------------------------------------------------------------------------- #
# 1. Vacancy by county / ZIP
# --------------------------------------------------------------------------- #
with tab1:
    st.subheader("Vacancy rate by county")
    vac = (latest_f[latest_f["bedroom_type"] == "all"]
           .groupby("county", as_index=False)["vacancies"].sum())
    supply = prop_f.groupby("county", as_index=False)["total_program_units"].sum()
    geo = vac.merge(supply, on="county", how="right").fillna(0)
    geo["vacancy_rate_%"] = geo.apply(
        lambda r: (r["vacancies"] / r["total_program_units"] * 100)
        if r["total_program_units"] else 0, axis=1)
    geo = geo.sort_values("vacancy_rate_%", ascending=False)
    if not geo.empty:
        fig = px.bar(geo, x="county", y="vacancy_rate_%",
                     hover_data=["vacancies", "total_program_units"],
                     labels={"vacancy_rate_%": "Vacancy rate (%)"})
        st.plotly_chart(fig, width="stretch")
    st.dataframe(geo, width="stretch", hide_index=True)

    st.subheader("Vacancy by ZIP")
    zvac = (latest_f[latest_f["bedroom_type"] == "all"]
            .groupby("zip", as_index=False)["vacancies"].sum()
            .sort_values("vacancies", ascending=False))
    st.dataframe(zvac, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# 2. Unit mix
# --------------------------------------------------------------------------- #
with tab2:
    st.subheader("Unit-mix distribution (by bedroom type)")
    mix = latest_f[latest_f["bedroom_type"] != "all"].copy()
    mix_g = mix.groupby(["bedroom_type", "accessible"], as_index=False)["num_units"].sum()
    mix_g["accessible"] = mix_g["accessible"].map({True: "Accessible", False: "Non-accessible"})
    if not mix_g.empty:
        order = ["efficiency", "1br", "2br", "3br", "4br", "5br", "5br+", "6br+"]
        fig = px.bar(mix_g, x="bedroom_type", y="num_units", color="accessible",
                     category_orders={"bedroom_type": order}, barmode="group",
                     labels={"num_units": "Units", "bedroom_type": "Bedroom type"})
        st.plotly_chart(fig, width="stretch")
    st.dataframe(mix_g, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# 3. Supply density
# --------------------------------------------------------------------------- #
with tab3:
    st.subheader("Affordable-unit supply by county")
    sup = (prop_f.groupby("county", as_index=False)
           .agg(properties=("project_id", "count"),
                program_units=("total_program_units", "sum"),
                units_811=("units_811", "sum"))
           .sort_values("program_units", ascending=False))
    if not sup.empty:
        fig = px.bar(sup, x="county", y="program_units",
                     hover_data=["properties", "units_811"],
                     labels={"program_units": "Program units"})
        st.plotly_chart(fig, width="stretch")
    st.dataframe(sup, width="stretch", hide_index=True)
    st.caption("Density-per-capita needs Census population — see scheduling note in README.")

# --------------------------------------------------------------------------- #
# 4. AMI & program breakdowns
# --------------------------------------------------------------------------- #
with tab4:
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("AMI-tier breakdown")
        ami_f = ami[ami["project_id"].isin(prop_f["project_id"])]
        ami_g = ami_f.groupby("ami_pct", as_index=False)["num_units"].sum()
        if not ami_g.empty:
            ami_g["ami_pct"] = ami_g["ami_pct"].astype(str) + "% AMI"
            fig = px.pie(ami_g, names="ami_pct", values="num_units", hole=0.4)
            st.plotly_chart(fig, width="stretch")
        st.dataframe(ami_g, width="stretch", hide_index=True)
    with col_b:
        st.subheader("Program-type breakdown")
        prog_f = prog[prog["project_id"].isin(prop_f["project_id"])]
        prog_g = prog_f.groupby("program", as_index=False)["project_id"].nunique()
        prog_g = prog_g.rename(columns={"project_id": "properties"})
        if not prog_g.empty:
            fig = px.bar(prog_g.sort_values("properties", ascending=False),
                         x="program", y="properties",
                         labels={"properties": "Properties"})
            st.plotly_chart(fig, width="stretch")
        st.dataframe(prog_g, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# 5. Trends
# --------------------------------------------------------------------------- #
with tab5:
    st.subheader("Vacancy trend over time")
    if len(snapshot_dates) < 2:
        st.info("Trends appear once at least two weekly snapshots exist. "
                f"Currently {len(snapshot_dates)} snapshot(s) on file.")
    else:
        geo_filt = snaps[snaps["county"].isin(sel_counties) & (snaps["bedroom_type"] == "all")]
        trend = geo_filt.groupby("snapshot_date", as_index=False)["vacancies"].sum()
        fig = px.line(trend, x="snapshot_date", y="vacancies", markers=True,
                      labels={"vacancies": "Total vacancies", "snapshot_date": "Snapshot"})
        st.plotly_chart(fig, width="stretch")

        st.subheader("Vacancy trend by county")
        ct = geo_filt.groupby(["snapshot_date", "county"], as_index=False)["vacancies"].sum()
        fig2 = px.line(ct, x="snapshot_date", y="vacancies", color="county", markers=True)
        st.plotly_chart(fig2, width="stretch")

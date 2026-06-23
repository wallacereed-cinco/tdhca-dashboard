"""
SQLAlchemy 2.0 models for the TDHCA Vacancy Clearinghouse tool.

Postgres-portable: only generic column types (String, Integer, Numeric,
Boolean, Date) are used. The same models run on SQLite (dev) and Postgres
(prod) — change only the engine URL.

Schema notes / design decisions
--------------------------------
* `properties`            — one row per project_id, mutable, upserted.
* `program_participation` — LIHTC/BOND/etc., replaced wholesale per project
                            on each refresh (cheap, avoids dedupe logic).
* `ami_tiers`             — same replace-on-refresh strategy.
* `unit_snapshots`        — dated, append-only. Source = the SEARCH ROW
                            bedroom cells, which carry the accessible /
                            non-accessible split and the weekly vacancy
                            signal. UNIQUE(project_id, snapshot_date,
                            bedroom_type, accessible) makes a re-run on the
                            same day idempotent.
* `detail_units`          — from the detail page "General Unit Info" table:
                            sqft / rent / unit_type / vacancies. No accessible
                            flag exists there, so it is kept separate rather
                            than forced into unit_snapshots. Replaced per
                            project on refresh.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean, Date, Integer, Numeric, String, UniqueConstraint,
    create_engine, select, delete,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session,
)


class Base(DeclarativeBase):
    pass


class Property(Base):
    __tablename__ = "properties"

    project_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String)
    address_line1: Mapped[Optional[str]] = mapped_column(String)
    address_line2: Mapped[Optional[str]] = mapped_column(String)
    city: Mapped[Optional[str]] = mapped_column(String)
    county: Mapped[Optional[str]] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="TX")
    zip: Mapped[Optional[str]] = mapped_column(String)
    type: Mapped[Optional[str]] = mapped_column(String)
    building_config: Mapped[Optional[str]] = mapped_column(String)
    dwelling_type: Mapped[Optional[str]] = mapped_column(String)
    total_units: Mapped[Optional[int]] = mapped_column(Integer)
    total_program_units: Mapped[Optional[int]] = mapped_column(Integer)
    units_811: Mapped[Optional[int]] = mapped_column(Integer)
    mgmt_phone: Mapped[Optional[str]] = mapped_column(String)
    mgmt_email: Mapped[Optional[str]] = mapped_column(String)
    first_seen: Mapped[Optional[dt.date]] = mapped_column(Date)
    last_seen: Mapped[Optional[dt.date]] = mapped_column(Date)


class ProgramParticipation(Base):
    __tablename__ = "program_participation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    program: Mapped[Optional[str]] = mapped_column(String)
    file_number: Mapped[Optional[str]] = mapped_column(String)
    year: Mapped[Optional[int]] = mapped_column(Integer)


class AmiTier(Base):
    __tablename__ = "ami_tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    ami_pct: Mapped[Optional[int]] = mapped_column(Integer)
    num_units: Mapped[Optional[int]] = mapped_column(Integer)


class UnitSnapshot(Base):
    __tablename__ = "unit_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "snapshot_date", "bedroom_type", "accessible",
            name="uq_unit_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    snapshot_date: Mapped[dt.date] = mapped_column(Date, index=True)
    bedroom_type: Mapped[str] = mapped_column(String)
    accessible: Mapped[bool] = mapped_column(Boolean)
    num_units: Mapped[Optional[int]] = mapped_column(Integer)
    vacancies: Mapped[Optional[int]] = mapped_column(Integer)


class DetailUnit(Base):
    """Detail-page 'General Unit Info' rows. No accessible flag available here."""
    __tablename__ = "detail_units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    unit_type: Mapped[Optional[str]] = mapped_column(String)
    bedroom_type: Mapped[Optional[str]] = mapped_column(String)
    sqft: Mapped[Optional[int]] = mapped_column(Integer)
    rent: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    num_units: Mapped[Optional[int]] = mapped_column(Integer)
    vacancies: Mapped[Optional[int]] = mapped_column(Integer)


# --------------------------------------------------------------------------- #
# Engine / session helpers
# --------------------------------------------------------------------------- #

def get_engine(url: str = "sqlite:///tdhca.db"):
    # future=True is default in 2.0; echo off by default.
    return create_engine(url)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


# --------------------------------------------------------------------------- #
# Upsert layer
# --------------------------------------------------------------------------- #

def upsert_property(session: Session, data: dict, today: dt.date) -> None:
    """
    Insert or update one property row. Sets first_seen once, bumps last_seen
    every run. `data` keys map to Property columns; extras are ignored.
    """
    cols = {c.name for c in Property.__table__.columns}
    payload = {k: v for k, v in data.items() if k in cols}
    payload.pop("first_seen", None)
    payload.pop("last_seen", None)

    obj = session.get(Property, data["project_id"])
    if obj is None:
        obj = Property(**payload)
        obj.first_seen = today
        obj.last_seen = today
        session.add(obj)
    else:
        for k, v in payload.items():
            if k == "project_id":
                continue
            # don't overwrite a good value with None
            if v is not None:
                setattr(obj, k, v)
        obj.last_seen = today


def replace_program_participation(session: Session, project_id: int, rows: list[dict]) -> None:
    session.execute(delete(ProgramParticipation).where(ProgramParticipation.project_id == project_id))
    for r in rows:
        session.add(ProgramParticipation(
            project_id=project_id,
            program=r.get("program"),
            file_number=r.get("file_number"),
            year=r.get("year"),
        ))


def replace_ami_tiers(session: Session, project_id: int, rows: list[dict]) -> None:
    session.execute(delete(AmiTier).where(AmiTier.project_id == project_id))
    for r in rows:
        session.add(AmiTier(
            project_id=project_id,
            ami_pct=r.get("ami_pct"),
            num_units=r.get("num_units"),
        ))


def replace_detail_units(session: Session, project_id: int, rows: list[dict]) -> None:
    session.execute(delete(DetailUnit).where(DetailUnit.project_id == project_id))
    for r in rows:
        session.add(DetailUnit(
            project_id=project_id,
            unit_type=r.get("unit_type"),
            bedroom_type=r.get("bedroom_type"),
            sqft=r.get("sqft"),
            rent=r.get("rent"),
            num_units=r.get("num_units"),
            vacancies=r.get("vacancies"),
        ))


def upsert_unit_snapshot(session: Session, project_id: int, snapshot_date: dt.date,
                         bedroom_type: str, accessible: bool,
                         num_units: Optional[int], vacancies: Optional[int]) -> None:
    """
    Idempotent on (project_id, snapshot_date, bedroom_type, accessible).
    Re-running the scraper the same day overwrites rather than duplicates.
    """
    stmt = select(UnitSnapshot).where(
        UnitSnapshot.project_id == project_id,
        UnitSnapshot.snapshot_date == snapshot_date,
        UnitSnapshot.bedroom_type == bedroom_type,
        UnitSnapshot.accessible == accessible,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is None:
        session.add(UnitSnapshot(
            project_id=project_id, snapshot_date=snapshot_date,
            bedroom_type=bedroom_type, accessible=accessible,
            num_units=num_units, vacancies=vacancies,
        ))
    else:
        existing.num_units = num_units
        existing.vacancies = vacancies

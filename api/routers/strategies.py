"""Strategies: the builder's templates, and the ones saved from it.

A strategy is a spec in `optbt.spec`'s shape - the same body a backtest run
takes, without the window - plus the underlying it was built for. Saved here, it
can be loaded back into the builder, backtested on the NSE's history, or paper
traded on a live crypto chain, and all three read the one spec.

    GET    /api/strategies/templates   built-in starting points, filled out
    GET    /api/strategies             saved ones, newest first (?underlying=)
    POST   /api/strategies             save one; with `id`, overwrite it
    DELETE /api/strategies/{id}
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.deps import DbPathDep
from api.routers.optbt import STRICT, StrategyIn
from api.store import open_db
from optbt.spec import VERSION
from optbt.templates import TEMPLATES
from storage import strategy_repo

router = APIRouter(tags=["strategies"], prefix="/api/strategies")

#: An index ("NIFTY") or a coin ("BTC"). Which venue serves it is the runner's
#: business; this only keeps the column to something a runner could look up.
_UNDERLYING = re.compile(r"^[A-Z0-9]{2,12}$")


class TemplateOut(BaseModel):
    id: str
    name: str
    say: str
    spec: StrategyIn


@router.get("/templates", response_model=list[TemplateOut])
def templates() -> list[TemplateOut]:
    """Every template, validated - so a template that drifts from the spec fails
    here, loudly, rather than loading into the builder half-understood."""
    return [
        TemplateOut(id=t.id, name=t.name, say=t.say, spec=StrategyIn.model_validate(t.spec))
        for t in TEMPLATES
    ]


class StrategySaveIn(BaseModel):
    model_config = STRICT

    #: Set to overwrite that saved strategy; absent saves a new one.
    id: int | None = None
    name: str = Field(min_length=1, max_length=80)
    underlying: str
    spec: StrategyIn

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a strategy needs a name")
        return value

    @field_validator("underlying")
    @classmethod
    def _underlying(cls, value: str) -> str:
        value = value.strip().upper()
        if not _UNDERLYING.match(value):
            raise ValueError(f"not an underlying: {value!r}")
        return value


class StrategyOut(BaseModel):
    id: int
    name: str
    underlying: str
    spec: dict[str, Any]
    created_at: datetime
    saved_at: datetime


@router.get("", response_model=list[StrategyOut])
def listing(db_path: DbPathDep, underlying: str | None = None) -> list[StrategyOut]:
    conn = open_db(db_path)
    try:
        found = strategy_repo.listing(conn, underlying.upper() if underlying else None)
    finally:
        conn.close()
    return [StrategyOut(**vars(s)) for s in found]


@router.post("", response_model=StrategyOut)
def save(body: StrategySaveIn, db_path: DbPathDep) -> StrategyOut:
    conn = open_db(db_path)
    try:
        # Stamped with the spec version it was written in, so a later reader that
        # changes the shape can tell an old one from a new one.
        spec = {**body.spec.model_dump(mode="json"), "version": VERSION}
        saved = strategy_repo.save(conn, body.name, body.underlying, spec, body.id)
    except KeyError as exc:
        raise HTTPException(404, f"No saved strategy {body.id}") from exc
    finally:
        conn.close()
    return StrategyOut(**vars(saved))


@router.delete("/{strategy_id}")
def delete(strategy_id: int, db_path: DbPathDep) -> dict[str, bool]:
    conn = open_db(db_path)
    try:
        if not strategy_repo.delete(conn, strategy_id):
            raise HTTPException(404, f"No saved strategy {strategy_id}")
    finally:
        conn.close()
    return {"deleted": True}

"""Запис газетиру: SQLite для бекенда й JSON охоплення для клієнта."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pipeline.gazetteer import Entity, GazetteerResult

# Лише населені пункти й області потрапляють у JSON охоплення (пошук і гра «Міста»).
SCOPE_JSON_TYPES = frozenset({"oblast", "city", "selyshche", "selo"})

_SCHEMA = """
create table entity (
    id text primary key,
    type text not null,
    katottg text,
    qid text,
    geonames_id text,
    osm_id text,
    names text not null,
    parents text not null,
    country text not null,
    lat real,
    lon real,
    geom_ref text,
    population text,
    flag text,
    coa text,
    scopes text not null,
    tiers text not null,
    start_letter text,
    chain_letter text,
    neighbors text not null,
    facts text not null,
    attributes text not null,
    dataset_version text not null
);
create table meta (dataset_version text not null, startable_json text not null);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_or_null(value: Any) -> str | None:
    return None if value is None else _json(value)


def _row(e: Entity) -> tuple[Any, ...]:
    return (
        e.id, e.type, e.katottg, e.qid, e.geonames_id, e.osm_id, _json(e.names), _json(e.parents), e.country,
        e.lat, e.lon, e.geom_ref, _json_or_null(e.population), _json_or_null(e.flag), _json_or_null(e.coa),
        _json(e.scopes), _json(e.tiers), e.start_letter, e.chain_letter, _json(e.neighbors), _json(e.facts),
        _json(e.attributes), e.dataset_version,
    )  # fmt: skip


def startable_json(result: GazetteerResult) -> dict[str, list[str]]:
    return {str(tier): sorted(letters) for tier, letters in sorted(result.startable.items())}


def write_sqlite(result: GazetteerResult, path: Path) -> None:
    """Усі сутності в таблиці `entity` (JSON-колонки) і `meta`; запис через `.part`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)
    con = sqlite3.connect(part)
    try:
        con.executescript(_SCHEMA)
        con.executemany(f"insert into entity values ({','.join('?' * 23)})", (_row(e) for e in result.entities))
        con.execute("insert into meta values (?, ?)", (result.dataset_version, _json(startable_json(result))))
        con.commit()
    except BaseException:
        con.close()
        part.unlink(missing_ok=True)
        raise
    con.close()
    part.replace(path)


def _scope_entity(e: Entity) -> dict[str, Any]:
    return {
        "id": e.id,
        "type": e.type,
        "name": e.names["uk"],
        "lat": e.lat,
        "lon": e.lon,
        "tier": e.tier,
        "population": None if e.population is None else e.population["value"],
        "chain_letter": e.chain_letter,
    }


def write_scope_json(result: GazetteerResult, path: Path) -> None:
    """JSON охоплення «Україна»: населені пункти й області; без точки — `lat`/`lon` = null."""
    payload = {
        "dataset_version": result.dataset_version,
        "startable": startable_json(result),
        "entities": [_scope_entity(e) for e in result.entities if e.type in SCOPE_JSON_TYPES],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    part.replace(path)

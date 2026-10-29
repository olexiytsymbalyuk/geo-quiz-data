"""Wikidata: розбір SPARQL-відповідей (фікстури синтетичні) і герметичний `fetch`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from pipeline import wikidata
from pipeline.wikidata import WdItem, WikidataError, parse_capitals, parse_items

FIXTURES = Path(__file__).parent / "fixtures"
QUERIES = Path(wikidata.__file__).parent / "queries"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _code(n: int) -> str:
    return f"UA05020030010{n:06d}"


@pytest.fixture(scope="module")
def parsed() -> tuple[dict[str, WdItem], dict[str, list[str]]]:
    return parse_items(_load("wdqs_items.json"))


# --- parse_items ---------------------------------------------------------------------------


def test_preferred_population_wins_over_normal(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(1)] == WdItem("Q101", 1200, 2022, 49.2328, 28.4683)


def test_preferred_wins_even_when_older_than_normal(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert (items[_code(2)].population, items[_code(2)].population_year) == (5000, 2010)


def test_latest_point_in_time_among_normal(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert (items[_code(3)].population, items[_code(3)].population_year) == (350, 2019)


def test_latest_point_in_time_among_several_preferred(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert (items[_code(4)].population, items[_code(4)].population_year) == (80, 2020)


def test_deprecated_population_is_ignored_but_item_stays(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(5)] == WdItem("Q105", None, None, 50.45, 30.5)


def test_item_without_population_and_coordinates(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(6)] == WdItem("Q106", None, None, None, None)


def test_population_without_point_in_time_has_no_year(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(7)] == WdItem("Q107", 10, None, 49.8, 24.0)


def test_duplicate_rows_do_not_change_the_result(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(8)] == WdItem("Q108", 15, 2018, 50.0, 40.0)


def test_two_qids_on_one_code_go_to_conflicts_only(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, conflicts = parsed
    assert conflicts == {_code(10): ["Q110", "Q111"]}
    assert _code(10) not in items


def test_code_is_stripped_of_surrounding_whitespace(parsed: tuple[dict[str, WdItem], dict[str, list[str]]]) -> None:
    items, _ = parsed
    assert items[_code(12)].qid == "Q112"
    assert all(code == code.strip() for code in items)


def test_empty_response_gives_empty_result() -> None:
    assert parse_items({"results": {"bindings": []}}) == ({}, {})


def _binding(**overrides: dict[str, str]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1"},
        "code": {"type": "literal", "value": _code(1)},
    }
    return {**base, **overrides}


def test_malformed_wkt_gives_no_coordinates() -> None:
    row = _binding(coord={"type": "literal", "value": "not a point"})
    items, _ = parse_items({"results": {"bindings": [row]}})
    assert (items[_code(1)].lat, items[_code(1)].lon) == (None, None)


def test_coordinates_out_of_range_are_dropped() -> None:
    row = _binding(coord={"type": "literal", "value": "Point(200 95)"})
    items, _ = parse_items({"results": {"bindings": [row]}})
    assert (items[_code(1)].lat, items[_code(1)].lon) == (None, None)


def test_non_wikidata_item_uri_is_an_error() -> None:
    row = _binding(item={"type": "uri", "value": "http://example.org/x"})
    with pytest.raises(WikidataError):
        parse_items({"results": {"bindings": [row]}})


def test_missing_bindings_is_an_error() -> None:
    with pytest.raises(WikidataError):
        parse_items({"head": {}})


# --- parse_capitals ------------------------------------------------------------------------


def test_capitals_fixture_has_24_pairs() -> None:
    capitals = parse_capitals(_load("wdqs_capitals.json"))
    assert len(capitals) == 24
    assert capitals["UA01000000000010001"] == "UA01020010010020001"


def test_oblast_with_two_centers_is_not_in_capitals_but_in_conflicts() -> None:
    capitals, conflicts = wikidata.split_capitals(_load("wdqs_capitals.json"))
    assert "UA25000000000010025" not in capitals
    assert conflicts == {"UA25000000000010025": ["UA25020010010020025", "UA25080030010020026"]}


def test_repeated_pair_is_not_a_conflict() -> None:
    pair = {
        "oblastCode": {"type": "literal", "value": "UA01000000000010001"},
        "centerCode": {"type": "literal", "value": _code(1)},
    }
    capitals, conflicts = wikidata.split_capitals({"results": {"bindings": [pair, pair]}})
    assert capitals == {"UA01000000000010001": _code(1)} and conflicts == {}


def test_capitals_query_skips_ended_and_deprecated_statements() -> None:
    text = (QUERIES / "oblast_capitals.rq").read_text(encoding="utf-8")
    assert "pq:P582" in text and "DeprecatedRank" in text


# --- запити --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["katottg_items.rq", "oblast_capitals.rq"])
def test_queries_have_no_labels_and_no_language_tags(name: str) -> None:
    text = (QUERIES / name).read_text(encoding="utf-8")
    lang = "r" + "u"  # щоб сам тест не містив мовних міток
    for needle in ("rdfs:label", "wikibase:label", "skos:", f'"{lang}"', f"@{lang}", "xml:" + "lang", "LANG("):
        assert needle not in text
    assert "P9435" in text


# --- fetch (мережа підміняється) -----------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeWeb:
    def __init__(self, *responses: _Response | Exception):
        self.queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []
    monkeypatch.setattr(wikidata.time, "sleep", pauses.append)
    return pauses


def _ok_pair() -> list[_Response]:
    return [_Response(200, _load("wdqs_items.json")), _Response(200, _load("wdqs_capitals.json"))]


def _install(monkeypatch: pytest.MonkeyPatch, web: _FakeWeb) -> None:
    monkeypatch.setattr(wikidata.requests, "get", web.get)


def test_fetch_queries_once_per_type_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    web = _FakeWeb(*_ok_pair())
    _install(monkeypatch, web)
    result = wikidata.fetch(tmp_path)
    assert len(web.calls) == 2
    assert result.cache_path.parent == tmp_path
    assert result.cache_path.name == f"wikidata-{result.queried_at[:10]}.json"
    assert result.cache_path.exists() and not list(tmp_path.glob("*.part"))
    assert len(result.items) == 9 and len(result.capitals) == 24
    assert result.conflicts == {_code(10): ["Q110", "Q111"]}
    assert list(result.capital_conflicts) == ["UA25000000000010025"]
    call = web.calls[0]
    assert call["url"] == wikidata.ENDPOINT
    assert call["headers"]["User-Agent"] == wikidata.USER_AGENT
    assert call["timeout"] == wikidata._TIMEOUT
    assert "P9435" in call["params"]["query"]


def test_fetch_uses_cache_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    first = _FakeWeb(*_ok_pair())
    _install(monkeypatch, first)
    fresh = wikidata.fetch(tmp_path)
    _install(monkeypatch, _FakeWeb())  # будь-який мережевий виклик -> IndexError
    cached = wikidata.fetch(tmp_path)
    assert cached == fresh


def test_fetch_takes_the_latest_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"queried_at": "2026-01-02T03:04:05Z", "items": _load("wdqs_items.json"), "capitals": _load("wdqs_capitals.json")}
    old = {**body, "queried_at": "2025-12-31T00:00:00Z", "capitals": {"results": {"bindings": []}}}
    (tmp_path / "wikidata-2025-12-31.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "wikidata-2026-01-02.json").write_text(json.dumps(body), encoding="utf-8")
    _install(monkeypatch, _FakeWeb())
    result = wikidata.fetch(tmp_path)
    assert result.queried_at == "2026-01-02T03:04:05Z"
    assert result.cache_path.name == "wikidata-2026-01-02.json"
    assert len(result.capitals) == 24


def test_fetch_refresh_ignores_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    stale = {"queried_at": "2020-01-01T00:00:00Z", "items": {"results": {"bindings": []}}, "capitals": {"results": {"bindings": []}}}
    (tmp_path / "wikidata-2020-01-01.json").write_text(json.dumps(stale), encoding="utf-8")
    web = _FakeWeb(*_ok_pair())
    _install(monkeypatch, web)
    result = wikidata.fetch(tmp_path, refresh=True)
    assert len(web.calls) == 2
    assert len(result.items) == 9
    assert result.cache_path.name != "wikidata-2020-01-01.json"


def test_fetch_retries_on_429_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    web = _FakeWeb(_Response(429, headers={"Retry-After": "7"}), *_ok_pair())
    _install(monkeypatch, web)
    wikidata.fetch(tmp_path)
    assert len(web.calls) == 3
    assert no_sleep == [7.0]


def test_fetch_gives_up_after_two_retries_with_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    web = _FakeWeb(_Response(503), _Response(503), _Response(503))
    _install(monkeypatch, web)
    with pytest.raises(WikidataError, match="503"):
        wikidata.fetch(tmp_path)
    assert len(web.calls) == 3
    assert not list(tmp_path.glob("wikidata-*"))


def test_fetch_does_not_retry_client_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    web = _FakeWeb(_Response(400))
    _install(monkeypatch, web)
    with pytest.raises(WikidataError, match="400"):
        wikidata.fetch(tmp_path)
    assert len(web.calls) == 1


def test_fetch_retries_connection_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    web = _FakeWeb(requests.ConnectionError("down"), *_ok_pair())
    _install(monkeypatch, web)
    assert len(wikidata.fetch(tmp_path).items) == 9


def test_fetch_failure_in_second_query_writes_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    web = _FakeWeb(_Response(200, _load("wdqs_items.json")), _Response(500), _Response(500), _Response(500))
    _install(monkeypatch, web)
    with pytest.raises(WikidataError):
        wikidata.fetch(tmp_path)
    assert not list(tmp_path.iterdir())


def test_broken_cache_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "wikidata-2026-01-02.json").write_text('{"queried_at": "x"}', encoding="utf-8")
    _install(monkeypatch, _FakeWeb())
    with pytest.raises(WikidataError):
        wikidata.fetch(tmp_path)


# --- звіт й CLI ----------------------------------------------------------------------------


def test_build_report_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    _install(monkeypatch, _FakeWeb(*_ok_pair()))
    result = wikidata.fetch(tmp_path)
    out = tmp_path / "work" / "report.json"
    report = wikidata.build_report(result, out)
    assert json.loads(out.read_text(encoding="utf-8")) == report
    assert report["codes_with_qid"] == 9
    assert report["codes_with_population"] == 7
    assert report["codes_with_coordinates"] == 5
    assert report["conflicts"] == 1
    assert report["oblast_capitals"] == 24
    assert report["oblast_capital_conflicts"] == 1
    assert report["queried_at"] == result.queried_at


def test_main_rejects_unknown_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert wikidata.main(["--nope"]) == 2
    assert "використання" in capsys.readouterr().err


def test_unparsable_response_writes_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    _install(monkeypatch, _FakeWeb(_Response(200, {"unexpected": 1}), _Response(200, _load("wdqs_capitals.json"))))
    with pytest.raises(WikidataError):
        wikidata.fetch(tmp_path)
    assert not list(tmp_path.iterdir())

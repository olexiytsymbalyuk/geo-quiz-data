"""PMTiles: запуск tippecanoe і штамп версії датасету в метаданих (Завдання 11)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write

from pipeline import tiles
from pipeline.tiles import ATTRIBUTION, KATOTTG_LICENSOR, read_metadata, run_tippecanoe, stamp_metadata, tile_stats
from pipeline.tools import ToolRun

TILE_A = gzip.compress(b"tile-a", mtime=0)
TILE_B = gzip.compress(b"tile-b-different", mtime=0)


def _write_archive(path: Path, metadata: dict[str, object]) -> None:
    header = {
        "version": 3,
        "tile_compression": Compression.GZIP,
        "tile_type": TileType.MVT,
        "min_lon_e7": 220_000_000,
        "min_lat_e7": 440_000_000,
        "max_lon_e7": 400_000_000,
        "max_lat_e7": 520_000_000,
        "center_zoom": 4,
        "center_lon_e7": 310_000_000,
        "center_lat_e7": 480_000_000,
    }
    with write(path) as writer:
        writer.write_tile(zxy_to_tileid(0, 0, 0), TILE_A)
        writer.write_tile(zxy_to_tileid(1, 0, 0), TILE_B)
        writer.write_tile(zxy_to_tileid(1, 1, 0), TILE_A)  # дубль вмісту — один блок даних
        writer.finalize(header, metadata)


def _tiles(path: Path) -> list[tuple[tuple[int, int, int], bytes]]:
    with path.open("rb") as handle:
        return [(zxy, bytes(data)) for zxy, data in all_tiles(MmapSource(handle))]


def test_stamp_metadata_adds_keys_and_keeps_existing_ones(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.pmtiles", tmp_path / "out.pmtiles"
    _write_archive(src, {"name": "atlas", "attribution": "OSM"})

    stamp_metadata(src, dst, {"dataset_version": "2026.11.1"})

    metadata = read_metadata(dst)
    assert metadata["dataset_version"] == "2026.11.1"
    assert metadata["name"] == "atlas"
    assert metadata["attribution"] == "OSM"
    assert "dataset_version" not in read_metadata(src)  # джерело не змінюється


def test_stamp_metadata_copies_tiles_byte_for_byte(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.pmtiles", tmp_path / "out.pmtiles"
    _write_archive(src, {})

    stamp_metadata(src, dst, {"dataset_version": "2026.11.1"})

    assert _tiles(dst) == _tiles(src)
    assert [data for _, data in _tiles(dst)] == [TILE_A, TILE_B, TILE_A]
    with dst.open("rb") as handle:
        assert Reader(MmapSource(handle)).get(1, 0, 0) == TILE_B


def test_stamp_metadata_keeps_header_geography_and_compression(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.pmtiles", tmp_path / "out.pmtiles"
    _write_archive(src, {})

    stamp_metadata(src, dst, {"dataset_version": "2026.11.1"})

    with src.open("rb") as a, dst.open("rb") as b:
        before, after = Reader(MmapSource(a)).header(), Reader(MmapSource(b)).header()
    for key in (
        "tile_compression",
        "tile_type",
        "min_zoom",
        "max_zoom",
        "min_lon_e7",
        "min_lat_e7",
        "max_lon_e7",
        "max_lat_e7",
        "center_zoom",
        "center_lon_e7",
        "center_lat_e7",
    ):
        assert after[key] == before[key], key


def test_stamp_metadata_overwrites_a_previous_stamp(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.pmtiles", tmp_path / "out.pmtiles"
    _write_archive(src, {"dataset_version": "old"})

    stamp_metadata(src, dst, {"dataset_version": "2026.11.1"})

    assert read_metadata(dst)["dataset_version"] == "2026.11.1"


def test_stamp_metadata_refuses_to_overwrite_its_source(tmp_path: Path) -> None:
    src = tmp_path / "in.pmtiles"
    _write_archive(src, {})
    with pytest.raises(ValueError):
        stamp_metadata(src, src, {"dataset_version": "2026.11.1"})


def test_tile_stats_reports_max_size_per_zoom_and_first_screen_budget(tmp_path: Path) -> None:
    archive = tmp_path / "a.pmtiles"
    _write_archive(archive, {})  # z0: 1 тайл, z1: 2 тайли (0,0) і (1,0) — північна півкуля

    everywhere = tile_stats(archive, (-179.0, 1.0, 179.0, 80.0))
    assert everywhere["tiles_by_zoom"] == {"0": 1, "1": 2}
    assert everywhere["max_tile_bytes_gzip"] == {"0": len(TILE_A), "1": len(TILE_B)}
    assert everywhere["max_tile_bytes_raw"] == {"0": len(b"tile-a"), "1": len(b"tile-b-different")}
    assert everywhere["first_screen_z3_z6_bytes"] == 0  # у архіві немає тайлів z3-z6

    deep = tmp_path / "deep.pmtiles"
    header = {"version": 3, "tile_compression": Compression.GZIP, "tile_type": TileType.MVT}
    with write(deep) as writer:
        writer.write_tile(zxy_to_tileid(4, 9, 5), TILE_A)  # у межах України
        writer.write_tile(zxy_to_tileid(3, 4, 2), TILE_B)  # z3, у межах України — теж у бюджеті
        writer.write_tile(zxy_to_tileid(4, 0, 0), TILE_B)  # поза екстентом
        writer.finalize(header, {})
    stats = tile_stats(deep, (22.0, 44.0, 40.0, 52.5))
    assert stats["first_screen_z3_z6_bytes"] == len(TILE_A) + len(TILE_B)


def test_attribution_names_all_sources() -> None:
    for source in ("OpenStreetMap", "КАТОТТГ", "Natural Earth", "Wikidata"):
        assert source in ATTRIBUTION


def test_katottg_licensor_is_one_name_in_attribution_sources_and_readme() -> None:
    root = Path(__file__).resolve().parents[2]
    assert KATOTTG_LICENSOR in ATTRIBUTION
    assert KATOTTG_LICENSOR in (root / "README.md").read_text(encoding="utf-8")
    assert KATOTTG_LICENSOR in (root / "config" / "sources.yaml").read_text(encoding="utf-8")
    assert "Мінінфраструктури" not in (root / "README.md").read_text(encoding="utf-8")


def test_run_tippecanoe_builds_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    layers = {"place": layers_dir / "place.geojsonl", "admin": layers_dir / "admin.geojsonl"}
    out = tmp_path / "out" / "atlas.pmtiles"
    seen: dict[str, object] = {}

    def fake_run_tool(args: list[str], mounts: dict[Path, str], workdir: str = "/work") -> ToolRun:
        seen.update(args=args, mounts=mounts)
        return ToolRun(seconds=1.0, max_rss_mb=2.0)

    monkeypatch.setattr(tiles, "run_tool", fake_run_tool)
    monkeypatch.setattr(tiles, "ensure_image", lambda: None)

    run = run_tippecanoe(layers, out)

    assert run == ToolRun(seconds=1.0, max_rss_mb=2.0)
    args = seen["args"]
    mounts = seen["mounts"]
    assert isinstance(args, list) and isinstance(mounts, dict)
    assert args[0] == "tippecanoe"
    assert {"-Z3", "-z12", "--force", "--read-parallel"} <= set(args)
    assert "--name=ukraine" in args and "--description=ukraine" in args  # без шляхів контейнера в метаданих
    assert "--no-feature-limit" not in args and "--no-tile-size-limit" not in args
    assert args[args.index("-o") + 1].endswith("atlas.pmtiles")
    assert [a for a in args if a.startswith("--attribution=")] == [f"--attribution={ATTRIBUTION}"]
    layer_args = [args[i + 1] for i, a in enumerate(args) if a == "-L"]
    assert layer_args == ["admin:/layers/admin.geojsonl", "place:/layers/place.geojsonl"]  # відсортовано
    assert mounts[layers_dir] == "/layers"
    assert mounts[out.parent] == "/out"
    assert out.parent.is_dir()


@pytest.mark.docker
def test_run_tippecanoe_writes_pmtiles_with_the_layers(tmp_path: Path) -> None:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    feature = {
        "type": "Feature",
        "properties": {"id": "ua:x", "name:uk": "Тест", "kind": "city", "rank": 0},
        "geometry": {"type": "Point", "coordinates": [30.5, 50.4]},
        "tippecanoe": {"layer": "place", "minzoom": 3, "maxzoom": 12},
    }
    (layers_dir / "place.geojsonl").write_text(json.dumps(feature, ensure_ascii=False) + "\n", encoding="utf-8")
    out = tmp_path / "out.pmtiles"

    run_tippecanoe({"place": layers_dir / "place.geojsonl"}, out)

    metadata = read_metadata(out)
    assert metadata["attribution"] == ATTRIBUTION
    assert metadata["name"] == "ukraine"
    layer_ids = {layer["id"] for layer in metadata["vector_layers"]}
    assert layer_ids == {"place"}

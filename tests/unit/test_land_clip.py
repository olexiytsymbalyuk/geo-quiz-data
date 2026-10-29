"""Обрізка полігонів суші до bbox датасету: стабільне, відтворюване джерело (рішення власника від 2026-10-29)."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
import shapefile
import yaml
from shapely.geometry import Polygon, box

from pipeline import land_clip
from pipeline.fetch import ChecksumMismatch, sha256_of
from pipeline.land_clip import ClipSpec, clip_land_polygons
from pipeline.outline import load_land_mask

DATASET_BBOX = (20.0, 44.0, 40.0, 52.0)
MARGIN = 3.0
# Кроки 1 градус; 22,5 -- у запасі (3°), 80 -- далеко.
INSIDE = Polygon([(30.123456789012345, 48.1), (31.2, 48.1), (31.2, 49.333333333333336), (30.123456789012345, 49.1)])
IN_MARGIN = box(18.0, 47.0, 18.9, 47.9)
FAR = box(80.0, 10.0, 81.0, 11.0)
STRADDLING = box(39.5, 51.5, 44.0, 56.0)  # перетинає межу запасу: лишається цілим, без підрізання


def _spec(source: Path) -> ClipSpec:
    return ClipSpec(
        source_url="https://example.invalid/land.zip",
        source_sha256=sha256_of(source),
        source_size=source.stat().st_size,
        source_last_modified="Thu, 29 Oct 2026 03:42:52 GMT",
        dataset_bbox=DATASET_BBOX,
        margin_deg=MARGIN,
    )


def _write_full_zip(path: Path, polygons: list[Polygon]) -> None:
    base = path.parent / "src" / "land_polygons"
    base.parent.mkdir(exist_ok=True)
    with shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        writer.field("x", "N", 9)
        writer.field("y", "N", 9)
        for index, polygon in enumerate(polygons):
            writer.record(index, -index)
            writer.shape(polygon.__geo_interface__)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("land-polygons-split-4326/README.txt", "тестовий архів")
        for extension in ("shp", "shx", "dbf"):
            archive.write(base.with_suffix(f".{extension}"), f"land-polygons-split-4326/land_polygons.{extension}")
        archive.writestr("land-polygons-split-4326/land_polygons.cpg", "UTF-8\n")
        archive.writestr("land-polygons-split-4326/land_polygons.prj", "GEOGCS[\"test\"]")


def _clip(tmp_path: Path, polygons: list[Polygon], name: str = "clip.zip") -> Path:
    source = tmp_path / f"full-{name}"
    _write_full_zip(source, polygons)
    out = tmp_path / name
    clip_land_polygons(source, out, _spec(source))
    return out


def _shapes(zip_path: Path) -> list[Polygon]:
    from pipeline.outline import extract_land_polygons

    with shapefile.Reader(str(extract_land_polygons(zip_path)), encoding="utf-8") as reader:
        return [Polygon(*[s.points]) for s in reader.shapes()]


def test_clip_keeps_only_polygons_intersecting_the_bbox_plus_margin(tmp_path: Path) -> None:
    out = _clip(tmp_path, [FAR, INSIDE, IN_MARGIN, STRADDLING])

    kept = _shapes(out)

    assert len(kept) == 3
    assert not any(p.equals(FAR) for p in kept)
    assert any(p.equals(STRADDLING) for p in kept)  # геометрія не підрізається


def test_clip_coordinates_are_lossless(tmp_path: Path) -> None:
    out = _clip(tmp_path, [INSIDE])

    (kept,) = _shapes(out)

    assert set(kept.exterior.coords) == set(INSIDE.exterior.coords)  # ті самі подвійні числа, без округлення


def test_clip_is_byte_identical_for_the_same_input(tmp_path: Path) -> None:
    first = _clip(tmp_path, [INSIDE, IN_MARGIN, STRADDLING], "a.zip")
    second = _clip(tmp_path, [INSIDE, IN_MARGIN, STRADDLING], "b.zip")

    assert first.read_bytes() == second.read_bytes()


def test_clip_does_not_depend_on_the_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "full.zip"
    _write_full_zip(source, [INSIDE, IN_MARGIN])
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    clip_land_polygons(source, first, _spec(source))
    real_localtime = time.localtime
    monkeypatch.setattr(time, "localtime", lambda *a: real_localtime(86_400.0 * 400 + 1_700_000_000))  # інша дата в заголовку dbf
    clip_land_polygons(source, second, _spec(source))

    assert first.read_bytes() == second.read_bytes()


def test_clip_does_not_depend_on_the_order_of_source_records(tmp_path: Path) -> None:
    polygons = [INSIDE, IN_MARGIN, STRADDLING, box(25, 45, 26, 46)]
    shuffled = [polygons[2], polygons[0], polygons[3], polygons[1]]

    first = _clip(tmp_path, polygons, "a.zip")
    second = _clip(tmp_path, shuffled, "b.zip")

    with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
        assert a.read("land_polygons.shp") == b.read("land_polygons.shp")
        assert a.read("land_polygons.shx") == b.read("land_polygons.shx")


def test_zip_members_are_in_pinned_order_with_fixed_metadata(tmp_path: Path) -> None:
    out = _clip(tmp_path, [INSIDE])

    with zipfile.ZipFile(out) as archive:
        infos = archive.infolist()

    assert [i.filename for i in infos] == [
        "land_polygons.shp",
        "land_polygons.shx",
        "land_polygons.dbf",
        "land_polygons.cpg",
        "land_polygons.prj",
        "PROVENANCE.json",
    ]
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {i.create_system for i in infos} == {3}
    assert {i.external_attr for i in infos} == {0o644 << 16}


def test_dbf_keeps_the_source_field_order_and_values(tmp_path: Path) -> None:
    out = _clip(tmp_path, [FAR, INSIDE])

    with zipfile.ZipFile(out) as archive:
        archive.extractall(tmp_path / "x")
    with shapefile.Reader(str(tmp_path / "x" / "land_polygons")) as reader:
        assert [f.name for f in reader.fields[1:]] == ["x", "y"]
        assert [list(r) for r in reader.records()] == [[1, -1]]


def test_provenance_is_recorded_without_timestamps(tmp_path: Path) -> None:
    source = tmp_path / "full.zip"
    _write_full_zip(source, [INSIDE])
    out = tmp_path / "clip.zip"
    clip_land_polygons(source, out, _spec(source))

    with zipfile.ZipFile(out) as archive:
        provenance = json.loads(archive.read("PROVENANCE.json"))

    assert provenance == {
        "license": "ODbL 1.0",
        "attribution": "© OpenStreetMap contributors",
        "source_url": "https://example.invalid/land.zip",
        "source_sha256": sha256_of(source),
        "source_size": source.stat().st_size,
        "source_last_modified": "Thu, 29 Oct 2026 03:42:52 GMT",
        "dataset_bbox": list(DATASET_BBOX),
        "clip_margin_deg": MARGIN,
        "clip_bbox": [17.0, 41.0, 43.0, 55.0],
        "polygons": 1,
    }


def test_clip_refuses_a_source_that_is_not_the_recorded_one(tmp_path: Path) -> None:
    source = tmp_path / "full.zip"
    _write_full_zip(source, [INSIDE])
    spec = _spec(source)
    wrong = ClipSpec(**{**spec.__dict__, "source_sha256": "0" * 64})

    with pytest.raises(ChecksumMismatch):
        clip_land_polygons(source, tmp_path / "clip.zip", wrong)

    assert not (tmp_path / "clip.zip").exists()


def test_clip_of_nothing_is_an_error_not_an_empty_file(tmp_path: Path) -> None:
    source = tmp_path / "full.zip"
    _write_full_zip(source, [FAR])

    with pytest.raises(ValueError, match="жодного полігона"):
        clip_land_polygons(source, tmp_path / "clip.zip", _spec(source))

    assert not (tmp_path / "clip.zip").exists()


def test_pipeline_reads_the_clip_like_the_full_archive(tmp_path: Path) -> None:
    polygons = [INSIDE, IN_MARGIN, STRADDLING, FAR, box(25, 45, 26, 46)]
    source = tmp_path / "full.zip"
    _write_full_zip(source, polygons)
    out = tmp_path / "clip.zip"
    clip_land_polygons(source, out, _spec(source))
    build_bbox = (20.5, 44.5, 39.5, 51.5)  # bbox збірки: межі областей ± 0,5°

    from_full = load_land_mask(source, build_bbox)
    from_clip = load_land_mask(out, build_bbox)

    assert not from_full.is_empty
    assert from_clip.equals(from_full)


def test_load_land_mask_refuses_a_bbox_outside_the_clip(tmp_path: Path) -> None:
    out = _clip(tmp_path, [INSIDE])

    with pytest.raises(ValueError, match="обрізк"):
        load_land_mask(out, (10.0, 40.0, 30.0, 50.0))


def test_load_land_mask_accepts_a_bbox_inside_the_clip(tmp_path: Path) -> None:
    out = _clip(tmp_path, [INSIDE])

    assert not load_land_mask(out, (22.0, 45.0, 38.0, 50.0)).is_empty


def test_cli_writes_the_asset_and_verifies_the_source_from_sources_yaml(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "full.zip"
    _write_full_zip(source, [INSIDE, FAR])
    spec = _spec(source)
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "land_polygons": {
                        "license": "ODbL 1.0",
                        "files": [
                            {
                                "name": "clip.zip",
                                "url": "https://example.invalid/releases/clip.zip",
                                "derived_from": land_clip.derived_from(spec),
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "assets"

    code = land_clip.main(["--sources", str(sources), "--source", str(source), "--out-dir", str(out_dir)])

    assert code == 0
    produced = out_dir / "clip.zip"
    assert produced.is_file()
    printed = capsys.readouterr().out
    assert sha256_of(produced) in printed and str(produced.stat().st_size) in printed


def test_release_assets_are_not_tracked_by_git() -> None:
    ignored = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "data/" in ignored  # data/release-assets/ лежить під ним


def _repo_land_entry() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "config" / "sources.yaml").read_text(encoding="utf-8"))
    entry: dict[str, object] = config["sources"]["land_polygons"]["files"][0]
    return entry


def test_sources_yaml_margin_equals_the_margin_neutral_clipping_uses() -> None:
    from pipeline.neutral import CLIP_MARGIN_DEG

    derived = _repo_land_entry()["derived_from"]
    assert isinstance(derived, dict)

    assert derived["clip_margin_deg"] == CLIP_MARGIN_DEG


def test_lock_records_the_same_provenance_as_sources_yaml() -> None:
    root = Path(__file__).resolve().parents[2]
    lock = yaml.safe_load((root / "config" / "sources.lock").read_text(encoding="utf-8"))
    entry = _repo_land_entry()
    locked = lock["sources"]["land_polygons"]["files"][entry["name"]]

    assert locked["derived_from"] == entry["derived_from"]
    assert locked["url"] == entry["url"]
    assert "resolved_url" not in locked

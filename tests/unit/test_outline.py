"""Контур України по суші, державний кордон без берегової лінії, маска суші."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import shapefile
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry

from pipeline.outline import build_outline, load_land_mask, state_border


def _write_land_zip(zip_path: Path, polygons: list[Polygon], member_dir: str = "land-polygons-split-4326") -> None:
    shp_dir = zip_path.parent / "src"
    shp_dir.mkdir()
    base = shp_dir / "land_polygons"
    with shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        writer.field("id", "N")
        for index, polygon in enumerate(polygons):
            writer.record(index)
            writer.shape(polygon.__geo_interface__)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{member_dir}/README.txt", "тестовий архів")
        for extension in ("shp", "shx", "dbf"):
            archive.write(base.with_suffix(f".{extension}"), f"{member_dir}/land_polygons.{extension}")


def test_build_outline_never_leaves_the_land_mask() -> None:
    # область заходить у море: мапа суші — лише y >= 3
    oblasts = [box(0, 0, 10, 10), box(10, 0, 20, 10)]
    mask = box(-5, 3, 25, 20)
    outline = build_outline(oblasts, mask)
    assert isinstance(outline, MultiPolygon)
    assert mask.buffer(1e-9).contains(outline)
    assert outline.area == pytest.approx(20 * 7)


def test_build_outline_is_valid_for_invalid_input_polygon() -> None:
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert not bowtie.is_valid
    outline = build_outline([bowtie], box(-1, -1, 11, 11))
    assert outline.is_valid
    assert outline.area > 0


def test_build_outline_is_empty_multipolygon_when_nothing_on_land() -> None:
    outline = build_outline([box(0, 0, 1, 1)], box(50, 50, 51, 51))
    assert isinstance(outline, MultiPolygon)
    assert outline.is_empty


def test_state_border_has_no_coastline_but_keeps_the_land_border() -> None:
    admin = box(0, 0, 10, 10)
    mask = box(-5, 5, 15, 20)  # південна половина області — «море»
    outline = build_outline([admin], mask)

    border = state_border(outline, admin)

    assert isinstance(border, MultiLineString)
    # берегова лінія (y = 5) відсутня; допуск — лише 50-метровий буфер на кутах
    coast = LineString([(0.01, 5), (9.99, 5)])
    assert border.intersection(coast.buffer(1e-6)).length == pytest.approx(0, abs=1e-9)
    # західний, східний і північний відрізки межі з областю — 5 + 5 + 10 (з похибкою буфера на кутах)
    assert border.length == pytest.approx(20, abs=5e-3)
    # а без відсікання узбережжя межа контуру довша на довжину берегової лінії
    assert outline.boundary.length == pytest.approx(30, abs=1e-6)


def test_state_border_of_fully_inland_outline_is_whole_boundary() -> None:
    admin = box(0, 0, 10, 10)
    outline = build_outline([admin], box(-5, -5, 15, 15))
    assert state_border(outline, admin).length == pytest.approx(40, abs=5e-3)


def test_state_border_of_empty_outline_is_empty() -> None:
    border = state_border(MultiPolygon(), box(0, 0, 1, 1))
    assert isinstance(border, MultiLineString)
    assert border.is_empty


def test_load_land_mask_reads_only_polygons_in_bbox(tmp_path: Path) -> None:
    inside = box(30, 48, 31, 49)
    outside = box(100, -30, 101, -29)
    zip_path = tmp_path / "land.zip"
    _write_land_zip(zip_path, [inside, outside])

    mask = load_land_mask(zip_path, (22.1, 44.3, 40.3, 52.4))

    assert mask.area == pytest.approx(inside.area)
    assert mask.covers(inside)
    assert not mask.intersects(outside)


def test_load_land_mask_works_again_from_the_extracted_cache(tmp_path: Path) -> None:
    zip_path = tmp_path / "land.zip"
    _write_land_zip(zip_path, [box(30, 48, 31, 49)])
    bbox = (22.1, 44.3, 40.3, 52.4)
    first = load_land_mask(zip_path, bbox)
    zip_path.unlink()  # кеш уже розпаковано поруч — архів не потрібен
    second = load_land_mask(zip_path.with_name("land.zip"), bbox)
    assert first.equals(second)


def test_load_land_mask_without_polygons_in_bbox_is_empty(tmp_path: Path) -> None:
    zip_path = tmp_path / "land.zip"
    _write_land_zip(zip_path, [box(100, -30, 101, -29)])
    mask: BaseGeometry = load_land_mask(zip_path, (22.1, 44.3, 40.3, 52.4))
    assert mask.is_empty


def test_load_land_mask_rejects_archive_without_land_polygons(tmp_path: Path) -> None:
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("readme.txt", "x")
    with pytest.raises(ValueError, match="land_polygons"):
        load_land_mask(zip_path, (0, 0, 1, 1))

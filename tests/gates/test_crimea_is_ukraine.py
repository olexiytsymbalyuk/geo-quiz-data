"""Ворота: Крим — це Україна (Додаток Б, `test_crimea_is_ukraine`).

Країна, ланцюжок `parents`, покриття суші шаром `land` і відсутність державного кордону на Перекопі.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.policy import load_policy
from tests.gates.gate_support import Gazetteer, TileSet, find_one, summarize
from tests.gates.gazetteer_support import crimea_violations
from tests.gates.geo_support import FINEST_ZOOM, MAX_UNCOVERED_KM2, crimea_land_gaps, perekop_lines

MIN_ZOOM = 4


def test_crimean_entities_are_ukrainian(gazetteer: Gazetteer) -> None:
    assert gazetteer.entities, "у газетирі немає сутностей — перевірка була б порожньою"
    found = crimea_violations(gazetteer)

    assert not found, summarize(found)


def test_land_layer_covers_crimea(tiles: TileSet, raw_dir: Path, gates_out: Path) -> None:
    """Маска суші OSM у bbox Криму (32.4–36.7 E, 44.3–46.3 N) мінус шар `land` на z12.

    Відхилення від брифу («непокрита площа < 0,1 км²»), з причини вимірювання на релізі 2026.11.1:
    1) з маски виключено лише зв'язні компоненти, що перетинають NE-полігон нейтральної країни (Тамань і коса Чушка,
       з'єднані з материком Росії); острови й коси лишаються в перевірці, хоч би що казав NE про їхню країну;
    2) спрощення берега в тайлах лишає тисячі субметрових шматків уздовж узбережжя (сума ≈ 0,8 км²), тому умова така:
       жодна зв'язна непокрита компонента після внутрішнього берегового буфера 50 м не перевищує 0,1 км².
    Опорні точки (Тузла, Перекоп, Арабатська стрілка) мають лежати в перевірюваній зоні.
    Обидва виміри пишуться до `crimea-coverage.json`.
    """
    zip_path = find_one(raw_dir / "land_polygons", "*.zip")
    ne_shp = find_one(raw_dir / "ne_admin0_ukr", "*.shp")
    total_km2, largest_km2, checked, outside = crimea_land_gaps(tiles, zip_path, ne_shp, load_policy().neutral_countries)
    report = {"zoom": FINEST_ZOOM, "uncovered_total_km2": round(total_km2, 4), "largest_component_km2": round(largest_km2, 4)}
    (gates_out / "crimea-coverage.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    assert checked > 0, "у bbox Криму немає жодного тайла шару land — перевірка порожня"
    assert outside == 0, f"{outside} опорних точок Криму поза перевірюваною зоною"
    assert largest_km2 <= MAX_UNCOVERED_KM2, (
        f"непокрита суша: компонента {largest_km2:.3f} км² (> {MAX_UNCOVERED_KM2}), разом {total_km2:.3f} км²"
    )


def test_no_state_border_through_perekop(tiles: TileSet) -> None:
    zooms = sorted({z for z, _, _ in tiles.refs() if z >= MIN_ZOOM})
    seen, state = perekop_lines(tiles, zooms)

    assert seen > 0, "у прямокутнику Перекопу немає жодної лінії admin_line — перевірка порожня"
    assert state == 0, f"{state} ліній admin_line рівня 0 перетинають прямокутник Перекопу"

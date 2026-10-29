"""Тайли: GeoJSONSeq шарів -> PMTiles (tippecanoe) і штамп `dataset_version` у метаданих (спека §5.3, крок 7)."""

from __future__ import annotations

import gzip
import math
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import zxy_to_tileid
from pmtiles.writer import write

from pipeline.tools import ToolRun, ensure_image, run_tool

MIN_ZOOM = 4
MAX_ZOOM = 12
TILESET_NAME = "ukraine"  # ідентифікатор охоплення; без шляхів контейнера в метаданих
ATTRIBUTION = (
    "© учасники OpenStreetMap (ODbL); КАТОТТГ, Мінрозвитку (CC BY 4.0); Natural Earth (суспільне надбання); "
    "Wikidata (CC0)"
)
FIRST_SCREEN_MAX_ZOOM = 6  # бюджет першого екрана мапи — тайли z4–z6 екстенту України
_LAYERS_MOUNT = "/layers"
_OUT_MOUNT = "/out"


def run_tippecanoe(layers: Mapping[str, Path], out: Path) -> ToolRun:
    """Збирає `.pmtiles` з файлів шарів: `-Z4 -z12`, `-L <шар>:<файл>` на кожен, `--attribution`.

    Усі файли шарів лежать в одній теці — вона монтується в контейнер; вихідна тека — теж.
    Прапорів послаблення лімітів немає: вони додаються лише за виміром (див. звіт Завдання 11).
    """
    parents = {path.parent.resolve() for path in layers.values()}
    if len(parents) != 1:
        raise ValueError("файли шарів мають лежати в одній теці")
    layers_dir = parents.pop()
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "tippecanoe",
        f"-Z{MIN_ZOOM}",
        f"-z{MAX_ZOOM}",
        "--force",
        "--read-parallel",
        f"--attribution={ATTRIBUTION}",
        f"--name={TILESET_NAME}",
        f"--description={TILESET_NAME}",
        "-o",
        f"{_OUT_MOUNT}/{out.name}",
    ]
    for name in sorted(layers):
        args += ["-L", f"{name}:{_LAYERS_MOUNT}/{layers[name].name}"]
    ensure_image()
    return run_tool(args, {layers_dir: _LAYERS_MOUNT, out.parent.resolve(): _OUT_MOUNT})


def read_metadata(path: Path) -> dict[str, Any]:
    """JSON-метадані архіву PMTiles."""
    with path.open("rb") as handle:
        metadata: dict[str, Any] = Reader(MmapSource(handle)).metadata()
    return metadata


def stamp_metadata(src: Path, dst: Path, extra: Mapping[str, str]) -> None:
    """Переписує архів у `dst`, додаючи `extra` до JSON-метаданих; тайли копіюються байт-у-байт."""
    if src.resolve() == dst.resolve():
        raise ValueError("штамп пишеться в інший файл, ніж джерело")
    partial = dst.with_name(dst.name + ".partial")
    with src.open("rb") as handle:
        reader = Reader(MmapSource(handle))
        header = dict(reader.header())
        metadata = {**reader.metadata(), **extra}
        with write(partial) as writer:
            for zxy, data in all_tiles(MmapSource(handle)):
                writer.write_tile(zxy_to_tileid(*zxy), bytes(data))
            writer.finalize(header, metadata)
    partial.replace(dst)


def tile_stats(pmtiles_path: Path, bounds: tuple[float, float, float, float]) -> dict[str, Any]:
    """Розміри тайлів за зумами (стиснені, як віддає сервер) і бюджет першого екрана z4–z6 для екстенту України."""
    min_x, min_y, max_x, max_y = bounds

    def tile_range(zoom: int) -> tuple[range, range]:
        n = 2**zoom
        x0, x1 = (int((lon + 180) / 360 * n) for lon in (min_x, max_x))

        def y_of(lat: float) -> int:
            return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

        return range(x0, x1 + 1), range(y_of(max_y), y_of(min_y) + 1)

    by_zoom: dict[int, list[int]] = defaultdict(list)
    raw_by_zoom: dict[int, list[int]] = defaultdict(list)
    first_screen = 0
    with pmtiles_path.open("rb") as handle:
        for (zoom, x, y), data in all_tiles(MmapSource(handle)):
            payload = bytes(data)
            by_zoom[zoom].append(len(payload))
            raw_by_zoom[zoom].append(len(gzip.decompress(payload)))
            xs, ys = tile_range(zoom)
            if MIN_ZOOM <= zoom <= FIRST_SCREEN_MAX_ZOOM and x in xs and y in ys:
                first_screen += len(payload)
    return {
        "tiles_by_zoom": {str(z): len(sizes) for z, sizes in sorted(by_zoom.items())},
        "max_tile_bytes_gzip": {str(z): max(sizes) for z, sizes in sorted(by_zoom.items())},
        "max_tile_bytes_raw": {str(z): max(sizes) for z, sizes in sorted(raw_by_zoom.items())},
        "first_screen_z4_z6_bytes": first_screen,
    }

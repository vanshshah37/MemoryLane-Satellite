"""Fetch and window-extract Sentinel-2 L2A tiles for Manali–Solang AOI.

Reads Cloud Optimized GeoTIFF assets from AWS Open Data / CDSE over /vsicurl/
and extracts the exact buffered AOI window for each intersecting MGRS tile.
Saves complete, georeferenced, valid GeoTIFFs to data/raw/ ready for Phase 3 compositing.
"""

import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Dict, Sequence

from pyproj import Transformer
import rasterio
from rasterio.windows import from_bounds
import requests

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processing.build_composite import extract_radiometric_metadata
from scripts.download_sentinel import validate_raster_integrity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TARGET_BANDS = ("B02", "B03", "B04", "B08", "SCL")


def fetch_tile_aoi_bands(
    scene_data: Dict,
    aoi_bbox: Sequence[float] = (77.05, 32.15, 77.30, 32.38),
    buffer_meters: float = 3000.0,
    output_base_dir: Path = Path("data/raw"),
) -> Path:
    """Extracts AOI windowed rasters and metadata for a single MGRS tile."""
    scene_id = scene_data.get("scene_id", "UNKNOWN_SCENE")
    mgrs_tile = scene_data.get("mgrs_tile", "UNKNOWN_TILE")
    dt_str = scene_data.get("datetime", "")
    date_prefix = dt_str.split("T")[0].replace("-", "") if dt_str else "20241015"
    tile_dir = output_base_dir / f"{date_prefix}_{mgrs_tile}_{scene_id}"
    tile_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"=== Processing Tile {mgrs_tile} ({scene_id}) ===")

    # Calculate AOI bounds in UTM Zone 43N (EPSG:32643) with buffer
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
    min_x, min_y = transformer.transform(aoi_bbox[0], aoi_bbox[1])
    max_x, max_y = transformer.transform(aoi_bbox[2], aoi_bbox[3])
    min_x -= buffer_meters
    min_y -= buffer_meters
    max_x += buffer_meters
    max_y += buffer_meters

    assets = scene_data.get("assets", {})

    # 1. Download metadata XML
    xml_asset = assets.get("METADATA_XML", {})
    xml_url = xml_asset.get("href")
    xml_path = tile_dir / "MTD_TL.xml"
    if xml_url:
        logger.info(f"Downloading granule metadata XML from {xml_url}...")
        try:
            r = requests.get(xml_url, timeout=30)
            if r.status_code == 200:
                with open(xml_path, "wb") as f:
                    f.write(r.content)
                logger.info(f"Saved MTD_TL.xml ({xml_path.stat().st_size} bytes)")
        except Exception as e:
            logger.warning(f"Could not download metadata XML: {e}")

    # Extract radiometric metadata
    try:
        radio_params = extract_radiometric_metadata(
            xml_path if xml_path.is_file() else scene_data,
            target_bands=("B02", "B03", "B04", "B08"),
        )
    except Exception as e:
        logger.warning(f"Radiometric metadata fallback: {e}")
        radio_params = {
            b: {"quantification_value": 10000.0, "boa_add_offset": -1000.0}
            for b in ("B02", "B03", "B04", "B08")
        }

    radio_meta_path = tile_dir / "radiometric_metadata.json"
    with open(radio_meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scene_id": scene_id,
                "mgrs_tile": mgrs_tile,
                "processing_baseline": scene_data.get("processing_baseline", "05.11"),
                "radiometric_parameters": radio_params,
            },
            f,
            indent=2,
        )

    # Save scene metadata
    scene_meta_path = tile_dir / "scene_metadata.json"
    with open(scene_meta_path, "w", encoding="utf-8") as f:
        json.dump(scene_data, f, indent=2)

    # 2. Extract AOI window for each band
    env = rasterio.Env(
        GDAL_HTTP_MAX_RETRY=5,
        GDAL_HTTP_RETRY_DELAY=2,
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.jp2,.xml",
    )

    downloaded_files = {
        "radiometric_metadata": str(radio_meta_path),
        "scene_metadata": str(scene_meta_path),
    }

    with env:
        for band in TARGET_BANDS:
            asset = assets.get(band)
            if not asset or not asset.get("href"):
                raise RuntimeError(f"Missing required asset {band} for tile {mgrs_tile}!")

            href = asset["href"]
            dst_file = tile_dir / f"{band}.tif"

            # Check existing valid raster
            if dst_file.is_file() and dst_file.stat().st_size > 0 and validate_raster_integrity(dst_file):
                logger.info(f"Skipping existing verified raster: {dst_file.name} ({dst_file.stat().st_size} bytes)")
                downloaded_files[band] = str(dst_file)
                continue

            vsi_url = f"/vsicurl/{href}" if not href.startswith("/vsi") else href
            logger.info(f"Streaming AOI window for {mgrs_tile} {band} from {href}...")
            t0 = time.time()

            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    with rasterio.open(vsi_url) as src:
                        win = from_bounds(min_x, min_y, max_x, max_y, src.transform)
                        # Intersect with dataset boundary
                        full_win = rasterio.windows.Window(0, 0, src.width, src.height)
                        win = win.intersection(full_win)

                        # Read window data
                        band_data = src.read(1, window=win)
                        win_transform = rasterio.windows.transform(win, src.transform)
                        profile = src.profile.copy()
                        profile.update({
                            "driver": "GTiff",
                            "height": band_data.shape[0],
                            "width": band_data.shape[1],
                            "transform": win_transform,
                            "compress": "deflate",
                            "predictor": 2 if profile["dtype"] in ("uint16", "int16") else 1,
                        })

                        part_file = dst_file.with_suffix(".tif.part")
                        with rasterio.open(part_file, "w", **profile) as dst:
                            dst.write(band_data, 1)

                        if not validate_raster_integrity(part_file):
                            raise ValueError("Extracted raster failed integrity check.")

                        part_file.replace(dst_file)
                        logger.info(
                            f"Successfully saved {dst_file.name} ({dst_file.stat().st_size} bytes, "
                            f"{band_data.shape[1]}x{band_data.shape[0]} px) in {time.time()-t0:.1f}s"
                        )
                        downloaded_files[band] = str(dst_file)
                        break
                except Exception as e:
                    logger.warning(f"Attempt {attempt}/{max_retries} failed for {band}: {e}")
                    if attempt == max_retries:
                        raise RuntimeError(f"Failed to fetch AOI window for {band} after {max_retries} attempts: {e}") from e
                    time.sleep(2.0 * attempt)

    # Save download manifest
    manifest_path = tile_dir / "download_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "scene_id": scene_id,
            "mgrs_tile": mgrs_tile,
            "datetime": dt_str,
            "download_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": downloaded_files,
        }, f, indent=2)

    return tile_dir


def main():
    manifest_path = Path("data/search_manifest.json")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    tiles = manifest.get("tiles", {})
    logger.info(f"Starting AOI window extraction for {len(tiles)} MGRS tiles...")

    downloaded_dirs = []
    for tile_id, tile_data in tiles.items():
        selected = tile_data.get("selected_scene")
        if not selected:
            logger.warning(f"No selected scene for tile {tile_id}")
            continue
        tile_dir = fetch_tile_aoi_bands(selected)
        downloaded_dirs.append(tile_dir)

    logger.info("=== All MGRS Tile AOI Datasets Successfully Extracted! ===")
    for d in downloaded_dirs:
        logger.info(f"Tile ready at: {d}")


if __name__ == "__main__":
    main()

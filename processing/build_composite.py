"""Sentinel-2 L2A Preprocessing, Multi-Tile Mosaicking & Composite Generator.

Performs:
1. Input validation across MGRS tile acquisitions (43SFR, 43SGR), verifying band availability,
   acquisition dates, and metadata completeness.
2. Dynamic metadata extraction and metadata-aware BOA reflectance conversion (compute_boa_reflectance).
3. SCL QA & Cloud-Quality Masking with Class 7 inclusion, Snow Class 11 preservation,
   and configurable cloud-edge buffering.
4. Seamless multi-tile mosaicking aligning multiple MGRS tiles to a unified 10m UTM grid
   with overlap blending and hole-filling.
5. Natural color RGB synthesis with soft-knee highlight compression (preserving snow texture)
   and atmospheric color balancing without washed-out highlights or fluorescent green tints.
6. AOI spatial clipping preserving exact geotransform and spatial resolution (10.0m).
7. Decoupled product exports:
   - Analytical 4-band 16-bit reflectance GeoTIFF (B04, B03, B02, B08 at 10m).
   - Dedicated 1-band 8-bit categorical SCL QA GeoTIFF (10m).
   - Display-ready 4-band 8-bit RGBA GeoTIFF.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import xml.etree.ElementTree as ET

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy.ndimage import binary_dilation

logger = logging.getLogger(__name__)

# Standard Sentinel-2 Band ID mapping as defined in ESA Sentinel-2 PSD (Product Specification Document)
S2_BAND_ID_MAP = {
    0: "B01",
    1: "B02",
    2: "B03",
    3: "B04",
    4: "B05",
    5: "B06",
    6: "B07",
    7: "B08",
    8: "B8A",
    9: "B09",
    10: "B11",
    11: "B12",
}

REQUIRED_BANDS = ("B02", "B03", "B04", "B08")
REQUIRED_RASTERS = ("B02", "B03", "B04", "B08", "SCL")


# ---------------------------------------------------------------------------
# Section 1: Radiometric Metadata Extraction (Phase 1 Baseline)
# ---------------------------------------------------------------------------

def extract_radiometric_metadata(
    metadata_source: Union[str, Path, Dict[str, Any]],
    target_bands: Sequence[str] = ("B02", "B03", "B04", "B08"),
) -> Dict[str, Dict[str, float]]:
    """Extracts BOA_ADD_OFFSET and QUANTIFICATION_VALUE separately for each band."""
    if isinstance(metadata_source, dict):
        return _extract_from_stac(metadata_source, target_bands)
    elif isinstance(metadata_source, (str, Path)):
        source_str = str(metadata_source)
        if Path(source_str).is_file():
            tree = ET.parse(source_str)
            root = tree.getroot()
        else:
            root = ET.fromstring(source_str)
        return _extract_from_xml(root, target_bands)
    else:
        raise TypeError(f"Unsupported metadata source type: {type(metadata_source)}")


def _extract_from_xml(
    root: ET.Element,
    target_bands: Sequence[str],
) -> Dict[str, Dict[str, float]]:
    """Extracts per-band radiometric parameters from Sentinel-2 XML metadata."""
    quant_val = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "BOA_QUANTIFICATION_VALUE":
            try:
                quant_val = float(elem.text.strip())
                break
            except (ValueError, AttributeError):
                pass

    if quant_val is None or quant_val <= 0:
        quant_val = 10000.0

    band_offsets: Dict[str, float] = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in ("BOA_ADD_OFFSET_VALUES_LIST", "BOA_ADD_OFFSET_LIST"):
            for child in elem:
                child_tag = child.tag.split("}")[-1]
                if child_tag == "BOA_ADD_OFFSET":
                    band_id_attr = child.attrib.get("band_id")
                    band_name_attr = child.attrib.get("band_name")
                    try:
                        offset_val = float(child.text.strip())
                    except (ValueError, AttributeError):
                        continue

                    if band_id_attr is not None:
                        try:
                            b_name = S2_BAND_ID_MAP.get(int(band_id_attr))
                            if b_name:
                                band_offsets[b_name] = offset_val
                        except ValueError:
                            pass
                    elif band_name_attr is not None:
                        band_offsets[band_name_attr] = offset_val

    result: Dict[str, Dict[str, float]] = {}
    for band in target_bands:
        offset = band_offsets.get(band, 0.0)
        result[band] = {
            "quantification_value": quant_val,
            "boa_add_offset": offset,
        }

    return result


def _extract_from_stac(
    stac_item: Dict[str, Any],
    target_bands: Sequence[str],
) -> Dict[str, Dict[str, float]]:
    """Extracts per-band radiometric parameters from Copernicus CDSE STAC Item."""
    properties = stac_item.get("properties", {})
    assets = stac_item.get("assets", {})

    baseline_str = properties.get(
        "s2:processing_baseline",
        properties.get("processing:software", {}).get("sentinel2-l2a", "")
    )
    is_legacy_baseline = False
    if baseline_str:
        try:
            major_v = float(baseline_str.split(".")[0])
            if major_v < 4.0:
                is_legacy_baseline = True
        except (ValueError, IndexError):
            pass

    default_quant = float(properties.get("s2:boa_quantification_value", 10000.0))
    stac_offset_list = properties.get("s2:boa_add_offset_values_list", {})

    result: Dict[str, Dict[str, float]] = {}
    for band in target_bands:
        asset_info = assets.get(band, {})
        raster_bands = asset_info.get("raster:bands", [])
        
        offset = None
        quant = default_quant

        if raster_bands and isinstance(raster_bands[0], dict):
            scale = raster_bands[0].get("scale")
            if scale and scale > 0:
                quant = round(1.0 / scale, 1)
            offset = raster_bands[0].get("offset")

        if offset is None:
            if band in stac_offset_list:
                offset = float(stac_offset_list[band])
            elif "s2:boa_offset" in asset_info:
                offset = float(asset_info["s2:boa_offset"])
            elif is_legacy_baseline:
                offset = 0.0
            else:
                offset = -1000.0

        result[band] = {
            "quantification_value": quant,
            "boa_add_offset": float(offset),
        }

    return result


def compute_boa_reflectance(
    dn_array: np.ndarray,
    quantification_value: float = 10000.0,
    boa_add_offset: float = -1000.0,
) -> np.ndarray:
    """Computes Bottom-Of-Atmosphere (BOA) surface reflectance with baseline awareness.

    Formula: R = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE for DN > 0
    DN == 0 is strictly preserved as NoData (np.nan).
    """
    valid_mask = dn_array > 0
    reflectance = np.full(dn_array.shape, np.nan, dtype=np.float32)
    reflectance[valid_mask] = (
        dn_array[valid_mask].astype(np.float32) + float(boa_add_offset)
    ) / float(quantification_value)
    return reflectance


# ---------------------------------------------------------------------------
# Section 2: SCL Masking & Normalization (Phase 1 Baseline)
# ---------------------------------------------------------------------------

def build_cloud_quality_mask(
    scl_array: np.ndarray,
    mask_classes: Sequence[int] = (0, 1, 3, 7, 8, 9, 10),
    snow_class: int = 11,
    buffer_pixels: int = 3,
) -> np.ndarray:
    """Generates a boolean quality mask where True = masked/invalid, False = valid."""
    invalid = np.isin(scl_array, mask_classes)
    is_snow = (scl_array == snow_class)

    if buffer_pixels > 0:
        y, x = np.ogrid[-buffer_pixels : buffer_pixels + 1, -buffer_pixels : buffer_pixels + 1]
        kernel = (x * x + y * y) <= (buffer_pixels * buffer_pixels)
        buffered_invalid = binary_dilation(invalid, structure=kernel)
        final_invalid = buffered_invalid & (~is_snow)
    else:
        final_invalid = invalid

    return final_invalid


def resample_scl_nearest(
    scl_array: np.ndarray,
    target_shape: Tuple[int, int],
) -> np.ndarray:
    """Resamples 20m SCL categorical array to match 10m target grid using nearest neighbor."""
    h_target, w_target = target_shape
    h_src, w_src = scl_array.shape

    row_idx = (np.arange(h_target) * h_src / h_target).astype(int)
    col_idx = (np.arange(w_target) * w_src / w_target).astype(int)

    return scl_array[np.ix_(row_idx, col_idx)]


def normalize_to_8bit_rgb(
    reflectance: np.ndarray,
    r_min: float = 0.01,
    r_max: float = 0.32,
    gamma: float = 1.15,
) -> np.ndarray:
    """Applies fixed physical multi-temporal reflectance normalization to 8-bit unsigned integer."""
    clipped = np.clip((reflectance - r_min) / (r_max - r_min), 0.0, 1.0)
    stretched = np.power(clipped, 1.0 / gamma)
    rgb_8bit = np.round(stretched * 255.0).astype(np.uint8)
    return rgb_8bit


def build_display_rgba(
    red_refl: np.ndarray,
    green_refl: np.ndarray,
    blue_refl: np.ndarray,
    invalid_mask: np.ndarray,
    r_min: float = 0.01,
    r_max: float = 0.32,
    gamma: float = 1.15,
) -> np.ndarray:
    """Combines normalized RGB channels with an 8-bit Alpha transparency channel."""
    r = normalize_to_8bit_rgb(red_refl, r_min=r_min, r_max=r_max, gamma=gamma)
    g = normalize_to_8bit_rgb(green_refl, r_min=r_min, r_max=r_max, gamma=gamma)
    b = normalize_to_8bit_rgb(blue_refl, r_min=r_min, r_max=r_max, gamma=gamma)

    alpha = np.where(invalid_mask | np.isnan(red_refl), 0, 255).astype(np.uint8)
    return np.stack([r, g, b, alpha], axis=0)


def build_analytical_reflectance_stack(
    b04_refl: np.ndarray,
    b03_refl: np.ndarray,
    b02_refl: np.ndarray,
    b08_refl: np.ndarray,
    quantification_value: float = 10000.0,
) -> np.ndarray:
    """Builds a 4-band 16-bit unsigned integer analytical surface reflectance stack."""
    bands = [b04_refl, b03_refl, b02_refl, b08_refl]
    stacked = np.zeros((4, b04_refl.shape[0], b04_refl.shape[1]), dtype=np.uint16)

    for i, band in enumerate(bands):
        valid = ~np.isnan(band) & (band > 0)
        scaled = np.round(band * quantification_value)
        stacked[i] = np.where(valid, np.clip(scaled, 1, 65535), 0).astype(np.uint16)

    return stacked


# ---------------------------------------------------------------------------
# Section 3: Phase 3 Multi-Tile Input Validation & Ingestion
# ---------------------------------------------------------------------------

@dataclass
class TileDataset:
    tile_dir: Path
    mgrs_tile: str
    acquisition_date: str
    scene_id: str
    band_paths: Dict[str, Path]
    scl_path: Path
    radiometric_params: Dict[str, Dict[str, float]]
    processing_baseline: str


def validate_tile_inputs(
    tile_dirs: Sequence[Union[str, Path]],
    strict_date_match: bool = True,
) -> List[TileDataset]:
    """Validates downloaded tile directories, checking required bands, metadata and acquisition dates.

    Parameters:
        tile_dirs: List of paths to tile directories (e.g. data/raw/<date>_<tile>_<scene_id>/).
        strict_date_match: If True, raises ValueError when tiles have different acquisition dates.

    Returns:
        List of validated TileDataset objects.

    Raises:
        FileNotFoundError: If a required raster or metadata file is missing.
        ValueError: If dates are incompatible or metadata is corrupted.
    """
    if not tile_dirs:
        raise ValueError("No tile directories provided for composite generation.")

    validated: List[TileDataset] = []
    dates: List[str] = []

    for d in tile_dirs:
        t_path = Path(d)
        if not t_path.is_dir():
            raise FileNotFoundError(f"Tile directory does not exist: {t_path}")

        # Check required band files (.jp2 or .tif)
        band_paths: Dict[str, Path] = {}
        for b in REQUIRED_BANDS:
            found = None
            for ext in (".jp2", ".tif", ".tiff"):
                candidate = t_path / f"{b}{ext}"
                if candidate.is_file():
                    found = candidate
                    break
            if not found:
                raise FileNotFoundError(f"Required band {b} missing in tile directory: {t_path}")
            band_paths[b] = found

        # Check SCL
        scl_path = None
        for ext in (".jp2", ".tif", ".tiff"):
            candidate = t_path / f"SCL{ext}"
            if candidate.is_file():
                scl_path = candidate
                break
        if not scl_path:
            raise FileNotFoundError(f"Required SCL raster missing in tile directory: {t_path}")

        # Read metadata (radiometric_metadata.json or scene_metadata.json or XML)
        radio_json = t_path / "radiometric_metadata.json"
        scene_json = t_path / "scene_metadata.json"
        xml_file = t_path / "MTD_TL.xml"

        mgrs_tile = "UNKNOWN"
        acq_date = "UNKNOWN"
        scene_id = t_path.name
        baseline = "UNKNOWN"
        radio_params: Dict[str, Dict[str, float]] = {}

        if radio_json.is_file():
            with open(radio_json, "r", encoding="utf-8") as f:
                rdata = json.load(f)
            mgrs_tile = rdata.get("mgrs_tile", mgrs_tile)
            scene_id = rdata.get("scene_id", scene_id)
            baseline = rdata.get("processing_baseline", baseline)
            radio_params = rdata.get("radiometric_parameters", {})

        if scene_json.is_file():
            with open(scene_json, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            dt = sdata.get("datetime") or sdata.get("properties", {}).get("datetime", "")
            if dt:
                acq_date = dt.split("T")[0]
            if mgrs_tile == "UNKNOWN":
                mgrs_tile = sdata.get("mgrs_tile", "")

        # Fallback date extraction from directory name (e.g. 20241015_43SGR_...)
        if acq_date == "UNKNOWN":
            m = re.match(r"^(\d{4})(\d{2})(\d{2})_", t_path.name)
            if m:
                acq_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # If radiometric_params missing, extract on the fly
        if not radio_params:
            source = xml_file if xml_file.is_file() else (scene_json if scene_json.is_file() else None)
            if source:
                radio_params = extract_radiometric_metadata(source, target_bands=REQUIRED_BANDS)
            else:
                radio_params = {
                    b: {"quantification_value": 10000.0, "boa_add_offset": -1000.0}
                    for b in REQUIRED_BANDS
                }

        dates.append(acq_date)
        validated.append(
            TileDataset(
                tile_dir=t_path,
                mgrs_tile=mgrs_tile,
                acquisition_date=acq_date,
                scene_id=scene_id,
                band_paths=band_paths,
                scl_path=scl_path,
                radiometric_params=radio_params,
                processing_baseline=baseline,
            )
        )

    # Validate cross-tile dates
    unique_dates = set(dates)
    if len(unique_dates) > 1:
        msg = f"Incompatible acquisition dates across tiles: {dates}."
        if strict_date_match:
            raise ValueError(f"{msg} Set strict_date_match=False to allow heterogeneous dates.")
        else:
            logger.warning(f"{msg} Proceeding with multi-temporal mosaic.")

    return validated


# ---------------------------------------------------------------------------
# Section 4: Phase 3 Natural Color RGB Synthesis with Soft-Knee Shoulder
# ---------------------------------------------------------------------------

def enhance_natural_color_rgb(
    red_refl: np.ndarray,
    green_refl: np.ndarray,
    blue_refl: np.ndarray,
    invalid_mask: np.ndarray,
    r_min_rgb: Tuple[float, float, float] = (0.010, 0.012, 0.015),
    r_knee: float = 0.28,
    r_max: float = 0.85,
    gamma: float = 1.15,
) -> np.ndarray:
    """Synthesizes high-fidelity natural color RGB with soft-knee highlight compression.

    Addresses key visual issues in alpine Sentinel-2 composites:
    1. Atmospheric Scattering (Haze): Subtracts calibrated dark-object baselines (r_min per band).
    2. Fluorescent Green Suppression: Balances peak green vegetation reflectance (~560nm).
    3. Snow Highlight Preservation: Uses a soft-knee shoulder function above r_knee so snow,
       glaciers, and crevasse textures are preserved up to r_max without clipping to 255.
    4. Contrast & Vibrance: Applies smooth perceptual gamma curve and valid alpha channel.

    Returns:
        4-channel uint8 array [Red, Green, Blue, Alpha] of shape (4, H, W).
    """
    channels = [red_refl, green_refl, blue_refl]
    out_rgb = []

    for i, c in enumerate(channels):
        rmin = r_min_rgb[i]
        # Dark level subtraction
        c_sub = np.maximum(c - rmin, 0.0)

        # Tone-mapping curve with soft-knee highlight compression
        # For c_sub <= (r_knee - rmin): linear stretch
        # For c_sub >  (r_knee - rmin): asymptotic soft-knee shoulder
        k = r_knee - rmin
        m = r_max - rmin

        # Linear normalization up to knee
        linear_part = np.clip(c_sub / k, 0.0, 1.0) * 0.70

        # Soft shoulder for high reflectance (snow/ice)
        high_part = np.where(
            c_sub > k,
            0.70 + 0.30 * (1.0 - np.exp(-2.5 * (c_sub - k) / (m - k + 1e-6))),
            linear_part,
        )

        norm = np.clip(high_part, 0.0, 1.0)
        # Apply perceptual gamma
        stretched = np.power(norm, 1.0 / gamma)
        byte_val = np.round(stretched * 255.0).astype(np.uint8)
        out_rgb.append(byte_val)

    # Gentle vegetation white balance: slight green dampening (0.96) to prevent toxic greens
    r = out_rgb[0]
    g = np.clip(np.round(out_rgb[1].astype(np.float32) * 0.96), 0, 255).astype(np.uint8)
    b = out_rgb[2]

    # Alpha channel: 255 = valid, 0 = invalid / cloud / nodata
    alpha = np.where(invalid_mask | np.isnan(red_refl), 0, 255).astype(np.uint8)

    return np.stack([r, g, b, alpha], axis=0)


# ---------------------------------------------------------------------------
# Section 5: Phase 3 Multi-Tile Mosaicking & Grid Warping
# ---------------------------------------------------------------------------

def compute_snapped_aoi_grid(
    aoi_bbox: Sequence[float],
    target_crs: str = "EPSG:32643",
    resolution: float = 10.0,
) -> Tuple[Tuple[float, float, float, float], int, int, rasterio.Affine]:
    """Computes snapped integer grid coordinates and Affine transform for target AOI.

    Parameters:
        aoi_bbox: [min_lon, min_lat, max_lon, max_lat] in EPSG:4326.
        target_crs: Projected CRS (default: UTM Zone 43N / EPSG:32643).
        resolution: Spatial resolution in meters (default: 10.0m).

    Returns:
        ((min_x, min_y, max_x, max_y), width_px, height_px, transform)
    """
    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    min_x, min_y = transformer.transform(aoi_bbox[0], aoi_bbox[1])
    max_x, max_y = transformer.transform(aoi_bbox[2], aoi_bbox[3])

    # Snap bounds to integer multiples of resolution
    min_x = math.floor(min_x / resolution) * resolution
    max_y = math.ceil(max_y / resolution) * resolution
    max_x = math.ceil(max_x / resolution) * resolution
    min_y = math.floor(min_y / resolution) * resolution

    width = int(round((max_x - min_x) / resolution))
    height = int(round((max_y - min_y) / resolution))
    transform = from_origin(min_x, max_y, resolution, resolution)

    return (min_x, min_y, max_x, max_y), width, height, transform


def warp_tile_to_target_grid(
    src_raster_path: Path,
    dst_shape: Tuple[int, int],
    dst_transform: rasterio.Affine,
    dst_crs: str = "EPSG:32643",
    resampling: Resampling = Resampling.bilinear,
    src_nodata: Optional[float] = 0.0,
    dst_nodata: Optional[float] = 0.0,
) -> np.ndarray:
    """Warps an individual tile raster into the unified target grid."""
    dst_array = np.full(dst_shape, dst_nodata, dtype=np.float32)

    with rasterio.open(src_raster_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst_array,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=resampling,
            src_nodata=src_nodata,
            dst_nodata=dst_nodata,
        )

    return dst_array


def mosaic_tile_datasets(
    tiles: Sequence[TileDataset],
    target_bounds: Tuple[float, float, float, float],
    target_shape: Tuple[int, int],
    target_transform: rasterio.Affine,
    target_crs: str = "EPSG:32643",
    mask_classes: Sequence[int] = (0, 1, 3, 7, 8, 9, 10),
    snow_class: int = 11,
    buffer_pixels: int = 3,
) -> Dict[str, np.ndarray]:
    """Mosaics multiple MGRS tiles (e.g. 43SFR and 43SGR) into a seamless common grid.

    Blending & Overlap Logic:
    1. Each tile is warped to the exact 10m target grid.
    2. Radiometric correction is applied using each tile's specific baseline/offset.
    3. SCL mask is computed and dilated (protecting snow class 11).
    4. In overlap areas:
       - If valid in Tile A and invalid/cloud in Tile B -> Tile A fills the hole seamlessly.
       - If valid in both tiles -> blends as average of calibrated BOA reflectance.
       - If cloud/nodata in both -> masked.
    5. Returns dict with mosaicked 'B04', 'B03', 'B02', 'B08', 'SCL', and 'invalid_mask'.
    """
    height, width = target_shape
    bands_to_process = ("B04", "B03", "B02", "B08")

    # Accumulators for reflectance mosaicking
    band_sums = {b: np.zeros((height, width), dtype=np.float32) for b in bands_to_process}
    band_counts = {b: np.zeros((height, width), dtype=np.uint8) for b in bands_to_process}

    # SCL consensus layer: start with 0 (NoData)
    mosaic_scl = np.zeros((height, width), dtype=np.uint8)
    mosaic_invalid = np.ones((height, width), dtype=bool)

    for tile in tiles:
        logger.info(f"Mosaicking Tile {tile.mgrs_tile} ({tile.scene_id})...")

        # 1. Warp SCL to target grid using nearest neighbor
        tile_scl_raw = warp_tile_to_target_grid(
            tile.scl_path,
            dst_shape=target_shape,
            dst_transform=target_transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
            src_nodata=0,
            dst_nodata=0,
        )
        tile_scl = np.round(tile_scl_raw).astype(np.uint8)

        # 2. Build cloud quality mask for this tile
        tile_invalid = build_cloud_quality_mask(
            tile_scl,
            mask_classes=mask_classes,
            snow_class=snow_class,
            buffer_pixels=buffer_pixels,
        )
        tile_valid = ~tile_invalid

        # Update consensus SCL: replace nodata or invalid pixels with valid terrain
        scl_update_mask = (mosaic_scl == 0) | (~np.isin(tile_scl, mask_classes) & np.isin(mosaic_scl, mask_classes))
        mosaic_scl[scl_update_mask] = tile_scl[scl_update_mask]

        # 3. Warp and calibrate each analytical band
        for b in bands_to_process:
            b_path = tile.band_paths[b]
            raw_dn = warp_tile_to_target_grid(
                b_path,
                dst_shape=target_shape,
                dst_transform=target_transform,
                dst_crs=target_crs,
                resampling=Resampling.bilinear,
                src_nodata=0,
                dst_nodata=0,
            )

            # Radiometric calibration
            b_params = tile.radiometric_params.get(
                b, {"quantification_value": 10000.0, "boa_add_offset": -1000.0}
            )
            refl = compute_boa_reflectance(
                raw_dn,
                quantification_value=b_params["quantification_value"],
                boa_add_offset=b_params["boa_add_offset"],
            )

            # Valid contributing pixels: must have positive reflectance and not be cloud-masked
            valid_pixels = tile_valid & ~np.isnan(refl) & (refl > 0)
            band_sums[b][valid_pixels] += refl[valid_pixels]
            band_counts[b][valid_pixels] += 1

    # Compute blended average in overlap zones
    mosaicked_bands: Dict[str, np.ndarray] = {}
    for b in bands_to_process:
        cnt = band_counts[b]
        mosaicked = np.full((height, width), np.nan, dtype=np.float32)
        has_data = cnt > 0
        mosaicked[has_data] = band_sums[b][has_data] / cnt[has_data]
        mosaicked_bands[b] = mosaicked

    # Final combined invalid mask: any pixel with count == 0 across RGB is invalid
    final_invalid = band_counts["B04"] == 0
    mosaicked_bands["SCL"] = mosaic_scl
    mosaicked_bands["invalid_mask"] = final_invalid

    return mosaicked_bands


# ---------------------------------------------------------------------------
# Section 6: Phase 3 Composite Pipeline Orchestrator
# ---------------------------------------------------------------------------

def build_composite_pipeline(
    tile_dirs: Sequence[Union[str, Path]],
    aoi_bbox: Sequence[float] = (77.05, 32.15, 77.30, 32.38),
    output_dir: Union[str, Path] = "data/processed",
    target_crs: str = "EPSG:32643",
    strict_date_match: bool = False,
    cloud_buffer_pixels: int = 3,
) -> Dict[str, Path]:
    """End-to-end composite pipeline: Ingests tiles, mosaics, enhances natural RGB, and exports products.

    Outputs:
    1. <output_dir>/manali_solang_reflectance.tif: 4-band uint16 analytical reflectance GeoTIFF.
    2. <output_dir>/manali_solang_scl.tif: 1-band uint8 SCL QA GeoTIFF.
    3. <output_dir>/manali_solang_rgb_display.tif: 4-band uint8 RGBA display GeoTIFF.

    Returns:
        Dict mapping product name to output Path.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Input validation
    validated_tiles = validate_tile_inputs(tile_dirs, strict_date_match=strict_date_match)
    logger.info(
        f"Validated {len(validated_tiles)} tile(s): {[t.mgrs_tile for t in validated_tiles]} "
        f"(Dates: {[t.acquisition_date for t in validated_tiles]})"
    )

    # 2. Compute snapped AOI grid
    bounds, width, height, transform = compute_snapped_aoi_grid(
        aoi_bbox=aoi_bbox, target_crs=target_crs, resolution=10.0
    )
    logger.info(f"AOI Grid: {width}x{height} pixels, resolution=10.0m, bounds={bounds}")

    # 3. Mosaic tiles into common grid
    mosaicked = mosaic_tile_datasets(
        tiles=validated_tiles,
        target_bounds=bounds,
        target_shape=(height, width),
        target_transform=transform,
        target_crs=target_crs,
        buffer_pixels=cloud_buffer_pixels,
    )

    b04 = mosaicked["B04"]
    b03 = mosaicked["B03"]
    b02 = mosaicked["B02"]
    b08 = mosaicked["B08"]
    scl = mosaicked["SCL"]
    invalid_mask = mosaicked["invalid_mask"]

    # 4. Generate Products
    # A. Analytical 4-Band uint16 Reflectance GeoTIFF
    refl_path = out_dir / "manali_solang_reflectance.tif"
    analytical_stack = build_analytical_reflectance_stack(b04, b03, b02, b08, quantification_value=10000.0)
    with rasterio.open(
        refl_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=4,
        dtype="uint16",
        crs=target_crs,
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(analytical_stack)
        dst.set_band_description(1, "B04_Red_10m")
        dst.set_band_description(2, "B03_Green_10m")
        dst.set_band_description(3, "B02_Blue_10m")
        dst.set_band_description(4, "B08_NIR_10m")
    logger.info(f"Saved analytical reflectance stack to: {refl_path}")

    # B. Dedicated SCL QA GeoTIFF
    scl_path = out_dir / "manali_solang_scl.tif"
    with rasterio.open(
        scl_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs=target_crs,
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(scl, 1)
        dst.set_band_description(1, "SCL_Scene_Classification_10m")
    logger.info(f"Saved SCL QA raster to: {scl_path}")

    # C. Natural Color Display RGBA GeoTIFF
    display_path = out_dir / "manali_solang_rgb_display.tif"
    rgba = enhance_natural_color_rgb(
        red_refl=b04,
        green_refl=b03,
        blue_refl=b02,
        invalid_mask=invalid_mask,
    )
    with rasterio.open(
        display_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=4,
        dtype="uint8",
        crs=target_crs,
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(rgba)
        dst.set_band_description(1, "Red")
        dst.set_band_description(2, "Green")
        dst.set_band_description(3, "Blue")
        dst.set_band_description(4, "Alpha")
    logger.info(f"Saved natural color display RGBA to: {display_path}")

    return {
        "reflectance": refl_path,
        "scl": scl_path,
        "rgb_display": display_path,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel-2 Multi-Tile Composite Builder")
    parser.add_argument(
        "--tiles",
        nargs="+",
        required=True,
        help="Paths to downloaded raw tile directories (e.g. data/raw/*43SFR* data/raw/*43SGR*)",
    )
    parser.add_argument(
        "--aoi",
        type=str,
        default=os.getenv("AOI_BBOX", "77.05,32.15,77.30,32.38"),
        help="AOI BBOX: min_lon,min_lat,max_lon,max_lat",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/processed",
        help="Output directory for processed rasters",
    )
    parser.add_argument(
        "--strict-dates",
        action="store_true",
        help="Enforce exact acquisition date match across all tiles",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    aoi = [float(x.strip()) for x in args.aoi.split(",")]
    tile_paths = [Path(p) for p in args.tiles]

    outputs = build_composite_pipeline(
        tile_dirs=tile_paths,
        aoi_bbox=aoi,
        output_dir=args.output_dir,
        strict_date_match=args.strict_dates,
    )

    print("\nComposite Pipeline Execution Complete:")
    for k, v in outputs.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

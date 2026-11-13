"""Sentinel-2 L2A Preprocessing & Composite Generator.

Performs:
1. Dynamic metadata extraction of BOA_ADD_OFFSET and BOA_QUANTIFICATION_VALUE per band
   from Sentinel-2 XML metadata (MTD_TL.xml / MTD_MSIL2A.xml) or STAC Item metadata.
2. Metadata-aware BOA reflectance conversion accounting for processing baseline differences.
3. SCL QA & Cloud-Quality Masking with Class 7 inclusion, Snow Class 11 preservation,
   and configurable cloud-edge buffering.
4. Multi-temporal fixed physical BOA reflectance color normalization.
5. Export of three decoupled products:
   - Analytical 4-band 16-bit reflectance (B04, B03, B02, B08 at 10m).
   - Dedicated 1-band 8-bit categorical SCL QA raster.
   - Display-ready 4-band 8-bit RGBA raster.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Union
import xml.etree.ElementTree as ET
import numpy as np
from scipy.ndimage import binary_dilation

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


def extract_radiometric_metadata(
    metadata_source: Union[str, Path, Dict[str, Any]],
    target_bands: Sequence[str] = ("B02", "B03", "B04", "B08"),
) -> Dict[str, Dict[str, float]]:
    """Extracts BOA_ADD_OFFSET and QUANTIFICATION_VALUE separately for each band.

    Extracts genuine radiometric parameters from Sentinel-2 XML metadata
    (MTD_TL.xml or MTD_MSIL2A.xml) or STAC Item dictionaries without hardcoded assumptions.

    Parameters:
        metadata_source: XML string, file path (str or Path), or STAC Item dictionary.
        target_bands: List of band identifiers to extract (default: B02, B03, B04, B08).

    Returns:
        Dict mapping each band name to its specific quantification_value and boa_add_offset:
        {
            "B02": {"quantification_value": 10000.0, "boa_add_offset": -1000.0},
            ...
        }
    """
    if isinstance(metadata_source, dict):
        return _extract_from_stac(metadata_source, target_bands)
    elif isinstance(metadata_source, (str, Path)):
        # Check if it's an existing file path or raw XML content
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
    # Find BOA_QUANTIFICATION_VALUE
    quant_val = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]  # Strip XML namespace if present
        if tag == "BOA_QUANTIFICATION_VALUE":
            try:
                quant_val = float(elem.text.strip())
                break
            except (ValueError, AttributeError):
                pass

    if quant_val is None or quant_val <= 0:
        quant_val = 10000.0

    # Find BOA_ADD_OFFSET_VALUES_LIST (introduced in Baseline >= 04.00)
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

    # If BOA_ADD_OFFSET is absent (Baseline < 04.00), offset is 0.0 for all bands
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

    # Check baseline string (e.g. "04.00", "05.00", "03.01")
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
        # Check per-asset raster extension first: assets[band]["raster:bands"][0]
        asset_info = assets.get(band, {})
        raster_bands = asset_info.get("raster:bands", [])
        
        offset = None
        quant = default_quant

        if raster_bands and isinstance(raster_bands[0], dict):
            scale = raster_bands[0].get("scale")
            if scale and scale > 0:
                quant = round(1.0 / scale, 1)
            offset = raster_bands[0].get("offset")

        # Fallback to STAC properties or asset extra fields
        if offset is None:
            if band in stac_offset_list:
                offset = float(stac_offset_list[band])
            elif "s2:boa_offset" in asset_info:
                offset = float(asset_info["s2:boa_offset"])
            elif is_legacy_baseline:
                offset = 0.0
            else:
                # Post-04.00 standard default if unlisted
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

    Formula:
        R = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE for DN > 0
        DN == 0 is strictly preserved as NoData (np.nan).

    Processing Baseline context:
        - Baseline < 04.00 (pre-Jan 25, 2022): BOA_ADD_OFFSET = 0.0, QUANTIFICATION_VALUE = 10000.0
        - Baseline >= 04.00 (post-Jan 25, 2022): BOA_ADD_OFFSET = -1000.0, QUANTIFICATION_VALUE = 10000.0
    """
    valid_mask = dn_array > 0
    reflectance = np.full(dn_array.shape, np.nan, dtype=np.float32)
    reflectance[valid_mask] = (
        dn_array[valid_mask].astype(np.float32) + float(boa_add_offset)
    ) / float(quantification_value)
    return reflectance


def build_cloud_quality_mask(
    scl_array: np.ndarray,
    mask_classes: Sequence[int] = (0, 1, 3, 7, 8, 9, 10),
    snow_class: int = 11,
    buffer_pixels: int = 3,
) -> np.ndarray:
    """Generates a boolean quality mask where True = masked/invalid, False = valid.

    Policy:
        - Masks configured bad/cloud classes (default: 0=NoData, 1=Defective, 3=Shadow,
          7=Unclassified/marginal haze, 8=Med cloud, 9=High cloud, 10=Cirrus).
        - Strictly preserves snow/ice class 11 (essential for Solang Valley & Rohtang peaks).
        - Applies configurable morphological circular dilation on cloud boundaries to eliminate
          penumbra while safeguarding snow pixels from erosion.
    """
    invalid = np.isin(scl_array, mask_classes)
    is_snow = (scl_array == snow_class)

    if buffer_pixels > 0:
        y, x = np.ogrid[-buffer_pixels : buffer_pixels + 1, -buffer_pixels : buffer_pixels + 1]
        kernel = (x * x + y * y) <= (buffer_pixels * buffer_pixels)
        buffered_invalid = binary_dilation(invalid, structure=kernel)
        # Protect snow from erosion
        final_invalid = buffered_invalid & (~is_snow)
    else:
        final_invalid = invalid

    return final_invalid


def normalize_to_8bit_rgb(
    reflectance: np.ndarray,
    r_min: float = 0.01,
    r_max: float = 0.32,
    gamma: float = 1.15,
) -> np.ndarray:
    """Applies fixed physical multi-temporal reflectance normalization to 8-bit unsigned integer.

    Instead of per-scene percentile stretching (which causes temporal flickering),
    this maps physical surface reflectance values to standard [0, 255] using fixed
    terrestrial reflectance bounds and perceptual gamma correction.
    """
    clipped = np.clip((reflectance - r_min) / (r_max - r_min), 0.0, 1.0)
    stretched = np.power(clipped, 1.0 / gamma)
    rgb_8bit = np.round(stretched * 255.0).astype(np.uint8)
    return rgb_8bit


def resample_scl_nearest(
    scl_array: np.ndarray,
    target_shape: Tuple[int, int],
) -> np.ndarray:
    """Resamples 20m SCL categorical array to match 10m target grid using nearest neighbor.

    Guarantees categorical integrity: no interpolation of non-existent class numbers.
    """
    h_target, w_target = target_shape
    h_src, w_src = scl_array.shape

    row_idx = (np.arange(h_target) * h_src / h_target).astype(int)
    col_idx = (np.arange(w_target) * w_src / w_target).astype(int)

    return scl_array[np.ix_(row_idx, col_idx)]


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
    """Builds a 4-band 16-bit unsigned integer analytical surface reflectance stack.

    Pixel values: DN = round(R * quantification_value), with 0 reserved strictly as NoData.
    """
    bands = [b04_refl, b03_refl, b02_refl, b08_refl]
    stacked = np.zeros((4, b04_refl.shape[0], b04_refl.shape[1]), dtype=np.uint16)

    for i, band in enumerate(bands):
        valid = ~np.isnan(band) & (band > 0)
        scaled = np.round(band * quantification_value)
        stacked[i] = np.where(valid, np.clip(scaled, 1, 65535), 0).astype(np.uint16)

    return stacked

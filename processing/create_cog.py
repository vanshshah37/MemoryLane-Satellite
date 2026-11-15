"""Cloud Optimized GeoTIFF (COG) Generator & Validator.

Converts processed GeoTIFFs (natural color RGB, analytical reflectance, and SCL QA)
into production-ready Cloud Optimized GeoTIFFs (COG) using rio-cogeo.

Features:
- Windows-verified lossless compression: DEFLATE with PREDICTOR=2.
- Power-of-2 internal pyramid overviews (2, 4, 8, 16, 32).
- Internal 512x512 pixel tiling for high-performance HTTP range read requests.
- Automatic verification with rio_cogeo.cogeo.cog_validate().
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union

from dotenv import load_dotenv
import rasterio
from rio_cogeo.cogeo import cog_translate, cog_validate
from rio_cogeo.profiles import cog_profiles

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_COG_DIR = os.getenv("COG_OUTPUT_DIR", "cog")
DEFAULT_COMPRESSION = os.getenv("COG_COMPRESSION", "DEFLATE").lower()
DEFAULT_PREDICTOR = int(os.getenv("COG_PREDICTOR", "2"))


def convert_to_cog(
    src_path: Union[str, Path],
    dst_path: Union[str, Path],
    profile: str = DEFAULT_COMPRESSION,
    predictor: int = DEFAULT_PREDICTOR,
    overview_resampling: str = "average",
    block_size: int = 512,
    nodata: Optional[float] = None,
) -> Path:
    """Converts a standard GeoTIFF into a production Cloud Optimized GeoTIFF.

    Parameters:
        src_path: Path to input GeoTIFF.
        dst_path: Path to output COG file.
        profile: Compression profile ('deflate', 'lzw', 'webp').
        predictor: Differencing predictor (2 for integer data, 3 for floating point).
        overview_resampling: Resampling method for overviews ('average', 'nearest', 'bilinear').
        block_size: Internal tile block size (default: 512).
        nodata: Optional nodata override.

    Returns:
        Path to validated COG.

    Raises:
        RuntimeError: If COG creation or cog_validate() fails.
    """
    src = Path(src_path)
    dst = Path(dst_path)

    if not src.is_file():
        raise FileNotFoundError(f"Source GeoTIFF not found: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Converting {src.name} to Cloud Optimized GeoTIFF at {dst}...")

    # Load base profile (deflate, lzw, etc.)
    try:
        base_profile = cog_profiles.get(profile.lower())
    except Exception:
        base_profile = cog_profiles.get("deflate")
    out_profile = dict(base_profile)
    out_profile.update({
        "blockxsize": block_size,
        "blockysize": block_size,
    })

    # Predictor 2 is standard horizontal differencing for integer rasters
    if "predictor" in out_profile or profile.lower() == "deflate":
        out_profile["predictor"] = predictor

    # Temporary configuration options
    config = {
        "GDAL_NUM_THREADS": "ALL_CPUS",
        "GDAL_TIFF_INTERNAL_MASK": "TRUE",
    }

    try:
        cog_translate(
            source=src,
            dst_path=dst,
            dst_kwargs=out_profile,
            config=config,
            overview_resampling=overview_resampling,
            nodata=nodata,
            quiet=True,
        )
    except Exception as e:
        if dst.exists():
            dst.unlink()
        raise RuntimeError(f"Failed to create COG from {src}: {e}") from e

    # Validate generated COG
    is_valid, errors, warnings = cog_validate(str(dst))
    if not is_valid or errors:
        if dst.exists():
            dst.unlink()
        raise RuntimeError(f"Generated COG {dst.name} failed validation! Errors: {errors}")

    logger.info(f"COG successfully generated and validated: {dst.name} ({dst.stat().st_size} bytes)")
    return dst


def create_all_cogs(
    processed_dir: Union[str, Path] = "data/processed",
    cog_output_dir: Union[str, Path] = DEFAULT_COG_DIR,
) -> Dict[str, Path]:
    """Converts display RGB and analytical reflectance rasters into COGs."""
    p_dir = Path(processed_dir)
    c_dir = Path(cog_output_dir)
    c_dir.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Path] = {}

    # 1. Display RGB COG
    rgb_src = p_dir / "manali_solang_rgb_display.tif"
    if rgb_src.is_file():
        rgb_dst = c_dir / "manali_solang_rgb.tif"
        results["rgb_display"] = convert_to_cog(
            src_path=rgb_src,
            dst_path=rgb_dst,
            overview_resampling="average",
        )

    # 2. Analytical Reflectance COG
    refl_src = p_dir / "manali_solang_reflectance.tif"
    if refl_src.is_file():
        refl_dst = c_dir / "manali_solang_reflectance.tif"
        results["reflectance"] = convert_to_cog(
            src_path=refl_src,
            dst_path=refl_dst,
            overview_resampling="average",
        )

    # 3. SCL QA COG
    scl_src = p_dir / "manali_solang_scl.tif"
    if scl_src.is_file():
        scl_dst = c_dir / "manali_solang_scl.tif"
        results["scl"] = convert_to_cog(
            src_path=scl_src,
            dst_path=scl_dst,
            overview_resampling="nearest",  # Categorical nearest neighbor
        )

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel-2 Cloud Optimized GeoTIFF Builder")
    parser.add_argument(
        "--input",
        type=str,
        default="data/processed/manali_solang_rgb_display.tif",
        help="Input GeoTIFF to convert to COG",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="cog/manali_solang_rgb.tif",
        help="Output COG path",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Convert all processed products (display, reflectance, scl) to COGs",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.all:
        logger.info("Converting all processed rasters to COGs...")
        outputs = create_all_cogs()
        print("\nAll COGs generated successfully:")
        for k, v in outputs.items():
            print(f"  {k}: {v}")
    else:
        out_path = convert_to_cog(args.input, args.output)
        print(f"\nCOG generated successfully: {out_path}")


if __name__ == "__main__":
    main()

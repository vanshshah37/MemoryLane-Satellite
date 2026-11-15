"""Unit and integration tests for Phase 3: Satellite Composite Generation.

Tests multi-tile mosaicking, overlap blending, cloud masking hole-filling,
natural color RGB synthesis with soft-knee highlight compression, AOI grid snapping,
and Cloud Optimized GeoTIFF conversion with cog_validate().
"""

import json
import os
from pathlib import Path
import tempfile

import numpy as np
from pyproj import Transformer
import pytest
import rasterio
from rasterio.transform import from_origin
from rio_cogeo.cogeo import cog_validate

from processing.build_composite import (
    validate_tile_inputs,
    compute_snapped_aoi_grid,
    enhance_natural_color_rgb,
    mosaic_tile_datasets,
    build_composite_pipeline,
)
from processing.create_cog import convert_to_cog, create_all_cogs


def create_synthetic_tile_fixture(
    tile_dir: Path,
    mgrs_tile: str,
    origin_x: float,
    origin_y: float,
    width_px: int = 100,
    height_px: int = 100,
    resolution: float = 10.0,
    acquisition_date: str = "2024-10-15",
    cloud_at_overlap: bool = False,
    snow_patch: bool = False,
) -> Path:
    """Helper to create a synthetic Sentinel-2 L2A tile directory with genuine GeoTIFFs and metadata."""
    tile_dir.mkdir(parents=True, exist_ok=True)
    transform = from_origin(origin_x, origin_y, resolution, resolution)

    # Base surface reflectance DNs (scale: 10000, Baseline 04.00 offset -1000 -> DN = R * 10000 + 1000)
    # Vegetation: R ~ 0.15 -> DN ~ 2500
    b04_data = np.full((height_px, width_px), 2500, dtype=np.uint16)
    b03_data = np.full((height_px, width_px), 2300, dtype=np.uint16)
    b02_data = np.full((height_px, width_px), 2000, dtype=np.uint16)
    b08_data = np.full((height_px, width_px), 4000, dtype=np.uint16)
    scl_data = np.full((height_px, width_px), 4, dtype=np.uint8)  # Class 4: Vegetation

    if snow_patch:
        # High snow peak (Class 11, R ~ 0.85 -> DN ~ 9500)
        scl_data[10:30, 10:30] = 11
        b04_data[10:30, 10:30] = 9500
        b03_data[10:30, 10:30] = 9500
        b02_data[10:30, 10:30] = 9500
        b08_data[10:30, 10:30] = 9500

    if cloud_at_overlap:
        # Cloud patch in the rightmost columns (Class 9: Cloud high prob)
        scl_data[:, -30:] = 9
        b04_data[:, -30:] = 6000

    bands = {
        "B04": b04_data,
        "B03": b03_data,
        "B02": b02_data,
        "B08": b08_data,
        "SCL": scl_data,
    }

    for b_name, b_arr in bands.items():
        dtype = "uint8" if b_name == "SCL" else "uint16"
        p = tile_dir / f"{b_name}.tif"
        with rasterio.open(
            p, "w", driver="GTiff", height=height_px, width=width_px,
            count=1, dtype=dtype, crs="EPSG:32643", transform=transform, nodata=0
        ) as dst:
            dst.write(b_arr, 1)

    # Radiometric metadata JSON
    radio_path = tile_dir / "radiometric_metadata.json"
    with open(radio_path, "w", encoding="utf-8") as f:
        json.dump({
            "scene_id": f"S2B_MSIL2A_{acquisition_date.replace('-', '')}_{mgrs_tile}",
            "mgrs_tile": mgrs_tile,
            "processing_baseline": "05.11",
            "radiometric_parameters": {
                b: {"quantification_value": 10000.0, "boa_add_offset": -1000.0}
                for b in ("B02", "B03", "B04", "B08")
            }
        }, f, indent=2)

    # Scene metadata JSON
    scene_path = tile_dir / "scene_metadata.json"
    with open(scene_path, "w", encoding="utf-8") as f:
        json.dump({
            "datetime": f"{acquisition_date}T05:27:19.024Z",
            "mgrs_tile": mgrs_tile,
            "properties": {"datetime": f"{acquisition_date}T05:27:19.024Z"}
        }, f, indent=2)

    return tile_dir


class TestCompositeInputValidation:
    """Tests tile input validation and date consistency checks."""

    def test_validate_tile_inputs_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = create_synthetic_tile_fixture(
                Path(tmpdir) / "20241015_43SFR_S2B",
                mgrs_tile="43SFR", origin_x=690000, origin_y=3580000
            )
            t2 = create_synthetic_tile_fixture(
                Path(tmpdir) / "20241015_43SGR_S2B",
                mgrs_tile="43SGR", origin_x=700000, origin_y=3580000
            )

            validated = validate_tile_inputs([t1, t2], strict_date_match=True)
            assert len(validated) == 2
            assert validated[0].mgrs_tile == "43SFR"
            assert validated[1].mgrs_tile == "43SGR"
            assert validated[0].acquisition_date == "2024-10-15"

    def test_missing_band_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = create_synthetic_tile_fixture(
                Path(tmpdir) / "20241015_43SFR_S2B",
                mgrs_tile="43SFR", origin_x=690000, origin_y=3580000
            )
            # Remove B08
            (t1 / "B08.tif").unlink()

            with pytest.raises(FileNotFoundError) as exc:
                validate_tile_inputs([t1])
            assert "Required band B08 missing" in str(exc.value)

    def test_heterogeneous_dates_handling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = create_synthetic_tile_fixture(
                Path(tmpdir) / "20241015_43SFR_S2B",
                mgrs_tile="43SFR", origin_x=690000, origin_y=3580000,
                acquisition_date="2024-10-15"
            )
            t2 = create_synthetic_tile_fixture(
                Path(tmpdir) / "20241013_43SGR_S2A",
                mgrs_tile="43SGR", origin_x=700000, origin_y=3580000,
                acquisition_date="2024-10-13"
            )

            # Strict mode must raise ValueError
            with pytest.raises(ValueError) as exc:
                validate_tile_inputs([t1, t2], strict_date_match=True)
            assert "Incompatible acquisition dates" in str(exc.value)

            # Non-strict mode succeeds with warning
            validated = validate_tile_inputs([t1, t2], strict_date_match=False)
            assert len(validated) == 2


class TestGridSnappingAndNaturalRGB:
    """Tests grid snapping and soft-knee natural RGB color enhancement."""

    def test_compute_snapped_aoi_grid(self):
        bbox = [77.05, 32.15, 77.30, 32.38]
        bounds, w, h, transform = compute_snapped_aoi_grid(bbox, target_crs="EPSG:32643", resolution=10.0)

        assert w > 2000
        assert h > 2000
        # Origins must be exact multiples of 10.0m
        assert transform.c % 10.0 == 0.0
        assert transform.f % 10.0 == 0.0

    def test_enhance_natural_color_rgb_soft_knee_preserves_snow(self):
        # Shape: 10x10. Dark pine forest (0.04), bare soil (0.18), and bright snow (0.85)
        shape = (10, 10)
        red = np.full(shape, 0.15, dtype=np.float32)
        green = np.full(shape, 0.14, dtype=np.float32)
        blue = np.full(shape, 0.10, dtype=np.float32)

        # Add snow patch
        red[0:3, 0:3] = 0.85
        green[0:3, 0:3] = 0.85
        blue[0:3, 0:3] = 0.85

        # Add cloud-masked pixel
        invalid_mask = np.zeros(shape, dtype=bool)
        invalid_mask[9, 9] = True

        rgba = enhance_natural_color_rgb(red, green, blue, invalid_mask)

        assert rgba.shape == (4, 10, 10)
        assert rgba.dtype == np.uint8

        # Valid terrain pixel has alpha 255
        assert rgba[3, 5, 5] == 255
        # Masked pixel has alpha 0
        assert rgba[3, 9, 9] == 0

        # Snow pixel [0, 0] should be bright but smoothly compressed (>= 230 and <= 255)
        assert rgba[0, 0, 0] >= 230


class TestMultiTileMosaickingAndHoleFilling:
    """Tests seamless mosaicking of overlapping tiles 43SFR and 43SGR."""

    def test_mosaicking_blends_overlap_and_fills_cloud_hole(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Tile 1: 43SFR on the left (origin_x=690000, width 1000m -> x from 690000 to 691000)
            # Has cloud in its rightmost 300m (x from 690700 to 691000)
            t1 = create_synthetic_tile_fixture(
                Path(tmpdir) / "tile_sfr",
                mgrs_tile="43SFR", origin_x=690000, origin_y=3580000,
                width_px=100, height_px=100, resolution=10.0,
                cloud_at_overlap=True, snow_patch=True
            )

            # Tile 2: 43SGR on the right (origin_x=690700, width 1000m -> x from 690700 to 691700)
            # Clear everywhere, overlapping with Tile 1 in x: 690700 to 691000 (300m overlap)
            t2 = create_synthetic_tile_fixture(
                Path(tmpdir) / "tile_sgr",
                mgrs_tile="43SGR", origin_x=690700, origin_y=3580000,
                width_px=100, height_px=100, resolution=10.0,
                cloud_at_overlap=False, snow_patch=False
            )

            validated = validate_tile_inputs([t1, t2])

            # Target mosaic grid covering both tiles: x from 690000 to 691700 (170 pixels)
            target_transform = from_origin(690000, 3580000, 10.0, 10.0)
            target_shape = (100, 170)
            target_bounds = (690000, 3579000, 691700, 3580000)

            mosaic = mosaic_tile_datasets(
                tiles=validated,
                target_bounds=target_bounds,
                target_shape=target_shape,
                target_transform=target_transform,
                target_crs="EPSG:32643",
                buffer_pixels=1,
            )

            b04_mosaic = mosaic["B04"]
            scl_mosaic = mosaic["SCL"]
            invalid = mosaic["invalid_mask"]

            assert b04_mosaic.shape == (100, 170)
            # Left side (covered only by Tile 1) has valid data
            assert not np.isnan(b04_mosaic[50, 20])
            # Right side (covered only by Tile 2) has valid data
            assert not np.isnan(b04_mosaic[50, 150])

            # In the overlap zone (col index 80 to 95 where Tile 1 had a cloud):
            # Tile 2 must have filled the cloud hole seamlessly!
            assert not np.isnan(b04_mosaic[50, 90])
            assert not invalid[50, 90]

            # Snow patch from Tile 1 (rows 10-30, cols 10-30) must be preserved!
            assert scl_mosaic[15, 15] == 11
            assert not invalid[15, 15]


class TestEndToEndCompositePipelineAndCOG:
    """Tests high-level composite pipeline execution and COG generation."""

    def test_pipeline_and_cog_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir) / "raw"
            processed_dir = Path(tmpdir) / "processed"
            cog_dir = Path(tmpdir) / "cog"

            # Create synthetic tiles
            t1 = create_synthetic_tile_fixture(
                raw_dir / "20241015_43SFR_S2B",
                mgrs_tile="43SFR", origin_x=693330, origin_y=3584890,
                width_px=80, height_px=80, resolution=10.0, snow_patch=True
            )
            t2 = create_synthetic_tile_fixture(
                raw_dir / "20241015_43SGR_S2B",
                mgrs_tile="43SGR", origin_x=693800, origin_y=3584890,
                width_px=80, height_px=80, resolution=10.0
            )

            # Run composite pipeline over mini test bbox
            # Coordinates inside synthetic tile bounds
            transformer = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)
            min_lon, min_lat = transformer.transform(693340, 3584300)
            max_lon, max_lat = transformer.transform(694200, 3584880)

            outputs = build_composite_pipeline(
                tile_dirs=[t1, t2],
                aoi_bbox=(min_lon, min_lat, max_lon, max_lat),
                output_dir=processed_dir,
                target_crs="EPSG:32643",
                strict_date_match=True,
            )

            assert "reflectance" in outputs
            assert "scl" in outputs
            assert "rgb_display" in outputs

            assert outputs["reflectance"].is_file()
            assert outputs["scl"].is_file()
            assert outputs["rgb_display"].is_file()

            # Verify analytical reflectance properties
            with rasterio.open(outputs["reflectance"]) as src:
                assert src.count == 4
                assert src.dtypes[0] == "uint16"
                assert src.crs.to_string() == "EPSG:32643"
                assert src.nodata == 0

            # Verify display RGBA properties
            with rasterio.open(outputs["rgb_display"]) as src:
                assert src.count == 4
                assert src.dtypes[0] == "uint8"
                assert src.crs.to_string() == "EPSG:32643"

            # Convert to COG using processing/create_cog.py
            cog_results = create_all_cogs(
                processed_dir=processed_dir,
                cog_output_dir=cog_dir,
            )

            assert "rgb_display" in cog_results
            assert "reflectance" in cog_results
            assert "scl" in cog_results

            # Validate generated COGs
            for cog_type, cog_file in cog_results.items():
                assert cog_file.is_file()
                is_valid, errors, warnings = cog_validate(str(cog_file))
                assert is_valid, f"COG validation failed for {cog_type}: {errors}"
                assert len(errors) == 0

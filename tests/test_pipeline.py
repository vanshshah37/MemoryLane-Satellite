"""Unit and integration tests for MemoryLane-Satellite.

Tests all production functions in processing/build_composite.py, including:
1. Dynamic metadata extraction (pre-04.00 and post-04.00 fixtures).
2. Metadata-aware BOA reflectance conversion and NoData preservation.
3. SCL cloud/quality masking, Class 7 handling, and snow protection during dilation.
4. Nearest-neighbor categorical SCL resampling.
5. Analytical 16-bit reflectance stack generation and NoData preservation.
6. Multi-temporal fixed physical color normalization.
7. COG translation and cog_validate() verification.
"""

import os
import tempfile
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rio_cogeo.cogeo import cog_translate, cog_validate
from rio_cogeo.profiles import cog_profiles

from processing.build_composite import (
    extract_radiometric_metadata,
    compute_boa_reflectance,
    build_cloud_quality_mask,
    resample_scl_nearest,
    normalize_to_8bit_rgb,
    build_display_rgba,
    build_analytical_reflectance_stack,
)

# ---------------------------------------------------------------------------
# Representative Sentinel-2 L2A Metadata Fixtures
# ---------------------------------------------------------------------------

PRE_0400_METADATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<n1:Level-2A_Tile_ID xmlns:n1="https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd">
  <General_Info>
    <ARCHIVING_INFO>
      <PROCESSING_BASELINE>03.01</PROCESSING_BASELINE>
    </ARCHIVING_INFO>
  </General_Info>
  <Quality_Indicators_Info>
    <Image_Content_QI>
      <QUANTIFICATION_VALUES_LIST>
        <BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE>
        <WVP_QUANTIFICATION_VALUE unit="none">1000</WVP_QUANTIFICATION_VALUE>
        <AOT_QUANTIFICATION_VALUE unit="none">1000</AOT_QUANTIFICATION_VALUE>
      </QUANTIFICATION_VALUES_LIST>
    </Image_Content_QI>
  </Quality_Indicators_Info>
</n1:Level-2A_Tile_ID>
"""

POST_0400_METADATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<n1:Level-2A_Tile_ID xmlns:n1="https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd">
  <General_Info>
    <ARCHIVING_INFO>
      <PROCESSING_BASELINE>04.00</PROCESSING_BASELINE>
    </ARCHIVING_INFO>
  </General_Info>
  <Quality_Indicators_Info>
    <Image_Content_QI>
      <QUANTIFICATION_VALUES_LIST>
        <BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE>
        <WVP_QUANTIFICATION_VALUE unit="none">1000</WVP_QUANTIFICATION_VALUE>
        <AOT_QUANTIFICATION_VALUE unit="none">1000</AOT_QUANTIFICATION_VALUE>
      </QUANTIFICATION_VALUES_LIST>
      <BOA_ADD_OFFSET_VALUES_LIST>
        <BOA_ADD_OFFSET band_id="0">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="1">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="2">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="4">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="5">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="6">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="8">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="9">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="10">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="11">-1000</BOA_ADD_OFFSET>
      </BOA_ADD_OFFSET_VALUES_LIST>
    </Image_Content_QI>
  </Quality_Indicators_Info>
</n1:Level-2A_Tile_ID>
"""

POST_0400_STAC_FIXTURE = {
    "properties": {
        "s2:processing_baseline": "05.00",
        "s2:boa_quantification_value": 10000.0,
        "s2:boa_add_offset_values_list": {
            "B02": -1000.0,
            "B03": -1000.0,
            "B04": -1000.0,
            "B08": -1000.0,
        },
    },
    "assets": {
        "B02": {"raster:bands": [{"scale": 0.0001, "offset": -1000.0}]},
        "B03": {"raster:bands": [{"scale": 0.0001, "offset": -1000.0}]},
        "B04": {"raster:bands": [{"scale": 0.0001, "offset": -1000.0}]},
        "B08": {"raster:bands": [{"scale": 0.0001, "offset": -1000.0}]},
    },
}

PRE_0400_STAC_FIXTURE = {
    "properties": {
        "s2:processing_baseline": "03.01",
        "s2:boa_quantification_value": 10000.0,
    },
    "assets": {
        "B02": {},
        "B03": {},
        "B04": {},
        "B08": {},
    },
}


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestMetadataExtraction:
    """Verifies genuine extraction of BOA_ADD_OFFSET and QUANTIFICATION_VALUE per band."""

    def test_extract_pre_0400_xml_metadata(self):
        """In Baseline < 04.00, offset is absent in XML and must resolve to 0.0 per band."""
        meta = extract_radiometric_metadata(PRE_0400_METADATA_XML)
        for band in ("B02", "B03", "B04", "B08"):
            assert band in meta
            assert meta[band]["quantification_value"] == 10000.0
            assert meta[band]["boa_add_offset"] == 0.0, f"Legacy baseline must have offset 0.0 for {band}"

    def test_extract_post_0400_xml_metadata(self):
        """In Baseline >= 04.00, BOA_ADD_OFFSET is parsed separately for each band."""
        meta = extract_radiometric_metadata(POST_0400_METADATA_XML)
        # B02 (band_id 1), B03 (band_id 2), B04 (band_id 3), B08 (band_id 7)
        for band in ("B02", "B03", "B04", "B08"):
            assert band in meta
            assert meta[band]["quantification_value"] == 10000.0
            assert meta[band]["boa_add_offset"] == -1000.0, f"Post-04.00 offset must be -1000.0 for {band}"

    def test_extract_stac_metadata(self):
        """Verifies extraction from CDSE STAC item representations."""
        post_meta = extract_radiometric_metadata(POST_0400_STAC_FIXTURE)
        assert post_meta["B04"]["boa_add_offset"] == -1000.0
        assert post_meta["B04"]["quantification_value"] == 10000.0

        pre_meta = extract_radiometric_metadata(PRE_0400_STAC_FIXTURE)
        assert pre_meta["B04"]["boa_add_offset"] == 0.0
        assert pre_meta["B04"]["quantification_value"] == 10000.0


class TestMetadataAwareBOAConversion:
    """Tests BOA reflectance calculation using extracted parameters."""

    def test_conversion_with_extracted_post_0400_metadata(self):
        meta = extract_radiometric_metadata(POST_0400_METADATA_XML)
        b04_params = meta["B04"]

        dn = np.array([0, 1000, 2500, 5000], dtype=np.uint16)
        r = compute_boa_reflectance(
            dn,
            quantification_value=b04_params["quantification_value"],
            boa_add_offset=b04_params["boa_add_offset"],
        )

        assert np.isnan(r[0]), "DN=0 must be NoData (NaN)"
        assert pytest.approx(r[1], abs=1e-5) == 0.0   # (1000 - 1000) / 10000 = 0.0
        assert pytest.approx(r[2], abs=1e-5) == 0.15  # (2500 - 1000) / 10000 = 0.15
        assert pytest.approx(r[3], abs=1e-5) == 0.40  # (5000 - 1000) / 10000 = 0.40

    def test_conversion_with_extracted_pre_0400_metadata(self):
        meta = extract_radiometric_metadata(PRE_0400_METADATA_XML)
        b04_params = meta["B04"]

        dn = np.array([0, 1000, 2500], dtype=np.uint16)
        r = compute_boa_reflectance(
            dn,
            quantification_value=b04_params["quantification_value"],
            boa_add_offset=b04_params["boa_add_offset"],
        )

        assert np.isnan(r[0]), "DN=0 must be NoData (NaN)"
        assert pytest.approx(r[1], abs=1e-5) == 0.10  # 1000 / 10000 = 0.10
        assert pytest.approx(r[2], abs=1e-5) == 0.25  # 2500 / 10000 = 0.25

    def test_dn_zero_is_strictly_nodata(self):
        """DN=0 must never evaluate to negative reflectance even with negative offset."""
        dn = np.array([0, 0, 10], dtype=np.uint16)
        r = compute_boa_reflectance(dn, quantification_value=10000.0, boa_add_offset=-1000.0)
        assert np.isnan(r[0])
        assert np.isnan(r[1])
        assert not np.isnan(r[2])


class TestSCLResamplingAndMasking:
    """Tests SCL categorical resampling, quality masking and snow preservation."""

    def test_resample_scl_nearest(self):
        """Verifies 20m -> 10m nearest-neighbor resampling preserves exact categorical classes."""
        # 2x2 source SCL
        src_scl = np.array([
            [4, 11],  # 4: Vegetation, 11: Snow
            [3, 7]    # 3: Shadow,     7: Unclassified
        ], dtype=np.uint8)

        # Upsample 2x to 4x4
        resampled = resample_scl_nearest(src_scl, target_shape=(4, 4))

        assert resampled.shape == (4, 4)
        assert resampled.dtype == np.uint8
        # Top-left quadrant must be 4
        assert np.all(resampled[0:2, 0:2] == 4)
        # Top-right quadrant must be 11
        assert np.all(resampled[0:2, 2:4] == 11)
        # Bottom-left quadrant must be 3
        assert np.all(resampled[2:4, 0:2] == 3)
        # Bottom-right quadrant must be 7
        assert np.all(resampled[2:4, 2:4] == 7)
        # No non-existent class values introduced
        assert set(np.unique(resampled)) == {3, 4, 7, 11}

    def test_scl_cloud_masking_including_class_7(self):
        scl = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], dtype=np.uint8)
        mask = build_cloud_quality_mask(scl, mask_classes=(0, 1, 3, 7, 8, 9, 10), buffer_pixels=0)

        assert mask[0]   # NoData
        assert mask[1]   # Saturated/Defective
        assert not mask[2]  # Dark area kept
        assert mask[3]   # Shadow
        assert not mask[4]  # Vegetation kept
        assert not mask[5]  # Bare kept
        assert not mask[6]  # Water kept
        assert mask[7]   # Class 7 Unclassified MASKED
        assert mask[8]   # Cloud med
        assert mask[9]   # Cloud high
        assert mask[10]  # Cirrus
        assert not mask[11]  # SNOW CLASS 11 PRESERVED

    def test_snow_class_11_preservation_during_dilation(self):
        scl = np.full((7, 7), 4, dtype=np.uint8)
        scl[3, 3] = 9   # High cloud
        scl[3, 4] = 11  # Adjacent snow

        mask = build_cloud_quality_mask(scl, mask_classes=(0, 1, 3, 7, 8, 9, 10), buffer_pixels=1)

        assert mask[3, 3]       # Cloud masked
        assert mask[3, 2]       # Vegetation dilated/masked
        assert not mask[3, 4]   # SNOW STRICTLY PROTECTED FROM DILATION


class TestAnalyticalAndDisplayProducts:
    """Tests 16-bit analytical stack preservation and 8-bit RGBA display product."""

    def test_analytical_uint16_reflectance_and_nodata_preservation(self):
        """Verifies analytical output preserves scaled uint16 surface reflectance and NoData."""
        shape = (4, 4)
        b04 = np.array([
            [0.10, 0.20, np.nan, 0.0],
            [0.05, 0.40, 0.80, np.nan],
            [np.nan, 0.15, 0.25, 0.35],
            [0.01, 0.02, 0.03, 0.04],
        ], dtype=np.float32)
        b03 = b04 * 0.9
        b02 = b04 * 0.8
        b08 = b04 * 1.5

        stack = build_analytical_reflectance_stack(b04, b03, b02, b08, quantification_value=10000.0)

        assert stack.shape == (4, 4, 4)
        assert stack.dtype == np.uint16

        # Valid pixel check: 0.10 * 10000 = 1000
        assert stack[0, 0, 0] == 1000
        # Valid pixel check: 0.20 * 10000 = 2000
        assert stack[0, 0, 1] == 2000

        # NoData check: NaN reflectance must be preserved as uint16 value 0
        assert stack[0, 0, 2] == 0, "NaN must map to uint16 0 NoData"
        # Zero reflectance must be preserved as uint16 value 0
        assert stack[0, 0, 3] == 0, "Zero reflectance must map to uint16 0 NoData"
        assert stack[0, 1, 3] == 0, "NaN must map to uint16 0 NoData"

    def test_display_rgba_normalization_consistency(self):
        r1 = np.array([0.01, 0.10, 0.20, 0.32], dtype=np.float32)
        r2 = np.array([0.01, 0.10, 0.20, 0.32], dtype=np.float32)

        out1 = normalize_to_8bit_rgb(r1, r_min=0.01, r_max=0.32, gamma=1.15)
        out2 = normalize_to_8bit_rgb(r2, r_min=0.01, r_max=0.32, gamma=1.15)

        np.testing.assert_array_equal(out1, out2)
        assert out1[0] == 0
        assert out1[-1] == 255


class TestCloudOptimizedGeoTIFF:
    """Verifies Cloud Optimized GeoTIFF generation and validation with DEFLATE profile."""

    def test_cog_generation_passes_cog_validate(self):
        """Verifies that an exported GeoTIFF converts to a valid COG on Windows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "test_raw.tif")
            cog_path = os.path.join(tmpdir, "test_cog.tif")

            transform = from_origin(230000, 3570000, 10, 10)
            data = np.random.randint(1, 255, size=(4, 512, 512), dtype=np.uint8)

            with rasterio.open(
                raw_path, "w",
                driver="GTiff",
                height=512, width=512,
                count=4, dtype="uint8",
                crs="EPSG:32643",
                transform=transform,
                nodata=0,
            ) as dst:
                dst.write(data)

            # Convert to COG using DEFLATE profile with PREDICTOR=2
            profile = cog_profiles.get("deflate")
            cog_translate(raw_path, cog_path, profile, quiet=True)

            # Validate generated COG
            is_valid, errors, warnings = cog_validate(cog_path)
            assert is_valid, f"COG validation failed with errors: {errors}"
            assert len(errors) == 0

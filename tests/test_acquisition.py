"""Unit and integration tests for Sentinel-2 STAC acquisition and asset downloading.

All network calls are mocked with responses/unittest.mock.
No real downloads, credentials, or paid services are required.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
import requests

from scripts.search_scenes import (
    SceneCandidate,
    AssetInfo,
    extract_mgrs_tile,
    resolve_scene_assets,
    parse_stac_item,
    compute_scene_score,
    search_sentinel_scenes,
    rank_scenes_by_tile,
    select_best_scenes,
    select_temporally_aligned_scenes,
    save_search_manifest,
)
from scripts.download_sentinel import (
    CDSEAuth,
    CDSEDownloadSession,
    AssetDownloader,
    translate_s3_url_to_https,
    validate_raster_integrity,
    format_tile_directory_name,
    download_tile_assets,
    download_manifest_scenes,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Mock Data
# ---------------------------------------------------------------------------

SAMPLE_STAC_PAGE_1 = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "S2B_MSIL2A_20241015T052719_N0511_R105_T43SGR_20241015T075805",
            "properties": {
                "datetime": "2024-10-15T05:27:19.024Z",
                "eo:cloud_cover": 0.02,
                "s2:mgrs_tile": "43SGR",
                "s2:processing_baseline": "05.11",
            },
            "assets": {
                "B02": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B02.jp2", "type": "image/jp2"},
                "B03": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B03.jp2", "type": "image/jp2"},
                "B04": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B04.jp2", "type": "image/jp2"},
                "B08": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B08.jp2", "type": "image/jp2"},
                "SCL": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/SCL.jp2", "type": "image/jp2"},
            },
        },
        {
            "id": "S2B_MSIL2A_20241015T052719_N0511_R105_T43SFR_20241015T075805",
            "properties": {
                "datetime": "2024-10-15T05:27:19.024Z",
                "eo:cloud_cover": 1.02,
                "s2:mgrs_tile": "43SFR",
                "s2:processing_baseline": "05.11",
            },
            "assets": {
                "B02": {"href": "s3://eodata/Sentinel-2/B02.jp2", "type": "image/jp2"},
                "B03": {"href": "s3://eodata/Sentinel-2/B03.jp2", "type": "image/jp2"},
                "B04": {"href": "s3://eodata/Sentinel-2/B04.jp2", "type": "image/jp2"},
                "B08": {"href": "s3://eodata/Sentinel-2/B08.jp2", "type": "image/jp2"},
                "SCL": {"href": "s3://eodata/Sentinel-2/SCL.jp2", "type": "image/jp2"},
            },
        },
    ],
    "links": [
        {"rel": "next", "href": "https://stac.dataspace.copernicus.eu/v1/search?page=2", "method": "GET"}
    ],
}

SAMPLE_STAC_PAGE_2 = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "S2A_MSIL2A_20241013T053801_N0511_R005_T43SFR_20241013T100347",
            "properties": {
                "datetime": "2024-10-13T05:38:01.024Z",
                "eo:cloud_cover": 1.41,
                "s2:mgrs_tile": "43SFR",
                "s2:processing_baseline": "05.11",
            },
            "assets": {
                "B02": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B02.jp2"},
                "B03": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B03.jp2"},
                "B04": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B04.jp2"},
                "B08": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/B08.jp2"},
                "SCL": {"href": "https://catalogue.dataspace.copernicus.eu/eodata/SCL.jp2"},
            },
        }
    ],
    "links": [],
}


# ---------------------------------------------------------------------------
# Section 1: STAC Discovery & Cross-Tile Temporal Consistency
# ---------------------------------------------------------------------------

class TestSTACDiscovery:
    """Tests STAC search, pagination, multi-tile grouping and cross-tile temporal alignment."""

    def test_stac_pagination(self):
        """Verifies multi-page STAC catalog queries are traversed until next link is exhausted."""
        mock_session = MagicMock()

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = SAMPLE_STAC_PAGE_1

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = SAMPLE_STAC_PAGE_2

        mock_session.post.return_value = resp1
        mock_session.get.return_value = resp2

        candidates = search_sentinel_scenes(
            bbox=[77.05, 32.15, 77.30, 32.38],
            start_date="2024-09-15",
            end_date="2024-10-31",
            session=mock_session,
        )

        assert len(candidates) == 3
        assert candidates[0].mgrs_tile == "43SGR"
        assert candidates[1].mgrs_tile == "43SFR"
        assert candidates[2].mgrs_tile == "43SFR"
        assert mock_session.post.call_count == 1
        assert mock_session.get.call_count == 1

    def test_multiple_mgrs_tiles_grouping_and_ranking(self):
        """Verifies candidate scenes are grouped by MGRS tile."""
        c1 = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][0])  # 43SGR
        c2 = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][1])  # 43SFR
        c3 = parse_stac_item(SAMPLE_STAC_PAGE_2["features"][0])  # 43SFR

        ranked = rank_scenes_by_tile([c1, c2, c3])

        assert "43SGR" in ranked
        assert "43SFR" in ranked
        assert len(ranked["43SGR"]) == 1
        assert len(ranked["43SFR"]) == 2

    def test_cross_tile_date_consistency_co_temporal_match(self):
        """Verifies that when both 43SFR and 43SGR share a common pass (2024-10-15), it is prioritized."""
        c_sgr = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][0])  # 43SGR, 2024-10-15
        c_sfr_same = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][1])  # 43SFR, 2024-10-15
        c_sfr_diff = parse_stac_item(SAMPLE_STAC_PAGE_2["features"][0])  # 43SFR, 2024-10-13

        ranked = rank_scenes_by_tile([c_sgr, c_sfr_same, c_sfr_diff])
        selected, temporal_meta = select_temporally_aligned_scenes(ranked)

        assert temporal_meta["status"] == "co_temporal_match"
        assert temporal_meta["common_date"] == "2024-10-15"
        assert temporal_meta["max_date_difference_days"] == 0.0
        assert selected["43SGR"].scene_id == c_sgr.scene_id
        assert selected["43SFR"].scene_id == c_sfr_same.scene_id

    def test_cross_tile_heterogeneous_dates_reports_discrepancy(self):
        """Verifies that if no common date exists across tiles, date difference is explicitly reported."""
        c_sgr = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][0])  # 43SGR on 2024-10-15
        c_sfr_other = parse_stac_item(SAMPLE_STAC_PAGE_2["features"][0])  # 43SFR on 2024-10-13

        ranked = rank_scenes_by_tile([c_sgr, c_sfr_other])
        selected, temporal_meta = select_temporally_aligned_scenes(ranked)

        assert temporal_meta["status"] == "heterogeneous_dates"
        assert temporal_meta["common_date"] is None
        # Difference between 2024-10-15 and 2024-10-13 is ~2.0 days
        assert pytest.approx(temporal_meta["max_date_difference_days"], abs=0.1) == 2.0
        assert temporal_meta["tile_dates"]["43SGR"] == "2024-10-15"
        assert temporal_meta["tile_dates"]["43SFR"] == "2024-10-13"

    def test_missing_required_assets_detection_and_penalty(self):
        incomplete_item = {
            "id": "S2A_MSIL2A_INCOMPLETE",
            "properties": {"eo:cloud_cover": 0.5, "datetime": "2024-10-01T00:00:00Z"},
            "assets": {
                "B02": {"href": "https://url/B02.jp2"},
                "B03": {"href": "https://url/B03.jp2"},
            },
        }
        candidate = parse_stac_item(incomplete_item)
        assert set(candidate.missing_assets) == {"B04", "B08", "SCL"}
        score = compute_scene_score(candidate)
        assert score > 3000.0


# ---------------------------------------------------------------------------
# Section 2: Security & Integrity in Asset Downloading
# ---------------------------------------------------------------------------

class TestDownloadSecurityAndIntegrity:
    """Tests Bearer token redirect whitelist security, S3 endpoint translation, and raster integrity."""

    def test_bearer_token_preserved_on_trusted_cdse_redirect(self):
        """Tokens MUST be preserved when redirecting within *.dataspace.copernicus.eu."""
        session = CDSEDownloadSession()

        req = requests.Request(
            "GET",
            "https://catalogue.dataspace.copernicus.eu/odata/v1/Products(1)/$value",
            headers={"Authorization": "Bearer my-secret-token"},
        )
        prep = session.prepare_request(req)

        # Mock redirect response from catalogue to eodata
        orig_resp = MagicMock()
        orig_resp.request.url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products(1)/$value"
        orig_resp.request.headers = {"Authorization": "Bearer my-secret-token"}

        # New redirect target on eodata subdomain
        redirect_req = requests.Request(
            "GET",
            "https://eodata.dataspace.copernicus.eu/Sentinel-2/B02.jp2",
            headers={},
        )
        prep_redirect = session.prepare_request(redirect_req)

        session.rebuild_auth(prep_redirect, orig_resp)
        assert prep_redirect.headers.get("Authorization") == "Bearer my-secret-token"

    def test_bearer_token_stripped_on_untrusted_redirect(self):
        """Tokens MUST be stripped if redirected to an untrusted external host."""
        session = CDSEDownloadSession()

        orig_resp = MagicMock()
        orig_resp.request.url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products(1)/$value"
        orig_resp.request.headers = {"Authorization": "Bearer my-secret-token"}

        # Redirect to an attacker or external S3 host
        redirect_req = requests.Request("GET", "https://untrusted-external-host.com/evil.jp2", headers={})
        prep_redirect = session.prepare_request(redirect_req)

        session.rebuild_auth(prep_redirect, orig_resp)
        assert "Authorization" not in prep_redirect.headers

    def test_s3_url_translates_to_verified_cdse_eodata_endpoint(self):
        """s3://eodata/... must translate strictly to https://eodata.dataspace.copernicus.eu/..."""
        s3_url = "s3://eodata/Sentinel-2/MSI/L2A/2024/T43SFR_B02.jp2"
        https_url = translate_s3_url_to_https(s3_url)
        assert https_url == "https://eodata.dataspace.copernicus.eu/Sentinel-2/MSI/L2A/2024/T43SFR_B02.jp2"

    def test_raster_integrity_validation_without_content_length(self):
        """Verifies rasterio integrity check detects valid vs corrupt rasters when Content-Length is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            valid_raster = Path(tmpdir) / "valid.tif"
            corrupt_raster = Path(tmpdir) / "corrupt.tif"

            # Create genuine GeoTIFF
            transform = from_origin(230000, 3570000, 10, 10)
            data = np.random.randint(1, 255, size=(1, 32, 32), dtype=np.uint8)
            with rasterio.open(
                valid_raster, "w",
                driver="GTiff", height=32, width=32, count=1,
                dtype="uint8", crs="EPSG:32643", transform=transform
            ) as dst:
                dst.write(data)

            # Create corrupt file with random non-raster bytes
            with open(corrupt_raster, "wb") as f:
                f.write(b"NOT_A_VALID_TIFF_OR_JP2_HEADER_CORRUPTED_BYTES_HERE")

            assert validate_raster_integrity(valid_raster) is True
            assert validate_raster_integrity(corrupt_raster) is False

    def test_existing_valid_download_skipped_via_raster_integrity(self):
        """An existing local raster with valid structure is skipped even when expected_size is None."""
        mock_auth = MagicMock()
        downloader = AssetDownloader(auth=mock_auth)
        downloader._http.get = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "B04.tif"
            transform = from_origin(230000, 3570000, 10, 10)
            data = np.random.randint(1, 255, size=(1, 16, 16), dtype=np.uint8)
            with rasterio.open(
                dest, "w", driver="GTiff", height=16, width=16, count=1,
                dtype="uint8", crs="EPSG:32643", transform=transform
            ) as dst:
                dst.write(data)

            res = downloader.download_asset("https://mock-url/B04.tif", dest, expected_size=None)
            assert res == dest
            downloader._http.get.assert_not_called()

    def test_interrupted_download_cleans_up_part_file(self):
        mock_auth = MagicMock()
        mock_auth.get_valid_token.return_value = "mock_token"
        downloader = AssetDownloader(auth=mock_auth, max_retries=2, backoff_factor=0.01)

        def broken_stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"Content-Length": "1000"}

            def broken_iter(chunk_size):
                yield b"first_chunk"
                raise ConnectionResetError("Connection dropped mid-stream")

            mock_resp.iter_content = broken_iter
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        downloader._http.get = MagicMock(side_effect=broken_stream)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "B08.jp2"
            with pytest.raises(RuntimeError):
                downloader.download_asset("https://mock-url/B08.jp2", dest)

            assert not dest.exists()
            assert not dest.with_suffix(".jp2.part").exists()


# ---------------------------------------------------------------------------
# Section 3: Multi-Tile Organization & Radiometric Metadata Integration
# ---------------------------------------------------------------------------

class TestMultiTileOrganizationAndMetadataIntegration:
    """Tests tile directory layout and extraction of radiometric metadata."""

    def test_download_tile_assets_organization_and_radiometric_json(self):
        candidate = parse_stac_item(SAMPLE_STAC_PAGE_1["features"][0])

        mock_auth = MagicMock()
        mock_auth.get_valid_token.return_value = "token"
        downloader = AssetDownloader(auth=mock_auth)

        def mock_downloader_asset(url, dest, expected_size=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"MOCK_BYTES")
            return dest

        downloader.download_asset = MagicMock(side_effect=mock_downloader_asset)

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = download_tile_assets(candidate, downloader, output_base_dir=tmpdir)

            tile_dir_name = format_tile_directory_name(candidate)
            assert "20241015_43SGR_" in tile_dir_name

            tile_path = Path(tmpdir) / tile_dir_name
            assert (tile_path / "B02.jp2").is_file()
            assert (tile_path / "B03.jp2").is_file()
            assert (tile_path / "B04.jp2").is_file()
            assert (tile_path / "B08.jp2").is_file()
            assert (tile_path / "SCL.jp2").is_file()
            assert (tile_path / "scene_metadata.json").is_file()
            assert (tile_path / "radiometric_metadata.json").is_file()
            assert (tile_path / "download_manifest.json").is_file()

            with open(tile_path / "radiometric_metadata.json", "r", encoding="utf-8") as f:
                radio_data = json.load(f)

            assert radio_data["mgrs_tile"] == "43SGR"
            params = radio_data["radiometric_parameters"]
            for band in ("B02", "B03", "B04", "B08"):
                assert band in params
                assert params[band]["quantification_value"] == 10000.0
                assert params[band]["boa_add_offset"] == -1000.0


    def test_missing_asset_aborts_tile_completion(self):
        """Verifies download fails cleanly if an essential asset is missing from STAC."""
        bad_item = {
            "id": "S2A_MSIL2A_BAD",
            "properties": {"datetime": "2024-10-01T00:00:00Z", "s2:mgrs_tile": "43SDR"},
            "assets": {"B02": {"href": "https://url/B02.jp2"}},
        }
        candidate = parse_stac_item(bad_item)
        downloader = AssetDownloader(auth=MagicMock())

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(RuntimeError) as exc:
                download_tile_assets(candidate, downloader, output_base_dir=tmpdir)
            assert "Missing required assets" in str(exc.value)

    def test_legacy_baseline_radiometric_integration(self):
        """Verifies legacy baseline 03.xx tile sets boa_add_offset=0.0 in radiometric_metadata.json."""
        legacy_item = {
            "id": "S2A_MSIL2A_20201001T053641_N0209_R005_T43SDR_20201001T084803",
            "properties": {
                "datetime": "2020-10-01T05:36:41.024Z",
                "s2:mgrs_tile": "43SDR",
                "s2:processing_baseline": "02.09",
            },
            "assets": {
                "B02": {"href": "https://url/B02.jp2"},
                "B03": {"href": "https://url/B03.jp2"},
                "B04": {"href": "https://url/B04.jp2"},
                "B08": {"href": "https://url/B08.jp2"},
                "SCL": {"href": "https://url/SCL.jp2"},
            },
        }
        candidate = parse_stac_item(legacy_item)
        downloader = AssetDownloader(auth=MagicMock())
        downloader.download_asset = MagicMock(return_value=Path("mock_path"))

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = download_tile_assets(candidate, downloader, output_base_dir=tmpdir)
            tile_path = Path(tmpdir) / format_tile_directory_name(candidate)

            with open(tile_path / "radiometric_metadata.json", "r", encoding="utf-8") as f:
                radio_data = json.load(f)

            params = radio_data["radiometric_parameters"]
            for band in ("B02", "B03", "B04", "B08"):
                assert params[band]["boa_add_offset"] == 0.0


class TestCDSEAuthentication:
    """Tests Keycloak token acquisition, caching and error handling."""

    def test_auth_success_and_caching(self):
        mock_session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "mock-jwt-token-12345", "expires_in": 300}
        mock_session.post.return_value = resp

        auth = CDSEAuth(username="user@domain.com", password="secretpassword", session=mock_session)
        token1 = auth.get_valid_token()
        assert token1 == "mock-jwt-token-12345"
        token2 = auth.get_valid_token()
        assert token2 == "mock-jwt-token-12345"
        assert mock_session.post.call_count == 1

    def test_auth_failure_raises_permission_error(self):
        mock_session = MagicMock()
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Invalid credentials"
        mock_session.post.return_value = resp

        auth = CDSEAuth(username="baduser", password="badpassword", session=mock_session)
        with pytest.raises(PermissionError) as exc_info:
            auth.get_valid_token()
        assert "401" in str(exc_info.value)

    def test_missing_credentials_raises_value_error(self):
        auth = CDSEAuth(username="", password="")
        with pytest.raises(ValueError):
            auth.get_valid_token()

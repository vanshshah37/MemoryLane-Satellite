"""Sentinel-2 L2A Robust Multi-Tile Asset Downloader.

Downloads target Sentinel-2 Level-2A assets:
- B02 (Blue 10m)
- B03 (Green 10m)
- B04 (Red 10m)
- B08 (NIR 10m)
- SCL (Scene Classification Layer 20m)
- Scene & Tile Metadata (XML & JSON)

Features:
- Official CDSE Keycloak OAuth2 token management with automatic renewal.
- Strict Domain Whitelist for Bearer Token forwarding across redirects:
  Tokens are ONLY forwarded to verified *.dataspace.copernicus.eu hosts.
- Multi-protocol asset access:
  * HTTPS asset URLs.
  * S3 asset URLs (s3://eodata/...) supported via native boto3 S3 client when credentials
    are present, or seamless translation to https://eodata.dataspace.copernicus.eu/ with Bearer token.
- Multi-tile organization: data/raw/<acquisition_date>_<mgrs_tile>_<scene_id>/
- Resilient streaming downloads with chunking, exponential backoff retries, and timeouts.
- Atomic file writes via temporary .part files ensuring no incomplete files are marked valid.
- Content-Length validation and HTML error page detection.
- Deep raster integrity validation: For rasters where Content-Length or checksums are unavailable,
  validates dataset headers, dimensions, and band readability via rasterio.
- Skips already downloaded, verified assets.
- Integration with processing.build_composite.extract_radiometric_metadata() to extract
  and persist per-band radiometric parameters (quantification_value and boa_add_offset).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
import rasterio
from rasterio.windows import Window
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Import existing production metadata extraction logic
from processing.build_composite import extract_radiometric_metadata

load_dotenv()

logger = logging.getLogger(__name__)

# Authentication & STAC Endpoints
DEFAULT_AUTH_URL = os.getenv(
    "CDSE_AUTH_URL",
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
)
DEFAULT_S3_ENDPOINT = os.getenv("CDSE_S3_ENDPOINT_URL", "https://eodata.dataspace.copernicus.eu")
DEFAULT_RAW_DIR = os.getenv("RAW_DATA_DIR", "data/raw")

# Required band/QA assets
TARGET_ASSETS = ("B02", "B03", "B04", "B08", "SCL")

# Strict regex pattern for trusted CDSE domains allowed to receive Bearer tokens
CDSE_DOMAIN_PATTERN = re.compile(r"^([a-z0-9-]+\.)*dataspace\.copernicus\.eu$", re.IGNORECASE)


class CDSEAuth:
    """Manages CDSE Keycloak OAuth2 authentication, token caching, and renewal."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_url: str = DEFAULT_AUTH_URL,
        session: Optional[requests.Session] = None,
    ):
        self.username = username or os.getenv("CDSE_USERNAME")
        self.password = password or os.getenv("CDSE_PASSWORD")
        self.auth_url = auth_url
        self.session = session or requests.Session()
        self._access_token: Optional[str] = None
        self._token_expiry_timestamp: float = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    def get_valid_token(self, force_refresh: bool = False) -> str:
        """Retrieves a valid Bearer token, refreshing if expired or forced."""
        now = time.time()
        # Safety buffer of 60 seconds before expiration
        if (
            not force_refresh
            and self._access_token
            and (self._token_expiry_timestamp - now) > 60
        ):
            return self._access_token

        if not self.has_credentials:
            raise ValueError(
                "CDSE credentials missing. Please set CDSE_USERNAME and CDSE_PASSWORD in .env"
            )

        logger.info(f"Authenticating with CDSE Keycloak at {self.auth_url}...")
        payload = {
            "client_id": "cdse-public",
            "username": self.username,
            "password": self.password,
            "grant_type": "password",
        }

        try:
            resp = self.session.post(
                self.auth_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Network error during CDSE authentication: {e}") from e

        if resp.status_code != 200:
            raise PermissionError(
                f"CDSE Authentication failed ({resp.status_code}): {resp.text}"
            )

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise PermissionError(f"No access_token returned by CDSE: {data}")

        expires_in = float(data.get("expires_in", 300))
        self._access_token = token
        self._token_expiry_timestamp = now + expires_in
        logger.info(f"Successfully obtained CDSE Bearer token (valid for {int(expires_in)}s)")
        return self._access_token

    def invalidate_token(self):
        """Invalidates cached token so the next call forces re-authentication."""
        self._access_token = None
        self._token_expiry_timestamp = 0.0


class CDSEDownloadSession(requests.Session):
    """Custom requests Session with strict domain whitelisting for Authorization header forwarding."""

    def rebuild_auth(self, prepared_request, response):
        """Forward Authorization header ONLY if redirect target strictly matches *.dataspace.copernicus.eu."""
        new_host = (urlparse(prepared_request.url).hostname or "").strip().lower()

        auth_header = response.request.headers.get("Authorization")
        super().rebuild_auth(prepared_request, response)

        if auth_header and CDSE_DOMAIN_PATTERN.match(new_host):
            prepared_request.headers["Authorization"] = auth_header
        else:
            # Strip Authorization header on redirect to any non-whitelisted domain
            if "Authorization" in prepared_request.headers:
                del prepared_request.headers["Authorization"]


def translate_s3_url_to_https(s3_url: str) -> str:
    """Translates an s3://eodata/... URI into the official CDSE HTTPS endpoint.

    Example:
        s3://eodata/Sentinel-2/MSI/L2A/... -> https://eodata.dataspace.copernicus.eu/Sentinel-2/MSI/L2A/...
    """
    if not s3_url.startswith("s3://"):
        return s3_url

    path = s3_url[len("s3://") :]
    if path.startswith("eodata/"):
        path = path[len("eodata/") :]
    return f"{DEFAULT_S3_ENDPOINT.rstrip('/')}/{path.lstrip('/')}"


def validate_raster_integrity(file_path: Path) -> bool:
    """Validates that a downloaded file is a readable, non-corrupt raster dataset.

    Used when Content-Length or checksums are unavailable to ensure partial downloads
    are rejected.
    """
    if not file_path.is_file() or file_path.stat().st_size == 0:
        return False

    suffix = file_path.suffix.lower()
    # Non-raster files (e.g. XML, JSON) skip raster check
    if suffix not in (".jp2", ".tif", ".tiff", ".vrt"):
        return file_path.stat().st_size > 0

    try:
        with rasterio.open(file_path) as src:
            if src.count < 1 or src.width <= 0 or src.height <= 0:
                return False
            # Read small test window to verify stream/decoding integrity
            w = min(16, src.width)
            h = min(16, src.height)
            _ = src.read(1, window=Window(0, 0, w, h))
        return True
    except Exception as e:
        logger.warning(f"Raster integrity check failed for {file_path.name}: {e}")
        return False


class AssetDownloader:
    """Manages resilient downloading of Sentinel-2 assets via HTTPS and S3."""

    def __init__(
        self,
        auth: Optional[CDSEAuth] = None,
        base_dir: Union[str, Path] = DEFAULT_RAW_DIR,
        chunk_size: int = 1024 * 1024,  # 1MB streaming chunks
        max_retries: int = 4,
        backoff_factor: float = 1.5,
        s3_client: Optional[Any] = None,
    ):
        self.auth = auth or CDSEAuth()
        self.base_dir = Path(base_dir)
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._s3_client = s3_client
        self._http = CDSEDownloadSession()

        # Mount retry adapter for transient connection errors
        retries = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)

    def _get_s3_client(self) -> Optional[Any]:
        """Initializes boto3 S3 client if S3 credentials are configured."""
        if self._s3_client is not None:
            return self._s3_client

        access_key = os.getenv("CDSE_S3_ACCESS_KEY")
        secret_key = os.getenv("CDSE_S3_SECRET_KEY")

        if access_key and secret_key:
            try:
                import boto3
                from botocore.config import Config

                self._s3_client = boto3.client(
                    "s3",
                    endpoint_url=DEFAULT_S3_ENDPOINT,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    config=Config(signature_version="s3v4", retries={"max_attempts": self.max_retries}),
                )
                logger.info("Initialized native boto3 S3 client for eodata storage.")
                return self._s3_client
            except Exception as e:
                logger.warning(f"Could not initialize boto3 S3 client: {e}. Falling back to HTTPS translation.")
        return None

    def download_asset(
        self,
        asset_url: str,
        destination_path: Path,
        expected_size: Optional[int] = None,
    ) -> Path:
        """Downloads a single asset using HTTPS or native S3 with retries and atomic write.

        Guarantees:
        - Atomic write via temporary .part file (never leaves corrupt/partial file on error).
        - Skips already existing, verified files.
        - Validates Content-Length (if present) and non-empty size.
        - Deep raster integrity validation using rasterio if Content-Length is unavailable.
        - Checks for HTML error payloads disguised as 200 responses.
        """
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        # Check existing valid download
        if destination_path.is_file() and destination_path.stat().st_size > 0:
            existing_size = destination_path.stat().st_size
            if expected_size is not None and existing_size == expected_size:
                logger.info(f"Skipping already downloaded asset: {destination_path.name} ({existing_size} bytes)")
                return destination_path
            elif expected_size is None and validate_raster_integrity(destination_path):
                logger.info(f"Skipping verified existing raster: {destination_path.name} ({existing_size} bytes)")
                return destination_path

        part_path = destination_path.with_suffix(destination_path.suffix + ".part")

        # Handle native S3 URL if client is available
        if asset_url.startswith("s3://"):
            s3 = self._get_s3_client()
            if s3:
                self._download_via_s3(s3, asset_url, part_path, expected_size)
                part_path.replace(destination_path)
                logger.info(f"S3 download completed: {destination_path.name} ({destination_path.stat().st_size} bytes)")
                return destination_path
            else:
                asset_url = translate_s3_url_to_https(asset_url)
                logger.info(f"Translated S3 URL to HTTPS: {asset_url}")

        # Download via HTTPS with retries
        self._download_via_https(asset_url, part_path, expected_size)
        part_path.replace(destination_path)
        logger.info(f"HTTPS download completed: {destination_path.name} ({destination_path.stat().st_size} bytes)")
        return destination_path

    def _download_via_s3(
        self,
        s3_client: Any,
        s3_url: str,
        part_path: Path,
        expected_size: Optional[int],
    ):
        """Downloads file using boto3 S3 client."""
        parsed = urlparse(s3_url)
        bucket = parsed.netloc or "eodata"
        key = parsed.path.lstrip("/")

        try:
            s3_client.download_file(Bucket=bucket, Key=key, Filename=str(part_path))
            downloaded_size = part_path.stat().st_size
            if downloaded_size == 0:
                raise ValueError("Downloaded S3 file is empty (0 bytes).")
            if expected_size and downloaded_size != expected_size:
                raise ValueError(
                    f"S3 size mismatch: expected {expected_size} bytes, got {downloaded_size} bytes."
                )
            if not validate_raster_integrity(part_path):
                raise ValueError(f"Downloaded S3 raster {part_path.name} is corrupt or unreadable.")
        except Exception as e:
            if part_path.exists():
                part_path.unlink()
            raise RuntimeError(f"Failed to download asset via S3 from {s3_url}: {e}") from e

    def _download_via_https(
        self,
        url: str,
        part_path: Path,
        expected_size: Optional[int],
    ):
        """Streams file via HTTPS with token auth, exponential retries, and atomic validation."""
        attempt = 0
        last_error = None

        parsed_url = urlparse(url)
        is_cdse = bool(CDSE_DOMAIN_PATTERN.match(parsed_url.netloc))

        while attempt < self.max_retries:
            attempt += 1
            try:
                headers = {
                    "User-Agent": "MemoryLane-Satellite/1.0",
                }
                if is_cdse:
                    token = self.auth.get_valid_token()
                    headers["Authorization"] = f"Bearer {token}"

                with self._http.get(url, headers=headers, stream=True, timeout=60) as resp:
                    if resp.status_code == 401:
                        logger.warning("Received 401 Unauthorized from CDSE. Invalidating token and refreshing...")
                        self.auth.invalidate_token()
                        continue

                    if resp.status_code != 200:
                        raise requests.HTTPError(
                            f"HTTP {resp.status_code} downloading {url}: {resp.text[:300]}"
                        )

                    content_type = resp.headers.get("Content-Type", "").lower()
                    if "text/html" in content_type:
                        raise ValueError(f"Server returned HTML page instead of binary raster: {resp.text[:300]}")

                    content_length_header = resp.headers.get("Content-Length")
                    total_bytes = int(content_length_header) if content_length_header else expected_size

                    downloaded_bytes = 0
                    with open(part_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=self.chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded_bytes += len(chunk)

                    if downloaded_bytes == 0:
                        raise ValueError("Downloaded file is empty (0 bytes).")

                    if total_bytes and downloaded_bytes != total_bytes:
                        raise ValueError(
                            f"Incomplete download: expected {total_bytes} bytes, received {downloaded_bytes} bytes."
                        )

                    # Validate raster structure if Content-Length was absent
                    if total_bytes is None and not validate_raster_integrity(part_path):
                        raise ValueError(
                            f"Downloaded raster {part_path.name} failed integrity check."
                        )

                    # Successfully downloaded and verified
                    return

            except Exception as e:
                last_error = e
                logger.warning(f"Download attempt {attempt}/{self.max_retries} failed for {url}: {e}")
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError:
                        pass

                if attempt < self.max_retries:
                    sleep_time = self.backoff_factor ** attempt
                    time.sleep(sleep_time)

        raise RuntimeError(
            f"Failed to download {url} after {self.max_retries} attempts. Last error: {last_error}"
        )


def format_tile_directory_name(scene_candidate: Any) -> str:
    """Formats standard directory name: <YYYYMMDD>_<MGRS>_<SCENE_ID>."""
    dt_str = getattr(scene_candidate, "datetime", "")
    if dt_str:
        try:
            date_prefix = dt_str.split("T")[0].replace("-", "")
        except Exception:
            date_prefix = "UNKNOWN_DATE"
    else:
        date_prefix = "UNKNOWN_DATE"

    mgrs = getattr(scene_candidate, "mgrs_tile", "UNKNOWN_TILE")
    scene_id = getattr(scene_candidate, "scene_id", "UNKNOWN_SCENE")
    return f"{date_prefix}_{mgrs}_{scene_id}"


def download_tile_assets(
    scene_candidate: Any,
    downloader: AssetDownloader,
    output_base_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Downloads all required assets for an MGRS tile scene and extracts radiometric metadata."""
    base_dir = Path(output_base_dir) if output_base_dir else downloader.base_dir
    tile_dir_name = format_tile_directory_name(scene_candidate)
    tile_dir = base_dir / tile_dir_name
    tile_dir.mkdir(parents=True, exist_ok=True)

    assets = getattr(scene_candidate, "assets", {})
    scene_id = getattr(scene_candidate, "scene_id", "")
    mgrs_tile = getattr(scene_candidate, "mgrs_tile", "")

    logger.info(f"Downloading assets for Tile {mgrs_tile} ({scene_id}) into {tile_dir}...")

    downloaded_files: Dict[str, str] = {}
    failed_assets: List[str] = []

    tasks = []
    for asset_key in TARGET_ASSETS:
        asset_info = assets.get(asset_key)
        if not asset_info or not getattr(asset_info, "href", None):
            logger.error(f"Missing required asset '{asset_key}' for scene {scene_id}!")
            failed_assets.append(asset_key)
            continue

        href = asset_info.href
        parsed_path = urlparse(href).path
        ext = Path(parsed_path).suffix or ".jp2"
        target_path = tile_dir / f"{asset_key}{ext}"
        tasks.append((asset_key, href, target_path))

    metadata_asset = assets.get("METADATA_XML")
    xml_path = None
    if metadata_asset and getattr(metadata_asset, "href", None):
        target_xml = tile_dir / "MTD_TL.xml"
        tasks.append(("METADATA_XML", metadata_asset.href, target_xml))

    def _download_task(item):
        k, url, dst = item
        try:
            res_path = downloader.download_asset(url, dst)
            return k, str(res_path), None
        except Exception as e:
            return k, None, e

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_download_task, t): t for t in tasks}
        for future in as_completed(futures):
            key, path_str, err = future.result()
            if err:
                logger.error(f"Failed to download asset {key}: {err}")
                if key in TARGET_ASSETS:
                    failed_assets.append(key)
            else:
                downloaded_files[key] = path_str
                if key == "METADATA_XML":
                    xml_path = Path(path_str)

    scene_meta_path = tile_dir / "scene_metadata.json"
    candidate_dict = scene_candidate.to_dict() if hasattr(scene_candidate, "to_dict") else scene_candidate
    with open(scene_meta_path, "w", encoding="utf-8") as f:
        json.dump(candidate_dict, f, indent=2)
    downloaded_files["scene_metadata"] = str(scene_meta_path)

    radiometric_source = xml_path if xml_path and xml_path.is_file() else candidate_dict
    try:
        radiometric_meta = extract_radiometric_metadata(
            radiometric_source, target_bands=("B02", "B03", "B04", "B08")
        )
    except Exception as e:
        logger.warning(f"Metadata extraction fallback for {scene_id}: {e}")
        radiometric_meta = {
            b: {"quantification_value": 10000.0, "boa_add_offset": -1000.0}
            for b in ("B02", "B03", "B04", "B08")
        }

    radio_meta_path = tile_dir / "radiometric_metadata.json"
    with open(radio_meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scene_id": scene_id,
                "mgrs_tile": mgrs_tile,
                "processing_baseline": getattr(scene_candidate, "processing_baseline", "UNKNOWN"),
                "radiometric_parameters": radiometric_meta,
            },
            f,
            indent=2,
        )
    downloaded_files["radiometric_metadata"] = str(radio_meta_path)

    if failed_assets:
        raise RuntimeError(
            f"Tile download incomplete for {scene_id}! Missing required assets: {failed_assets}"
        )

    manifest_path = tile_dir / "download_manifest.json"
    manifest_data = {
        "scene_id": scene_id,
        "mgrs_tile": mgrs_tile,
        "download_completed_at": datetime.now(timezone.utc).isoformat(),
        "files": downloaded_files,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    return manifest_data


def download_manifest_scenes(
    manifest_file: Union[str, Path],
    output_base_dir: Optional[Union[str, Path]] = None,
    auth: Optional[CDSEAuth] = None,
) -> Dict[str, Any]:
    """Downloads the selected scenes across all MGRS tiles specified in a search manifest."""
    manifest_path = Path(manifest_file)
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    downloader = AssetDownloader(auth=auth, base_dir=output_base_dir or DEFAULT_RAW_DIR)
    results: Dict[str, Any] = {}

    tiles = data.get("tiles", {})
    logger.info(f"Processing download manifest with {len(tiles)} MGRS tile(s)...")

    from scripts.search_scenes import parse_stac_item

    for tile, tile_data in tiles.items():
        selected = tile_data.get("selected_scene")
        if not selected:
            logger.warning(f"No selected scene found for tile {tile} in manifest.")
            continue

        candidate = parse_stac_item(selected)
        logger.info(f"Downloading selected scene for tile {tile}: {candidate.scene_id}...")
        res = download_tile_assets(candidate, downloader, output_base_dir)
        results[tile] = res

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel-2 L2A Multi-Tile Asset Downloader")
    parser.add_argument(
        "--manifest",
        type=str,
        default="data/search_manifest.json",
        help="Path to search manifest JSON file",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=DEFAULT_RAW_DIR,
        help="Base directory for downloaded raw scene tiles",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not Path(args.manifest).is_file():
        raise FileNotFoundError(
            f"Manifest file not found: {args.manifest}. Run scripts/search_scenes.py first."
        )

    logger.info(f"Starting downloads from manifest: {args.manifest}")
    results = download_manifest_scenes(args.manifest, output_base_dir=args.output_dir)
    print("\nDownload Summary:")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

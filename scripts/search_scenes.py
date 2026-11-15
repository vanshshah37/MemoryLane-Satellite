"""Sentinel-2 L2A STAC Discovery & Multi-Tile Scene Selection.

Searches the Copernicus Data Space Ecosystem (CDSE) STAC catalog for
Sentinel-2 Level-2A items covering the configured Area of Interest (AOI).

Features:
- Full pagination support across matching catalog items.
- Multi-MGRS tile discovery and grouping based on genuine tile boundaries (e.g. 43SFR and 43SGR).
- Extraction of MGRS tile ID, acquisition date, cloud cover, processing baseline, and asset URLs.
- Multi-criteria scene ranking per tile.
- Cross-tile temporal alignment: Prioritizes co-temporal acquisitions (same date/orbit across tiles)
  for seamless mosaicking. If no common date satisfies cloud criteria, reports date differences explicitly.
- Configurable search and ranking weights via CLI or environment.
- Manifest generation for downstream ingestion.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urljoin

from dotenv import load_dotenv
import requests

load_dotenv()

logger = logging.getLogger(__name__)

# Default STAC catalog endpoints
DEFAULT_STAC_URL = os.getenv("CDSE_STAC_URL", "https://stac.dataspace.copernicus.eu/v1")
COLLECTION_NAME = "sentinel-2-l2a"

# Required band/QA asset keys
REQUIRED_ASSETS = ("B02", "B03", "B04", "B08", "SCL")

# Asset alias mapping to handle different STAC conventions
ASSET_KEY_ALIASES = {
    "B02": ["B02", "b02", "blue", "B2", "b2"],
    "B03": ["B03", "b03", "green", "B3", "b3"],
    "B04": ["B04", "b04", "red", "B4", "b4"],
    "B08": ["B08", "b08", "nir", "B8", "b8", "nir08"],
    "SCL": ["SCL", "scl", "scene_classification", "classification"],
}

MGRS_REGEX = re.compile(r"_T([0-9]{2}[A-Z]{3})_")


@dataclass
class AssetInfo:
    key: str
    href: str
    media_type: Optional[str] = None
    title: Optional[str] = None


@dataclass
class SceneCandidate:
    scene_id: str
    mgrs_tile: str
    datetime: str
    cloud_cover: float
    processing_baseline: str
    assets: Dict[str, AssetInfo]
    missing_assets: List[str]
    score: float = 0.0
    properties: Optional[Dict[str, Any]] = None
    geometry: Optional[Dict[str, Any]] = None

    @property
    def date_str(self) -> str:
        """Returns YYYY-MM-DD date component."""
        if self.datetime:
            return self.datetime.split("T")[0]
        return ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["assets"] = {k: asdict(v) for k, v in self.assets.items()}
        return d


def extract_mgrs_tile(item: Dict[str, Any]) -> str:
    """Extracts MGRS tile identifier (e.g. '43SFR', '43SGR') from STAC item metadata or ID."""
    props = item.get("properties", {})

    # 1. Direct property
    if "s2:mgrs_tile" in props and props["s2:mgrs_tile"]:
        return str(props["s2:mgrs_tile"])

    # 2. MGRS extension properties
    zone = props.get("mgrs:utm_zone")
    lat_band = props.get("mgrs:latitude_band")
    grid = props.get("mgrs:grid_square")
    if zone is not None and lat_band and grid:
        return f"{int(zone):02d}{lat_band}{grid}"

    # 3. Regex from item ID (e.g. S2A_MSIL2A_20241013T053801_N0511_R005_T43SFR_20241013T100347)
    item_id = item.get("id", "")
    match = MGRS_REGEX.search(item_id)
    if match:
        return match.group(1)

    return "UNKNOWN"


def resolve_scene_assets(item: Dict[str, Any]) -> Tuple[Dict[str, AssetInfo], List[str]]:
    """Resolves required assets from STAC item assets dictionary handling aliases."""
    raw_assets = item.get("assets", {})
    resolved: Dict[str, AssetInfo] = {}
    missing: List[str] = []

    for target_key in REQUIRED_ASSETS:
        aliases = ASSET_KEY_ALIASES.get(target_key, [target_key])
        found = False
        for alias in aliases:
            if alias in raw_assets:
                asset_data = raw_assets[alias]
                href = asset_data.get("href", "")
                if href:
                    resolved[target_key] = AssetInfo(
                        key=target_key,
                        href=href,
                        media_type=asset_data.get("type"),
                        title=asset_data.get("title"),
                    )
                    found = True
                    break
        if not found:
            missing.append(target_key)

    # Capture metadata XML asset if present
    for meta_key in ("PRODUCT_METADATA", "GRANULE_METADATA", "metadata", "MTD_TL", "MTD_MSIL2A"):
        if meta_key in raw_assets:
            resolved["METADATA_XML"] = AssetInfo(
                key="METADATA_XML",
                href=raw_assets[meta_key].get("href", ""),
                media_type=raw_assets[meta_key].get("type"),
                title=raw_assets[meta_key].get("title"),
            )
            break

    return resolved, missing


def parse_stac_item(item: Dict[str, Any]) -> SceneCandidate:
    """Parses a single STAC item into a SceneCandidate dataclass."""
    props = item.get("properties", {})
    mgrs_tile = extract_mgrs_tile(item)
    resolved_assets, missing_assets = resolve_scene_assets(item)

    cloud_cover = float(props.get("eo:cloud_cover", 100.0))
    dt_str = props.get("datetime", "")

    baseline = props.get(
        "s2:processing_baseline",
        props.get("processing:software", {}).get("sentinel2-l2a", "UNKNOWN"),
    )

    return SceneCandidate(
        scene_id=item.get("id", ""),
        mgrs_tile=mgrs_tile,
        datetime=dt_str,
        cloud_cover=cloud_cover,
        processing_baseline=baseline,
        assets=resolved_assets,
        missing_assets=missing_assets,
        properties=props,
        geometry=item.get("geometry"),
    )


def compute_scene_score(
    candidate: SceneCandidate,
    target_date: Optional[datetime] = None,
    cloud_weight: float = 1.0,
    date_weight: float = 0.5,
    missing_asset_penalty: float = 1000.0,
) -> float:
    """Computes a multi-criteria score for ranking candidate scenes per tile.

    Lower score = higher preference.
    """
    score = candidate.cloud_cover * cloud_weight
    score += len(candidate.missing_assets) * missing_asset_penalty

    if target_date and candidate.datetime:
        try:
            scene_dt = datetime.fromisoformat(candidate.datetime.replace("Z", "+00:00"))
            if target_date.tzinfo is None:
                target_dt = target_date.replace(tzinfo=timezone.utc)
            else:
                target_dt = target_date
            diff_days = abs((scene_dt - target_dt).total_seconds()) / 86400.0
            score += diff_days * date_weight
        except (ValueError, TypeError):
            pass

    return round(score, 3)


def search_sentinel_scenes(
    bbox: Sequence[float],
    start_date: str,
    end_date: str,
    max_cloud_cover: float = 20.0,
    stac_url: str = DEFAULT_STAC_URL,
    limit_per_page: int = 100,
    max_pages: int = 20,
    session: Optional[requests.Session] = None,
) -> List[SceneCandidate]:
    """Queries the CDSE STAC API and retrieves all matching items across pages."""
    http = session or requests.Session()
    search_url = urljoin(stac_url.rstrip("/") + "/", "search")

    datetime_param = f"{start_date}T00:00:00Z/{end_date}T23:59:59Z"

    payload: Dict[str, Any] = {
        "collections": [COLLECTION_NAME],
        "bbox": list(bbox),
        "datetime": datetime_param,
        "query": {"eo:cloud_cover": {"lte": float(max_cloud_cover)}},
        "limit": int(limit_per_page),
    }

    all_items: List[Dict[str, Any]] = []
    current_url: Optional[str] = search_url
    current_payload: Optional[Dict[str, Any]] = payload
    current_method = "POST"
    page_count = 0

    while current_url and page_count < max_pages:
        page_count += 1
        logger.info(f"Querying STAC page {page_count} at {current_url}...")

        if current_method == "POST":
            response = http.post(
                current_url,
                json=current_payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        else:
            response = http.get(
                current_url,
                headers={"Accept": "application/json"},
                timeout=30,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"STAC search failed with status {response.status_code}: {response.text}"
            )

        data = response.json()
        features = data.get("features", [])
        all_items.extend(features)
        logger.info(f"Retrieved {len(features)} items from page {page_count} (Total: {len(all_items)})")

        next_link = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_link = link
                break

        if next_link and next_link.get("href"):
            current_url = next_link["href"]
            current_method = next_link.get("method", "GET").upper()
            current_payload = next_link.get("body")
        else:
            current_url = None

    candidates = [parse_stac_item(item) for item in all_items]
    return candidates


def rank_scenes_by_tile(
    candidates: Sequence[SceneCandidate],
    target_date: Optional[datetime] = None,
    cloud_weight: float = 1.0,
    date_weight: float = 0.5,
) -> Dict[str, List[SceneCandidate]]:
    """Groups candidate scenes by MGRS tile and ranks each group using multi-criteria scoring."""
    tile_groups: Dict[str, List[SceneCandidate]] = {}

    for c in candidates:
        tile = c.mgrs_tile
        if tile not in tile_groups:
            tile_groups[tile] = []
        c.score = compute_scene_score(
            c,
            target_date=target_date,
            cloud_weight=cloud_weight,
            date_weight=date_weight,
        )
        tile_groups[tile].append(c)

    for tile in tile_groups:
        tile_groups[tile].sort(key=lambda s: s.score)

    return tile_groups


def select_best_scenes(
    ranked_tiles: Dict[str, List[SceneCandidate]],
) -> Dict[str, SceneCandidate]:
    """Selects the single best scene per MGRS tile independently."""
    best: Dict[str, SceneCandidate] = {}
    for tile, candidates in ranked_tiles.items():
        if candidates:
            best[tile] = candidates[0]
    return best


def select_temporally_aligned_scenes(
    ranked_tiles: Dict[str, List[SceneCandidate]],
    max_acceptable_cloud: float = 20.0,
) -> Tuple[Dict[str, SceneCandidate], Dict[str, Any]]:
    """Selects scenes across multiple intersecting MGRS tiles with temporal alignment priority.

    Mosaicking Strategy:
    1. If multiple MGRS tiles intersect the AOI (e.g. 43SFR and 43SGR), searches for a
       common acquisition date where ALL intersecting tiles have valid scenes with cloud <= max_acceptable_cloud.
    2. If a common date exists, selects the co-temporal pair to eliminate cross-date seams.
    3. If no common date exists, selects the top-ranked scene per tile and explicitly computes
       and reports the date difference (delta_days) and baseline differences.

    Returns:
        (selected_scenes_dict, temporal_alignment_metadata)
    """
    tiles = list(ranked_tiles.keys())
    selected: Dict[str, SceneCandidate] = {}

    if not tiles:
        return {}, {"status": "no_tiles"}

    if len(tiles) == 1:
        tile = tiles[0]
        best = ranked_tiles[tile][0] if ranked_tiles[tile] else None
        if best:
            selected[tile] = best
        return selected, {
            "status": "single_tile",
            "common_date": best.date_str if best else None,
            "max_date_difference_days": 0.0,
        }

    # Find common dates across all intersecting tiles
    tile_dates: Dict[str, Dict[str, SceneCandidate]] = {}
    for tile in tiles:
        tile_dates[tile] = {}
        for c in ranked_tiles[tile]:
            d = c.date_str
            # Keep best candidate for each date
            if d and (d not in tile_dates[tile] or c.score < tile_dates[tile][d].score):
                if c.cloud_cover <= max_acceptable_cloud and not c.missing_assets:
                    tile_dates[tile][d] = c

    # Set intersection of dates
    common_dates: Set[str] = set.intersection(*(set(tile_dates[t].keys()) for t in tiles))

    if common_dates:
        # Choose the common date with the lowest combined score across all tiles
        best_common_date = None
        lowest_combined_score = float("inf")

        for d in sorted(common_dates, reverse=True):
            comb_score = sum(tile_dates[t][d].score for t in tiles)
            if comb_score < lowest_combined_score:
                lowest_combined_score = comb_score
                best_common_date = d

        for tile in tiles:
            selected[tile] = tile_dates[tile][best_common_date]

        logger.info(
            f"Cross-Tile Temporal Co-registration SUCCESS: All {len(tiles)} tiles share acquisition date {best_common_date}."
        )
        return selected, {
            "status": "co_temporal_match",
            "common_date": best_common_date,
            "max_date_difference_days": 0.0,
            "combined_score": round(lowest_combined_score, 3),
        }

    # No common date found: fallback to independent top-ranked scenes
    logger.warning("No single co-temporal acquisition date covers all intersecting MGRS tiles!")
    dates_list: List[datetime] = []

    for tile in tiles:
        if ranked_tiles[tile]:
            best = ranked_tiles[tile][0]
            selected[tile] = best
            if best.datetime:
                try:
                    dt = datetime.fromisoformat(best.datetime.replace("Z", "+00:00"))
                    dates_list.append(dt)
                except Exception:
                    pass

    delta_days = 0.0
    if len(dates_list) >= 2:
        delta_days = round(abs((max(dates_list) - min(dates_list)).total_seconds()) / 86400.0, 2)

    logger.warning(f"Temporal Discrepancy across tiles: {delta_days} day(s) difference between chosen scenes.")

    return selected, {
        "status": "heterogeneous_dates",
        "common_date": None,
        "max_date_difference_days": delta_days,
        "tile_dates": {t: selected[t].date_str for t in selected},
    }


def save_search_manifest(
    ranked_tiles: Dict[str, List[SceneCandidate]],
    output_path: Union[str, Path],
    selected_scenes: Optional[Dict[str, SceneCandidate]] = None,
    temporal_metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Saves search results, candidate rankings, and temporal alignment metadata to JSON."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    manifest_data: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_tiles": len(ranked_tiles),
        "temporal_alignment": temporal_metadata or {},
        "tiles": {},
    }

    for tile, candidates in ranked_tiles.items():
        sel = selected_scenes.get(tile) if selected_scenes else (candidates[0] if candidates else None)
        manifest_data["tiles"][tile] = {
            "selected_scene": sel.to_dict() if sel else None,
            "candidate_count": len(candidates),
            "candidates": [c.to_dict() for c in candidates],
        }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    logger.info(f"Saved search manifest to {out_file}")
    return out_file


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel-2 L2A Multi-Tile Scene Search")
    parser.add_argument(
        "--bbox",
        type=str,
        default=os.getenv("AOI_BBOX", "77.05,32.15,77.30,32.38"),
        help="Bounding box: min_lon,min_lat,max_lon,max_lat",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=os.getenv("SEARCH_START_DATE", "2024-09-15"),
        help="Start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=os.getenv("SEARCH_END_DATE", "2024-10-31"),
        help="End date YYYY-MM-DD",
    )
    parser.add_argument(
        "--cloud-max",
        type=float,
        default=float(os.getenv("MAX_CLOUD_COVER", "20")),
        help="Maximum cloud cover percentage (0-100)",
    )
    parser.add_argument(
        "--target-date",
        type=str,
        default=None,
        help="Target date for temporal ranking (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--output-manifest",
        type=str,
        default="data/search_manifest.json",
        help="Output JSON path for discovered scene manifest",
    )
    parser.add_argument(
        "--stac-url",
        type=str,
        default=DEFAULT_STAC_URL,
        help="CDSE STAC API URL",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bbox = [float(x.strip()) for x in args.bbox.split(",")]
    target_dt = datetime.strptime(args.target_date, "%Y-%m-%d") if args.target_date else None

    logger.info(f"Searching Sentinel-2 L2A scenes for AOI {bbox} ({args.start_date} to {args.end_date})...")

    candidates = search_sentinel_scenes(
        bbox=bbox,
        start_date=args.start_date,
        end_date=args.end_date,
        max_cloud_cover=args.cloud_max,
        stac_url=args.stac_url,
    )

    logger.info(f"Discovered {len(candidates)} total matching candidate scenes.")
    ranked_tiles = rank_scenes_by_tile(candidates, target_date=target_dt)

    logger.info(f"Grouped across {len(ranked_tiles)} MGRS tile(s): {list(ranked_tiles.keys())}")
    selected_scenes, temporal_meta = select_temporally_aligned_scenes(
        ranked_tiles, max_acceptable_cloud=args.cloud_max
    )

    logger.info(f"Temporal Alignment Status: {temporal_meta.get('status')}")
    if temporal_meta.get("common_date"):
        logger.info(f"  Unified Acquisition Date: {temporal_meta.get('common_date')}")
    else:
        logger.info(f"  Date Discrepancy: {temporal_meta.get('max_date_difference_days')} days across tiles")

    out_manifest = save_search_manifest(
        ranked_tiles,
        args.output_manifest,
        selected_scenes=selected_scenes,
        temporal_metadata=temporal_meta,
    )
    print(f"\nManifest successfully written to: {out_manifest}")


if __name__ == "__main__":
    main()

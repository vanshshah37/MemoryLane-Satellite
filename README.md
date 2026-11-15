# MemoryLane-Satellite

A completely self-hosted, zero-cost satellite imagery pipeline built with open geospatial standards, open data from Copernicus Sentinel-2 L2A, and open-source tooling.

Designed as an independent imagery service for **Memory-Lane**, requiring **no paid APIs**, **no cloud vendor lock-in**, and **no AI/ML blackboxes**.

---

## Architecture Overview

```
Copernicus Data Space (Sentinel-2 L2A)
  │
  ▼  (STAC API Discovery / Paginated Multi-Tile Traversal & Multi-Criteria Ranking)
scripts/search_scenes.py ──▶ data/search_manifest.json
  │
  ▼  (HTTPS Bearer Auth or S3 Protocol / Resilient Streaming & Atomic Writes)
scripts/download_sentinel.py ──▶ data/raw/<date>_<tile>_<scene_id>/
  │                               ├── B02, B03, B04, B08, SCL (.jp2 / .tif)
  │                               ├── scene_metadata.json
  │                               └── radiometric_metadata.json (extracted via extract_radiometric_metadata)
  │
  ▼  (AOI Windowing ~23x26 km, SCL Resampling, QA Masking: Class 1, 3, 7, 8, 9, 10 + Edge Buffering)
processing/build_composite.py
  ├─ Metadata-aware Calibration: R = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE (DN=0 as NoData)
  ├─ Output 1: 4-Band 16-bit Surface Reflectance (B04, B03, B02, B08 at 10m)
  ├─ Output 2: 1-Band 8-bit Categorical SCL QA Raster (10m)
  └─ Output 3: 4-Band 8-bit Display RGBA (Fixed Multi-temporal Normalization + Alpha Mask)
  │
  ▼  (COG Tiling 512x512, Overviews, DEFLATE + Predictor 2 compression)
processing/create_cog.py
  │
  ▼  (Local Cloud Optimized GeoTIFF)
cog/manali_solang_rgb.tif
  │
  ▼  (FastAPI / TiTiler ASGI Tile Server)
server/main.py
  │
  ▼  (Dynamic XYZ Tile Endpoint / TileJSON)
MapLibre GL JS (Frontend Map)
```

---

## Test Area of Interest: Manali – Solang Valley

* **Region:** Himachal Pradesh, Western Himalayas, India
* **Bounding Box (BBOX):** `[77.05, 32.15, 77.30, 32.38]` (WGS84 EPSG:4326)
* **Actual Dimensions:** Approximately **23.5 km × 25.5 km** (~600 km² coverage)
* **Projection:** UTM Zone 43N (`EPSG:32643`)
* **MGRS Tile Coverage:** Spans two adjacent tiles: **`43SFR`** (~70.98% coverage) and **`43SGR`** (~70.80% coverage). Both tiles are required for 100% coverage. Both are captured simultaneously in relative orbit **`R105`**.

---

## Directory Structure

```
MemoryLane-Satellite/
├── .env.example              # Environment template (CDSE credentials, S3, AOI, thresholds)
├── .gitignore                # Git ignore rules for rasters, credentials & virtualenvs
├── pytest.ini                # Pytest configuration (pythonpath = .)
├── requirements.txt          # Python dependencies with Windows pre-compiled binary wheels
├── README.md                 # Project guide & quickstart runbook
├── cog/                      # Output Cloud Optimized GeoTIFF storage
├── data/
│   ├── raw/                  # Downloaded Sentinel-2 tiles (<date>_<tile>_<scene_id>/)
│   ├── processed/            # 16-bit reflectance, SCL QA, and 8-bit masked RGBA composites
│   └── search_manifest.json  # Discovered scenes and per-tile candidate rankings
├── docs/
│   ├── architecture.md       # Detailed technical design & mathematical formulas
│   └── data-sources.md       # CDSE STAC endpoints, band specifications & SCL reference
├── processing/
│   ├── __init__.py
│   ├── build_composite.py    # Masking, buffering, reflectance preservation & metadata extraction
│   └── create_cog.py         # Cloud Optimized GeoTIFF conversion (DEFLATE standard)
├── scripts/
│   ├── __init__.py
│   ├── search_scenes.py      # STAC catalog query, pagination, multi-tile grouping & ranking
│   └── download_sentinel.py  # Resilient multi-protocol asset downloader (HTTPS / S3)
├── server/
│   └── main.py               # Standalone TiTiler ASGI dynamic tile server
└── tests/
    ├── test_acquisition.py   # Unit tests for STAC search, pagination, S3/HTTPS & downloads
    ├── test_composite.py     # Unit tests for multi-tile mosaicking, hole filling, soft-knee RGB & COGs
    └── test_pipeline.py      # Unit tests for metadata extraction, BOA math, QA & COGs
```

---

## Quickstart & Environment Setup (Windows Native)

### 1. Prerequisites
* **Python 3.11+ 64-bit** (Python 3.11.9 detected on system)
* A free account on the [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/)
* *Neither Docker nor standalone C++ GDAL installation is required.* Pre-compiled wheels on PyPI bundle GDAL and PROJ runtimes.

### 2. Create and Activate Virtual Environment
```powershell
cd D:\Projects\MemoryLane-Satellite
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 4. Configure Environment
Copy `.env.example` to `.env` and fill in your Copernicus credentials:
```powershell
cp .env.example .env
```
Edit `.env`:
```ini
CDSE_USERNAME=your_copernicus_email@domain.com
CDSE_PASSWORD=your_copernicus_password

# Optional: If using authenticated S3 range reads
CDSE_S3_ACCESS_KEY=your_access_key
CDSE_S3_SECRET_KEY=your_secret_key
```

---

## Sentinel-2 Acquisition Workflow

The acquisition pipeline runs in two decoupled steps:

### Step 1: Search & Multi-Tile Candidate Ranking
```powershell
python scripts/search_scenes.py `
  --bbox 77.05,32.15,77.30,32.38 `
  --start-date 2024-09-15 `
  --end-date 2024-10-31 `
  --cloud-max 20 `
  --output-manifest data/search_manifest.json
```
* Queries CDSE STAC with full pagination.
* Discovers all intersecting MGRS tiles (e.g., `43SDR`).
* Ranks scenes per tile using cloud cover, target date proximity, and band completeness.
* Generates `data/search_manifest.json`.

### Step 2: Resilient Asset Acquisition
```powershell
python scripts/download_sentinel.py `
  --manifest data/search_manifest.json `
  --output-dir data/raw
```
* Authenticates with CDSE Keycloak and manages Bearer token renewal.
* Downloads `B02`, `B03`, `B04`, `B08`, `SCL`, and metadata XML via HTTPS or native S3.
* Performs atomic writes (`.part` temporary files) and verifies `Content-Length`.
* Skips existing verified files.
* Automatically runs `extract_radiometric_metadata()` and creates `radiometric_metadata.json` inside each tile's directory.

### Step 3: Multi-Tile Mosaicking & Natural RGB Generation
```powershell
python processing/build_composite.py `
  --tiles data/raw/20241015_43SFR_S2B data/raw/20241015_43SGR_S2B `
  --bbox 77.05,32.15,77.30,32.38 `
  --output-dir data/processed
```
* Snaps AOI to exact 10.0m UTM Zone 43N grid (2305 × 2599 pixels).
* Calibrates surface reflectance using extracted per-band radiometric offsets.
* Merges `43SFR` and `43SGR` with overlap blending and cloud hole-filling.
* Synthesizes natural color RGB with soft-knee highlight compression (preserving snow facets).
* Produces `data/processed/manali_solang_reflectance.tif`, `manali_solang_scl.tif`, and `manali_solang_rgb_display.tif`.

### Step 4: Cloud Optimized GeoTIFF (COG) Creation
```powershell
python processing/create_cog.py `
  --processed-dir data/processed `
  --cog-dir cog
```
* Converts processed rasters to COG using internal $512 \times 512$ tiling and overviews.
* Compresses with DEFLATE (`PREDICTOR=2`) for 100% native Windows compatibility.
* Validates generated COGs with `rio_cogeo.cogeo.cog_validate()`.
* Generates `cog/manali_solang_rgb.tif`, `cog/manali_solang_reflectance.tif`, and `cog/manali_solang_scl.tif`.

---

## Running Automated Tests

Run the full test suite (36 unit and integration tests) using pytest:
```powershell
.\.venv\Scripts\pytest -v tests/
```

All network calls in tests are mocked; no real satellite imagery downloads or CDSE credentials are required for verification.

---

## License & Data Attribution
* Satellite Data: Contains modified Copernicus Sentinel data [2024] processed by MemoryLane-Satellite.
* Codebase: MIT License.

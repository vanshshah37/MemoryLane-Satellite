# MemoryLane-Satellite

A completely self-hosted, zero-cost satellite imagery pipeline built with open geospatial standards, open data from Copernicus Sentinel-2 L2A, and open-source tooling.

Designed as an independent imagery service for **Memory-Lane**, requiring **no paid APIs**, **no cloud vendor lock-in**, and **no AI/ML blackboxes**.

---

## Architecture Overview

```
Copernicus Data Space (Sentinel-2 L2A)
  │
  ▼  (STAC API Discovery / HTTPS or S3 Access for B04, B03, B02, B08 + SCL)
scripts/search_scenes.py & scripts/download_sentinel.py
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
* **Actual Dimensions:** Approximately **23.5 km × 25.5 km** (~23 × 26 km, ~600 km² coverage)
* **Projection:** UTM Zone 43N (`EPSG:32643`)
* **Himalayan Terrain Strategy:**
  * **Metadata-Aware Radiometric Calibration:** Accounts for Processing Baseline offset differences ($\text{BOA\_ADD\_OFFSET} = -1000$ for Baseline $\ge$ 04.00, $0$ for older), strictly preserving $DN=0$ as NoData.
  * **Deterministic Snow vs. Cloud Separation:** Sentinel-2 Scene Classification Layer (SCL) class 11 (Snow/Ice) is strictly preserved, while classes 1 (defective/saturated), 3 (shadow), 7 (unclassified/marginal), and 8, 9, 10 (clouds) are masked out.
  * **Cloud-Edge Buffering:** Configurable morphological dilation (default: 30 meters) buffers cloud boundaries without eroding snow fields.
  * **Separate Analytical & QA Products:** 4-band 10m reflectance (B04, B03, B02, B08) is exported separately from the 10m SCL QA raster and the 8-bit display RGB.

---

## Directory Structure

```
MemoryLane-Satellite/
├── .env.example              # Environment template (CDSE credentials, S3, AOI, thresholds)
├── .gitignore                # Git ignore rules for rasters, credentials & virtualenvs
├── requirements.txt          # Python dependencies with Windows pre-compiled binary wheels
├── README.md                 # Project guide & quickstart runbook
├── cog/                      # Output Cloud Optimized GeoTIFF storage
├── data/
│   ├── raw/                  # Downloaded Sentinel-2 single-band rasters
│   └── processed/            # 16-bit reflectance, SCL QA, and 8-bit masked RGBA composites
├── docs/
│   ├── architecture.md       # Detailed technical design & mathematical formulas
│   └── data-sources.md       # CDSE STAC endpoints, band specifications & SCL reference
├── processing/
│   ├── build_composite.py    # Masking, buffering, reflectance preservation & RGB normalization
│   └── create_cog.py         # Cloud Optimized GeoTIFF conversion (DEFLATE standard)
├── scripts/
│   ├── download_sentinel.py  # Selective band downloader via CDSE HTTPS or S3
│   └── search_scenes.py      # STAC catalog search for candidate scenes
├── server/
│   └── main.py               # Standalone TiTiler ASGI dynamic tile server
└── tests/
    └── test_pipeline.py      # Unit and smoke tests for pipeline modules
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

## Step-by-Step Execution Workflow

Once the setup is reviewed and confirmed, pipeline execution proceeds as follows:

1. **Search Scenes:**
   ```powershell
   python scripts/search_scenes.py --cloud-max 20 --start-date 2024-09-15 --end-date 2024-10-31
   ```
2. **Download Target Bands Intersecting AOI:**
   ```powershell
   python scripts/download_sentinel.py --scene-id <SELECTED_SCENE_ID>
   ```
3. **Build Products & Convert to COG:**
   ```powershell
   # Generates 16-bit 4-band reflectance, SCL QA, and 8-bit masked RGB
   python processing/build_composite.py
   # Generates DEFLATE-compressed Cloud Optimized GeoTIFF
   python processing/create_cog.py
   ```
4. **Launch Local TiTiler Tile Server:**
   ```powershell
   uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
   ```
   * Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`
   * Dynamic XYZ Tile endpoint: `http://127.0.0.1:8000/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=D:/Projects/MemoryLane-Satellite/cog/manali_solang_rgb.tif`

---

## License & Data Attribution
* Satellite Data: Contains modified Copernicus Sentinel data [2024] processed by MemoryLane-Satellite.
* Codebase: MIT License.

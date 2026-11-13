# MemoryLane-Satellite

A completely self-hosted, zero-cost satellite imagery pipeline built with open geospatial standards, open data from Copernicus Sentinel-2 L2A, and open-source tooling.

Designed as an independent imagery service for **Memory-Lane**, requiring **no paid APIs**, **no cloud vendor lock-in**, and **no AI/ML blackboxes**.

---

## Architecture Overview

```
Copernicus Data Space (Sentinel-2 L2A)
  │
  ▼  (STAC API / Selective Band Download: B04, B03, B02, SCL)
scripts/search_scenes.py & scripts/download_sentinel.py
  │
  ▼  (AOI Windowing, 20m->10m SCL Resampling, Cloud & Shadow Masking, 8-bit Percentile Stretch)
processing/build_composite.py
  │
  ▼  (Tiling 512x512, Internal Overviews, DEFLATE/WEBP compression)
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

## Initial Test Area: Manali – Solang Valley

* **Region:** Himachal Pradesh, Western Himalayas, India
* **Bounding Box (BBOX):** `[77.05, 32.15, 77.30, 32.38]` (~18 km x 25 km)
* **Projection:** UTM Zone 43N (`EPSG:32643`)
* **Key Challenge:** Discerning high-altitude snow/ice from clouds, handled deterministically via Sentinel-2 Scene Classification Layer (SCL class 11).

---

## Directory Structure

```
MemoryLane-Satellite/
├── .env.example              # Sample environment configuration
├── .gitignore                # Git ignore rules for rasters, tokens & venv
├── requirements.txt          # Python dependencies (Windows-compatible wheels)
├── README.md                 # Project guide & quickstart
├── cog/                      # Output Cloud Optimized GeoTIFF storage
├── data/
│   ├── raw/                  # Downloaded Sentinel-2 single-band rasters
│   └── processed/            # Masked & contrast-stretched GeoTIFF composites
├── docs/
│   ├── architecture.md       # Full architecture specification
│   └── data-sources.md       # CDSE STAC, bands & SCL class documentation
├── processing/
│   ├── build_composite.py    # Cloud masking & 8-bit RGB composite builder
│   └── create_cog.py         # Cloud Optimized GeoTIFF conversion via rio-cogeo
├── scripts/
│   ├── download_sentinel.py  # Selective band downloader via CDSE OData/HTTPS
│   └── search_scenes.py      # STAC catalog query for low-cloud scenes
├── server/
│   └── main.py               # TiTiler dynamic tile server application
└── tests/
    └── test_pipeline.py      # End-to-end integration and smoke tests
```

---

## Quickstart & Environment Setup (Windows Native)

### 1. Prerequisites
* **Python 3.11+ 64-bit** (Python 3.11.9 detected)
* A free account on the [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/)
* *Note: Neither Docker nor standalone C++ GDAL installation is required.* Python wheels provide pre-compiled GDAL/PROJ runtimes.

### 2. Create and Activate Virtual Environment
```powershell
# Navigate to the project root
cd D:\Projects\MemoryLane-Satellite

# Create virtual environment
python -m venv .venv

# Activate virtual environment
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
```

---

## Step-by-Step Execution Plan

Once setup is reviewed and confirmed, execution proceeds in 4 decoupled steps:

1. **Search Scenes:**
   ```powershell
   python scripts/search_scenes.py --cloud-max 20 --start-date 2024-09-01 --end-date 2024-10-31
   ```
2. **Download Target Bands (Selective ~100MB instead of ~1GB):**
   ```powershell
   python scripts/download_sentinel.py --scene-id <SELECTED_SCENE_ID>
   ```
3. **Build Masked Composite & Convert to COG:**
   ```powershell
   python processing/build_composite.py
   python processing/create_cog.py
   ```
4. **Launch Local TiTiler Tile Server:**
   ```powershell
   uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
   ```
   * Explore interactive tile viewer: `http://127.0.0.1:8000/docs`
   * Tile endpoint: `http://127.0.0.1:8000/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=D:/Projects/MemoryLane-Satellite/cog/manali_solang_rgb.tif`

---

## License & Data Credits
* Satellite Data: Contains modified Copernicus Sentinel data [2024] processed by MemoryLane-Satellite.
* Codebase: MIT License.


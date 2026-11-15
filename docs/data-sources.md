# Copernicus Data Space Ecosystem (CDSE) Data Source Guide

## 1. Catalog & Authentication Endpoints

* **Portal:** [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/)
* **STAC Root URL:** `https://stac.dataspace.copernicus.eu/v1`
* **STAC Search Endpoint:** `https://stac.dataspace.copernicus.eu/v1/search`
* **Collection:** `sentinel-2-l2a`
* **Identity Provider (OIDC / OAuth2 Keycloak):**
  * URL: `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`
  * Client ID: `cdse-public`
  * Grant Type: `password`
  * Credentials required: `CDSE_USERNAME`, `CDSE_PASSWORD`
* **Object Storage (S3 / eodata):**
  * Endpoint URL: `https://eodata.dataspace.copernicus.eu`
  * Direct S3 URI Scheme: `s3://eodata/...`
  * Translated HTTPS Scheme: `https://eodata.dataspace.copernicus.eu/...` (authorized via Bearer token)
  * Optional S3 Credentials: `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY`

---

## 2. STAC Search & Pagination Mechanism

Searches are executed via `POST` requests to `/search` with JSON payload:
```json
{
  "collections": ["sentinel-2-l2a"],
  "bbox": [77.05, 32.15, 77.30, 32.38],
  "datetime": "2024-09-15T00:00:00Z/2024-10-31T23:59:59Z",
  "query": {
    "eo:cloud_cover": {"lte": 20.0}
  },
  "limit": 100
}
```

### Pagination
When results exceed the `limit`, CDSE STAC includes a `links` array with `rel="next"`:
```json
{
  "rel": "next",
  "href": "https://stac.dataspace.copernicus.eu/v1/search?page=2",
  "method": "GET"
}
```
The search engine automatically follows these links until all pages are retrieved.

---

## 3. MGRS Tile Discovery & Multi-Tile Strategy

Sentinel-2 products are tiled according to the Military Grid Reference System (MGRS), each tile spanning roughly 100 km $\times$ 100 km.

* **Manali–Solang AOI:** `[77.05, 32.15, 77.30, 32.38]` (WGS84 EPSG:4326), spanning $\sim 23\text{ km} \times 26\text{ km}$.
* **Primary Tile:** **`43SDR`** (UTM Zone 43N).
* **Adjacent Potential Intersections:** **`43SDQ`** (South), **`43REQ`** (East).
* **Tile Identification:** Extracted from:
  1. `item.properties["s2:mgrs_tile"]`
  2. MGRS extension (`mgrs:utm_zone` + `mgrs:latitude_band` + `mgrs:grid_square`)
  3. Granule naming convention regex: `_T([0-9]{2}[A-Z]{3})_`

Scenes are grouped by MGRS tile and ranked independently so the complete AOI is covered even if intersecting multiple tiles.

---

## 4. Band Directory, Asset Aliases & Resolution

| Asset Key | Band Name | Central Wavelength (nm) | Resolution (m) | Asset Aliases Handled | Purpose |
| :--- | :--- | :---: | :---: | :--- | :--- |
| `B02` | Blue | 490 | 10 | `B02`, `b02`, `blue`, `B2` | Visual Blue & Analytical Band 3 |
| `B03` | Green | 560 | 10 | `B03`, `b03`, `green`, `B3` | Visual Green & Analytical Band 2 |
| `B04` | Red | 665 | 10 | `B04`, `b04`, `red`, `B4` | Visual Red & Analytical Band 1 |
| **`B08`** | **NIR Broad** | **842** | **10** | `B08`, `b08`, `nir`, `B8` | **Analytical Band 4 (Biomass & NDVI)** |
| **`SCL`** | **Scene Classification** | - | **20** | `SCL`, `scl`, `classification` | **Quality Assurance & Masking** |
| `METADATA_XML` | Granule / Tile Metadata | - | - | `MTD_TL`, `MTD_MSIL2A`, `metadata` | Radiometric offset & scale extraction |

---

## 5. Metadata-Aware Radiometric Calibration

On January 25, 2022, ESA introduced **Processing Baseline 04.00**, adding a negative radiometric offset to surface reflectance data to prevent negative integer truncation during atmospheric correction.

The pipeline implements metadata-aware reflectance parsing:

$$R = \begin{cases} \text{NoData}, & \text{if } DN = 0 \\ \dfrac{DN + \text{BOA\_ADD\_OFFSET}}{\text{QUANTIFICATION\_VALUE}}, & \text{if } DN > 0 \end{cases}$$

### Baseline Parameters
* **Baseline < 04.00 (Before Jan 25, 2022):**
  * `QUANTIFICATION_VALUE = 10000.0`
  * `BOA_ADD_OFFSET = 0.0`
* **Baseline >= 04.00 (Jan 25, 2022 onward, including 05.xx):**
  * `QUANTIFICATION_VALUE = 10000.0`
  * `BOA_ADD_OFFSET = -1000.0` (band-specific offsets extracted from STAC item `properties` or granule `MTD_TL.xml`)

*Pixel value $DN = 0$ is strictly preserved as NoData.*

---

## 6. Multi-Tile Raw Data Directory Organization

Downloaded raw assets are stored in isolated, self-contained directories organized by acquisition date, MGRS tile, and unique scene ID:

```
data/raw/<YYYYMMDD>_<MGRS>_<SCENE_ID>/
├── B02.jp2 (or .tif)
├── B03.jp2 (or .tif)
├── B04.jp2 (or .tif)
├── B08.jp2 (or .tif)
├── SCL.jp2 (or .tif)
├── MTD_TL.xml
├── scene_metadata.json
├── radiometric_metadata.json
└── download_manifest.json
```

* `scene_metadata.json`: Full STAC Item record including geometry, properties, and asset URLs.
* `radiometric_metadata.json`: Parsed per-band calibration parameters (`quantification_value` and `boa_add_offset`).
* `download_manifest.json`: Audit log of local file paths, download timestamp, and integrity status.

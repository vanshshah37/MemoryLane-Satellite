# Copernicus Data Space Ecosystem (CDSE) Data Source Guide

## 1. Catalog & Authentication Endpoints

* **Portal:** [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/)
* **STAC Root URL:** `https://stac.dataspace.copernicus.eu/v1`
* **STAC Search Endpoint:** `https://stac.dataspace.copernicus.eu/v1/search`
* **Identity Provider (OIDC / OAuth2):**
  * URL: `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`
  * Client ID: `cdse-public`
  * Grant Type: `password`
* **Authenticated S3 Storage (`eodata`):**
  * Endpoint URL: `https://eodata.dataspace.copernicus.eu`
  * Protocol: S3-compatible (authenticated via Access Key & Secret Key)
  * Feature: Allows windowed range reading using GDAL `/vsis3/` or `boto3`.

## 2. Collection Details: `sentinel-2-l2a`

Sentinel-2 Level-2A products provide Bottom-Of-Atmosphere (BOA) surface reflectance derived from Level-1C inputs.

* **Revisit Frequency:** 5 days at equator (faster over Manali/Himalayan latitudes).
* **Coordinate Reference System:** Universal Transverse Mercator (UTM) Zone 43N (EPSG:32643) for Manali–Solang.

## 3. Target Area of Interest: Manali–Solang Valley

* **Bounding Box:** `[77.05, 32.15, 77.30, 32.38]` (WGS84 EPSG:4326)
* **Ground Dimensions:**
  * West-East width: ~23.5 km
  * South-North height: ~25.5 km
  * Total area: ~600 km² (~23 × 26 km)
* **Coverage:** Manali town, Solang Valley, Rohtang foothills, Upper Beas watershed.

## 4. Band Directory & Resolution

| Asset Key | Band Name | Central Wavelength (nm) | Resolution (m) | Purpose in MemoryLane-Satellite |
| :--- | :--- | :--- | :--- | :--- |
| `B01` | Coastal aerosol | 443 | 60 | Atmospheric correction |
| `B02` | Blue | 490 | 10 | Visual Blue channel & Analytical Band 3 |
| `B03` | Green | 560 | 10 | Visual Green channel & Analytical Band 2 |
| `B04` | Red | 665 | 10 | Visual Red channel & Analytical Band 1 |
| `B05` | Vegetation Red Edge 1 | 705 | 20 | Vegetation analysis |
| `B06` | Vegetation Red Edge 2 | 740 | 20 | Vegetation analysis |
| `B07` | Vegetation Red Edge 3 | 783 | 20 | Vegetation analysis |
| **`B08`** | **NIR Broad** | **842** | **10** | **Analytical Band 4 (Biomass, NDVI, Water Edge)** |
| `B8A` | NIR Narrow | 865 | 20 | Water vapor absorption reference |
| `B09` | Water vapour | 945 | 60 | Water vapor correction |
| `B11` | SWIR 1 | 1610 | 20 | Moisture / Snow vs Cloud |
| `B12` | SWIR 2 | 2190 | 20 | Geology & burn scars |
| `AOT` | Aerosol Optical Thickness | - | 10/20 | Atmospheric quality assessment |
| `WVP` | Water Vapour | - | 10/20 | Column water vapor |
| **`SCL`** | **Scene Classification Layer**| - | **20** | **Quality Assurance & Masking (Separate QA Raster)** |
| `visual` | True Color Image (TCI) | - | 10 | Pre-generated 8-bit RGB |

---

## 5. Metadata-Aware Radiometric Calibration

On January 25, 2022, ESA introduced **Processing Baseline 04.00**, adding a negative radiometric offset to surface reflectance data to prevent negative integer truncation during atmospheric correction.

The pipeline implements metadata-aware reflectance parsing:

$$R = \frac{DN + \text{BOA\_ADD\_OFFSET}}{\text{QUANTIFICATION\_VALUE}}$$

### Baseline Parameters
* **Baseline < 04.00 (Before Jan 25, 2022):**
  * `QUANTIFICATION_VALUE = 10000.0`
  * `BOA_ADD_OFFSET = 0.0`
* **Baseline >= 04.00 (Jan 25, 2022 onward, including 05.xx):**
  * `QUANTIFICATION_VALUE = 10000.0`
  * `BOA_ADD_OFFSET = -1000.0` (band-specific offsets extracted from STAC item `properties` or granule `MTD_TL.xml`)

### NoData Preservation Rule
If $DN = 0$, the pixel represents **NoData** (granule edge or invalid sensor pixel). The offset is never applied to $DN = 0$; it is strictly maintained as NoData.

---

## 6. Scene Classification Layer (SCL) Reference & Policy

The 20m SCL raster is resampled to 10m via nearest-neighbor interpolation and kept as a separate QA raster (`data/processed/<scene_id>_scl.tif`).

| Pixel Value | Class Label | Cloud/QA Action | Rationale |
| :---: | :--- | :--- | :--- |
| `0` | NO_DATA | **Masked** (Alpha=0) | Fill pixels outside swath |
| `1` | SATURATED_OR_DEFECTIVE | **Masked** (Alpha=0) | Sensor saturation / bad pixels |
| `2` | DARK_AREA_PIXELS | Retained | Mountain shadows (preserved to avoid terrain holes) |
| `3` | CLOUD_SHADOWS | **Masked** (Alpha=0) | Ground cast shadows from clouds |
| `4` | VEGETATION | Retained | Pine forests, alpine pastures |
| `5` | NOT_VEGETATED | Retained | Bare rock, gravel, moraine |
| `6` | WATER | Retained | Beas River, glacial lakes |
| `7` | UNCLASSIFIED | **Masked** (Alpha=0) | **Configurable:** Marginal / ambiguous haze / poor quality |
| `8` | CLOUD_MEDIUM_PROBABILITY | **Masked** (Alpha=0) | Probable cloud cover |
| `9` | CLOUD_HIGH_PROBABILITY | **Masked** (Alpha=0) | Dense cumulus cloud cover |
| `10` | THIN_CIRRUS | **Masked** (Alpha=0) | High-altitude thin ice clouds |
| `11` | SNOW_OR_ICE | **RETAINED** | **Preserved:** High Himalayan snow cover & glaciers |

### Cloud Edge Buffering
A configurable morphological dilation (default: 30 meters / 3 pixels on a 10m grid) is applied to the union of classes `[0, 1, 3, 7, 8, 9, 10]`. Snow pixels (`class 11`) are explicitly excluded to prevent eroding snowpack.

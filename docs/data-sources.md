# Copernicus Data Space Ecosystem (CDSE) Data Source Guide

## 1. Catalog & Authentication Endpoints

* **Portal:** [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/)
* **STAC Root URL:** `https://stac.dataspace.copernicus.eu/v1`
* **STAC Search:** `https://stac.dataspace.copernicus.eu/v1/search`
* **Identity Provider (OIDC / OAuth2):**
  * URL: `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`
  * Client ID: `cdse-public`
  * Grant Type: `password`

## 2. Collection Details: `sentinel-2-l2a`

Sentinel-2 Level-2A products provide Bottom-Of-Atmosphere (BOA) surface reflectance derived from associated Level-1C products.

* **Revisit Frequency:** 5 days at equator (faster at Himalayan latitudes with Sentinel-2A and Sentinel-2B constellated).
* **Radiometric Resolution:** 12-bit native sensor digitized to 15-bit integer (Quantification Value: 10,000 = 1.0 surface reflectance).
* **Coordinate Reference System:** Universal Transverse Mercator (UTM) with WGS84 ellipsoid. For Manali/Solang, this is UTM Zone 43N (EPSG:32643).

## 3. Band Directory & Resolution

| Asset Key | Band Name | Central Wavelength (nm) | Resolution (m) | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `B01` | Coastal aerosol | 443 | 60 | Atmospheric correction |
| `B02` | Blue | 490 | 10 | Visual Blue channel |
| `B03` | Green | 560 | 10 | Visual Green channel |
| `B04` | Red | 665 | 10 | Visual Red channel |
| `B05` | Vegetation Red Edge 1 | 705 | 20 | Vegetation analysis |
| `B06` | Vegetation Red Edge 2 | 740 | 20 | Vegetation analysis |
| `B07` | Vegetation Red Edge 3 | 783 | 20 | Vegetation analysis |
| `B08` | NIR Broad | 842 | 10 | Biomass & water edge |
| `B8A` | NIR Narrow | 865 | 20 | Water vapor absorption reference |
| `B09` | Water vapour | 945 | 60 | Water vapor correction |
| `B11` | SWIR 1 | 1610 | 20 | Moisture / Snow vs Cloud |
| `B12` | SWIR 2 | 2190 | 20 | Geology & burn scars |
| `AOT` | Aerosol Optical Thickness | - | 10/20 | Atmospheric quality assessment |
| `WVP` | Water Vapour | - | 10/20 | Column water vapor |
| `SCL` | Scene Classification Layer| - | 20 | Pixel-level classification |
| `visual` | True Color Image (TCI) | - | 10 | Pre-generated 8-bit RGB |

## 4. Scene Classification Layer (SCL) Reference

The SCL raster provides quality classification for each pixel at 20m resolution:

| Pixel Value | Class Label | Cloud Masking Action in Solang Pipeline |
| :---: | :--- | :--- |
| `0` | NO_DATA | Mask (Transparent) |
| `1` | SATURATED_OR_DEFECTIVE | Mask |
| `2` | DARK_AREA_PIXELS | Retain |
| `3` | CLOUD_SHADOWS | Mask (Transparent) |
| `4` | VEGETATION | Retain (Keep) |
| `5` | NOT_VEGETATED | Retain (Keep) |
| `6` | WATER | Retain (Keep) |
| `7` | UNCLASSIFIED | Retain |
| `8` | CLOUD_MEDIUM_PROBABILITY | Mask (Transparent) |
| `9` | CLOUD_HIGH_PROBABILITY | Mask (Transparent) |
| `10` | THIN_CIRRUS | Mask (Transparent) |
| `11` | SNOW_OR_ICE | **Retain (Keep)** - Critical for Himalayan peaks! |


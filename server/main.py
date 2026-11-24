"""MemoryLane-Satellite TiTiler Dynamic Tile Server.

FastAPI application that serves Cloud Optimized GeoTIFFs (COG) to MapLibre GL JS
frontends via Slippy Map XYZ raster tile endpoints and TileJSON.
"""

import argparse
import io
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import rasterio
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from titiler.core.factory import TilerFactory

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Default COG path
DEFAULT_COG_PATH = Path("cog/manali_solang_rgb.tif")
STATIC_INDEX_PATH = Path("server/index.html")

app = FastAPI(
    title="MemoryLane-Satellite TiTiler Server",
    description="Zero-cost, self-hosted dynamic tile server for Sentinel-2 L2A Cloud Optimized GeoTIFFs.",
    version="1.0.0",
)

# Enable CORS for local web mapping frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Standard TiTiler endpoints mounted at /cog (e.g. /cog/info, /cog/tiles/...)
cog_factory = TilerFactory(router_prefix="/cog")
app.include_router(cog_factory.router, prefix="/cog", tags=["TiTiler COG API"])

# 1x1 fully transparent PNG (68 bytes, valid PNG specification with matching CRCs)
TRANSPARENT_1X1_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc`\x00"
    b"\x02\x00\x00\x05\x00\x01z^\xab?\x00\x00\x00\x00IEND\xaeB`\x82"
)


def get_active_cog_path() -> Path:
    """Returns the active COG file path from environment or default."""
    env_path = os.getenv("SATELLITE_COG_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
    return DEFAULT_COG_PATH


@app.get("/", summary="Standalone MapLibre GL Preview")
async def get_preview():
    """Serves the standalone MapLibre visual preview HTML page."""
    if STATIC_INDEX_PATH.is_file():
        return FileResponse(str(STATIC_INDEX_PATH), media_type="text/html")
    return HTMLResponse("<h1>MemoryLane-Satellite TiTiler Running</h1><p>Visit /tilejson.json</p>")


@app.get("/health", summary="Health Check")
async def health():
    """Health check endpoint reporting COG existence and server status."""
    cog = get_active_cog_path()
    return {
        "status": "healthy",
        "service": "MemoryLane-Satellite TiTiler",
        "cog_path": str(cog),
        "cog_exists": cog.is_file(),
        "cog_size_bytes": cog.stat().st_size if cog.is_file() else 0,
    }


@app.get("/tiles/{z}/{x}/{y}.png", summary="XYZ Raster Tile")
async def get_tile(z: int, x: int, y: int):
    """Renders a 256x256 PNG raster tile from the local satellite COG."""
    cog = get_active_cog_path()
    if not cog.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Satellite COG not found at {cog}. Please run pipeline to generate it.",
        )

    try:
        with Reader(str(cog)) as src:
            img = src.tile(x, y, z)
            rendered = img.render(img_format="PNG")
            return Response(
                content=rendered,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except TileOutsideBounds:
        # Return transparent 1x1 PNG for map background without triggering browser console 404 errors
        return Response(
            content=TRANSPARENT_1X1_PNG,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception as e:
        logger.warning(f"Tile read error at {z}/{x}/{y}: {e}")
        return Response(content=TRANSPARENT_1X1_PNG, media_type="image/png")


@app.get("/tilejson.json", summary="TileJSON 3.0 Metadata")
async def get_tilejson():
    """Returns TileJSON 3.0 specification for MapLibre GL / Leaflet."""
    cog = get_active_cog_path()
    bounds = [77.05, 32.15, 77.30, 32.38]
    center = [77.175, 32.265, 12]

    if cog.is_file():
        try:
            with Reader(str(cog)) as src:
                info = src.info()
                bounds = list(info.bounds)
                center = [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, 12]
        except Exception as e:
            logger.warning(f"Could not read COG bounds: {e}")

    return {
        "tilejson": "3.0.0",
        "name": "Sentinel-2 L2A Manali-Solang RGB Composite",
        "description": "10m natural color satellite mosaic covering Manali and Solang Valley, HP, India.",
        "version": "1.0.0",
        "attribution": "Contains modified Copernicus Sentinel data [2024] / MemoryLane-Satellite",
        "scheme": "xyz",
        "tiles": ["/tiles/{z}/{x}/{y}.png"],
        "minzoom": 8,
        "maxzoom": 16,
        "bounds": bounds,
        "center": center,
    }


@app.get("/info", summary="Dataset Spatial Metadata")
async def get_info():
    """Returns spatial properties, CRS, dimensions, resolution, and band count."""
    cog = get_active_cog_path()
    if not cog.is_file():
        return {
            "status": "pending_generation",
            "message": "Satellite COG not yet generated.",
        }

    with rasterio.open(cog) as src:
        bounds = src.bounds
        res = src.res
        return {
            "file": str(cog),
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "crs": src.crs.to_string() if src.crs else "Unknown",
            "resolution": {"x": round(res[0], 2), "y": round(res[1], 2)},
            "bounds": {
                "left": bounds.left,
                "bottom": bounds.bottom,
                "right": bounds.right,
                "top": bounds.top,
            },
            "dtypes": src.dtypes,
            "nodata": src.nodata,
            "source": "Copernicus Sentinel-2 L2A (10m)",
            "datetime": "2024-10-15 (Orbit R105)",
            "coverage": "Manali–Solang, Himachal Pradesh",
            "tiles": ["43SFR", "43SGR"],
        }


def run_server(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    logger.info(f"Starting MemoryLane-Satellite TiTiler server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MemoryLane-Satellite TiTiler Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    parser.add_argument("--cog-path", default=None, help="Path to RGB Cloud Optimized GeoTIFF")
    args = parser.parse_args()

    if args.cog_path:
        os.environ["SATELLITE_COG_PATH"] = args.cog_path

    run_server(host=args.host, port=args.port)

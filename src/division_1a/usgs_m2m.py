import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml
from shapely.geometry import mapping, shape

from src.utils.logger import get_logger
from src.utils.retry import retry_with_backoff

logger = get_logger(__name__)


def load_config(config_path: str = "config/settings.yaml") -> Dict[str, Any]:
    with open(config_path, "r") as f:
        raw = f.read()

    def replace_env(match):
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise EnvironmentError(f"Environment variable '{var_name}' is not set.")
        return value

    resolved = re.sub(r"\$\{(\w+)\}", replace_env, raw)
    return yaml.safe_load(resolved)


class USGSM2MClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.usgs_cfg = config["usgs"]
        self.endpoint = self.usgs_cfg["api_endpoint"]
        self.api_key: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "TAPAS-Pipeline/1.0"
        })

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()
        return False

    # ── Auth ──────────────────────────────────────────────────

    def login(self) -> None:
        logger.info("Logging in to USGS M2M API...")
        response = self._post("login-token", {
            "username": self.usgs_cfg["username"],
            "token": self.usgs_cfg["token"],
        })
        self.api_key = response["data"]
        self.session.headers.update({"X-Auth-Token": self.api_key})
        logger.info("Login successful.")

    def logout(self) -> None:
        if not self.api_key:
            return
        try:
            self._post("logout", {})
            logger.info("Logged out.")
        except Exception as exc:
            logger.warning(f"Logout failed (non-critical): {exc}")
        finally:
            self.api_key = None
            self.session.headers.pop("X-Auth-Token", None)

    # ── Search ────────────────────────────────────────────────

    def search_scenes(
        self,
        dataset_name: str,
        aoi_geojson_path: str,
        date_start: str,
        date_end: str,
        max_cloud_cover: int = 15,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        logger.info(
            f"Searching '{dataset_name}' | {date_start} to {date_end} | "
            f"cloud <= {max_cloud_cover}%"
        )
        aoi = self._load_aoi(aoi_geojson_path)
        payload = {
            "datasetName": dataset_name,
            "maxResults": max_results,
            "startingNumber": 1,
            "sceneFilter": {
                "spatialFilter": {"filterType": "geojson", "geoJson": aoi},
                "acquisitionFilter": {"start": date_start, "end": date_end},
                "cloudCoverFilter": {"min": 0, "max": max_cloud_cover, "includeUnknown": False},
            },
        }
        response = self._post("scene-search", payload)
        results = response.get("data", {})
        scenes = results.get("results", [])
        total = results.get("totalHits", 0)
        logger.info(f"Found {len(scenes)} scenes ({total} total hits).")
        if not scenes:
            logger.warning("No scenes returned. Check AOI, dates, or cloud cover threshold.")
        return scenes

    # ── Download resolution ───────────────────────────────────

    def resolve_download_options(
        self,
        dataset_name: str,
        entity_ids: List[str],
        product_filter: str = "Bundle",
    ) -> List[Dict[str, Any]]:
        logger.info(f"Resolving download options for {len(entity_ids)} scenes...")
        response = self._post("download-options", {
            "datasetName": dataset_name,
            "entityIds": entity_ids,
        })
        options = response.get("data", [])
        downloadable = [
            {
                "entityId": o["entityId"],
                "productId": o["id"],
                "productName": o.get("productName", ""),
                "filesize": o.get("filesize", 0),
            }
            for o in options
            if o.get("available", False) and product_filter.lower() in o.get("productName", "").lower()
        ]
        logger.info(f"Resolved {len(downloadable)} downloadable products.")
        if not downloadable:
            available_names = list(set(o.get("productName", "") for o in options))
            logger.warning(f"No match for filter '{product_filter}'. Available: {available_names}")
        return downloadable

    def request_downloads(
        self,
        dataset_name: str,
        products: List[Dict[str, Any]],
        label: str = "tapas_run",
    ) -> List[Dict[str, Any]]:
        logger.info(f"Requesting downloads for {len(products)} products...")
        payload = {
            "downloads": [{"entityId": p["entityId"], "productId": p["productId"]} for p in products],
            "label": label,
            "returnAvailable": True,
        }
        response = self._post("download-request", payload)
        data = response.get("data", {})
        available = data.get("availableDownloads", [])
        queued = data.get("preparingDownloads", [])
        logger.info(f"{len(available)} available immediately, {len(queued)} queued.")
        if queued:
            available += self._poll_queued_downloads(label)
        return available

    def _poll_queued_downloads(
        self,
        label: str,
        poll_interval: int = 30,
        max_polls: int = 20,
    ) -> List[Dict[str, Any]]:
        logger.info(f"Polling for queued downloads (label='{label}')...")
        all_ready = []
        for poll_num in range(1, max_polls + 1):
            time.sleep(poll_interval)
            logger.info(f"Poll {poll_num}/{max_polls}...")
            response = self._post("download-retrieve", {"label": label})
            data = response.get("data", {})
            all_ready.extend(data.get("available", []))
            if not data.get("requested", []):
                logger.info("All queued downloads ready.")
                break
        else:
            logger.warning(f"Max polls reached. Some scenes may still be preparing.")
        return all_ready

    # ── Download ──────────────────────────────────────────────

    def download_scenes(
        self,
        available_downloads: List[Dict[str, Any]],
        output_dir: str,
    ) -> List[Path]:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        chunk_size = self.config.get("download", {}).get("chunk_size_bytes", 8_388_608)
        downloaded_paths = []
        total = len(available_downloads)

        for idx, item in enumerate(available_downloads, start=1):
            url = item.get("url")
            display_id = item.get("displayId", item.get("entityId", f"scene_{idx}"))

            if not url:
                logger.warning(f"[{idx}/{total}] No URL for '{display_id}'. Skipping.")
                continue

            filename = url.split("/")[-1].split("?")[0]
            if not filename.endswith(".tar"):
                filename = f"{display_id}.tar"
            dest = out_path / filename

            if dest.exists():
                logger.info(f"[{idx}/{total}] Already exists, skipping: {dest.name}")
                downloaded_paths.append(dest)
                continue

            logger.info(f"[{idx}/{total}] Downloading: {display_id}")
            try:
                path = self._stream_download(url, dest, chunk_size, display_id)
                downloaded_paths.append(path)
                logger.info(f"[{idx}/{total}] Done: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
            except Exception as exc:
                logger.error(f"[{idx}/{total}] Failed '{display_id}': {exc}")
                if dest.exists():
                    dest.unlink()

        logger.info(f"Download complete: {len(downloaded_paths)}/{total} scenes.")
        return downloaded_paths

    @retry_with_backoff(max_attempts=5, base_delay=2.0)
    def _stream_download(self, url: str, dest: Path, chunk_size: int, display_id: str) -> Path:
        temp = dest.with_suffix(dest.suffix + ".tmp")
        response = self.session.get(url, stream=True, timeout=60, allow_redirects=True)
        if response.status_code != 200:
            raise requests.exceptions.HTTPError(f"HTTP {response.status_code}")

        sha256 = hashlib.sha256()
        with open(temp, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    sha256.update(chunk)

        temp.rename(dest)
        dest.with_suffix(".sha256").write_text(f"{sha256.hexdigest()}  {dest.name}\n")
        return dest

    # ── Internals ─────────────────────────────────────────────

    @retry_with_backoff(max_attempts=5, base_delay=2.0)
    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.endpoint}{endpoint}"
        response = self.session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
        error_code = body.get("errorCode")
        if error_code:
            raise ValueError(f"M2M error [{error_code}]: {body.get('errorMessage', '')}")
        return body

    @staticmethod
    def _load_aoi(geojson_path: str) -> Dict[str, Any]:
        path = Path(geojson_path)
        if not path.exists():
            raise FileNotFoundError(f"AOI file not found: {geojson_path}")
        with open(path, "r") as f:
            geojson = json.load(f)

        if geojson.get("type") == "FeatureCollection":
            geojson = geojson["features"][0]["geometry"]
        elif geojson.get("type") == "Feature":
            geojson = geojson["geometry"]

        geom = shape(geojson)
        if not geom.is_valid:
            logger.warning("AOI geometry invalid, attempting repair...")
            geom = geom.buffer(0)
            geojson = mapping(geom)

        logger.info(f"AOI loaded: {geojson['type']}, bounds={geom.bounds}")
        return geojson


# ── Orchestrator ──────────────────────────────────────────────

def run_landsat_ingestion(
    aoi_geojson_path: str,
    config_path: str = "config/settings.yaml",
    dataset_key: str = "dataset_name_landsat8",
) -> List[Path]:
    config = load_config(config_path)
    search_cfg = config["search"]
    dl_cfg = config["download"]
    dataset_name = config["usgs"][dataset_key]

    with USGSM2MClient(config) as client:
        scenes = client.search_scenes(
            dataset_name=dataset_name,
            aoi_geojson_path=aoi_geojson_path,
            date_start=search_cfg["date_start"],
            date_end=search_cfg["date_end"],
            max_cloud_cover=search_cfg["max_cloud_cover"],
            max_results=search_cfg["max_results"],
        )
        if not scenes:
            logger.warning("No scenes found. Exiting.")
            return []

        entity_ids = [s["entityId"] for s in scenes]
        manifest_path = Path(dl_cfg["output_dir"]) / "scene_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as f:
            json.dump({"dataset": dataset_name, "scenes": scenes}, f, indent=2, default=str)
        logger.info(f"Manifest saved: {manifest_path}")

        products = client.resolve_download_options(dataset_name, entity_ids)
        if not products:
            logger.error("No downloadable products found. Exiting.")
            return []

        available = client.request_downloads(dataset_name, products)
        if not available:
            logger.error("No download URLs returned. Exiting.")
            return []

        return client.download_scenes(available, dl_cfg["output_dir"])


if __name__ == "__main__":
    paths = run_landsat_ingestion("config/aoi_geometries/india_north.geojson")
    print(f"\n{len(paths)} scene(s) downloaded.")
    for p in paths:
        print(f"  {p}")

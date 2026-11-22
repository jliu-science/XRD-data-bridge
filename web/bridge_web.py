"""Browser-only transport for the shared XRD dashboard, running in Pyodide."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from converter import inspect_hdf5_tree
from sample_data_gen import generate_demo_dataset
from watcher import FolderBridgeManager


WORK = Path("/work")
WORK.mkdir(exist_ok=True)
manager = FolderBridgeManager(WORK / "config.json", WORK / "state.json")
manager.update_config(
    {"input_dir": str(WORK / "input"), "custom_output_dir": str(WORK / "output"), "output_mode": "custom_dir"}
)
manager.logs.clear()

SETTINGS = {
    "include_subfolders", "anode", "target_anode", "output_formats", "asc_flavor",
    "angle_dataset_override", "intensity_dataset_override",
}


def _source(path: str) -> Path:
    source = Path(path).resolve()
    if not source.is_relative_to(Path(manager.config.input_dir).resolve()) or not source.is_file():
        raise ValueError("Select a scan from the current raw data folder.")
    return source


def _download(converted: list[dict]) -> bool:
    output_dir = Path(manager.config.custom_output_dir)
    files = {
        Path(path)
        for result in converted
        for scan in result.get("outputs", [])
        for path in scan["files"].values()
    }
    if not files:
        return False
    with ZipFile(WORK / "converted.zip", "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(output_dir))
    return True


def _dispatch(route: str, data: dict) -> dict:
    if route == "/api/state":
        return manager.get_dashboard_state()
    if route == "/api/config":
        manager.update_config({key: data[key] for key in SETTINGS & data.keys()})
    elif route == "/api/browse-folder":
        folder = Path(data["path"]).resolve()
        if folder.parent != WORK / "imports" or not folder.is_dir():
            raise ValueError("Choose files or a folder with the browser picker.")
        manager.update_config({"input_dir": str(folder)})
        return {"ok": True, "path": folder.name, "state": manager.get_dashboard_state()}
    elif route == "/api/generate-demo":
        created = generate_demo_dataset(manager.config.input_dir, include_sample_report=True)
        return {"created": [p.name for p in created], "state": manager.get_dashboard_state()}
    elif route == "/api/convert-all":
        result = manager.convert_all(subfolder_filter=data.get("subfolder", ""))
        return {"result": result, "download": _download(result["converted"]), "state": manager.get_dashboard_state()}
    elif route == "/api/convert-one":
        result = manager.convert_file(_source(data["path"]))
        return {"result": result, "download": _download([result]), "state": manager.get_dashboard_state()}
    elif route == "/api/preview":
        return manager.preview_scan(_source(data["path"]))
    elif route == "/api/hdf5-tree":
        return inspect_hdf5_tree(_source(data["path"]))
    else:
        raise ValueError("This action requires the desktop bridge.")
    return {"state": manager.get_dashboard_state()}


def request(route: str, payload: str) -> str:
    # Match the desktop API's JSON shape, with errors handled by the shared UI.
    try:
        return json.dumps(_dispatch(route, json.loads(payload)))
    except Exception as exc:
        return json.dumps({"error": str(exc)})

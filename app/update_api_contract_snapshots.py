"""Regenerate the checked-in OpenAPI and ordered route contract snapshots."""

import json
from pathlib import Path


CONTRACT_DIR = Path(__file__).with_name("api_contract")
OPENAPI_SNAPSHOT = CONTRACT_DIR / "openapi.json"
ROUTES_SNAPSHOT = CONTRACT_DIR / "routes.json"


def get_route_manifest(application):
    """Return the ordered public routing contract for a FastAPI application."""
    return [
        {
            "index": index,
            "methods": sorted(getattr(route, "methods", []) or []),
            "name": route.name,
            "path": route.path,
            "type": type(route).__name__,
        }
        for index, route in enumerate(application.routes)
    ]


def write_json_snapshot(path, data):
    """Write deterministic, reviewable JSON to a contract snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_snapshots(application, contract_dir=CONTRACT_DIR):
    """Write OpenAPI and ordered route snapshots for application."""
    openapi_path = Path(contract_dir) / OPENAPI_SNAPSHOT.name
    routes_path = Path(contract_dir) / ROUTES_SNAPSHOT.name
    write_json_snapshot(openapi_path, application.openapi())
    write_json_snapshot(routes_path, get_route_manifest(application))
    return openapi_path, routes_path


def main():
    import app as app_module

    openapi_path, routes_path = write_snapshots(app_module.app)
    print(f"Updated {openapi_path}")
    print(f"Updated {routes_path}")


if __name__ == "__main__":
    main()

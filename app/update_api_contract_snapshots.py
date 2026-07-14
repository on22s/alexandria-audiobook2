"""Regenerate the checked-in OpenAPI and ordered route contract snapshots."""

import argparse
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


def _route_key(route):
    return (route["path"], tuple(route["methods"]), route["name"], route["type"])


def _operations(openapi):
    methods = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
    return {
        (method.upper(), path): operation
        for path, path_item in openapi.get("paths", {}).items()
        for method, operation in path_item.items()
        if method in methods
    }


def compare_contracts(expected_openapi, expected_routes, actual_openapi, actual_routes):
    """Return deterministic, human-readable differences between two contracts."""
    differences = []
    expected_route_keys = [_route_key(route) for route in expected_routes]
    actual_route_keys = [_route_key(route) for route in actual_routes]
    expected_route_set = set(expected_route_keys)
    actual_route_set = set(actual_route_keys)
    for key in sorted(expected_route_set - actual_route_set):
        differences.append(f"Route removed: {'/'.join(key[1])} {key[0]} ({key[2]})")
    for key in sorted(actual_route_set - expected_route_set):
        differences.append(f"Route added: {'/'.join(key[1])} {key[0]} ({key[2]})")
    shared_route_keys = expected_route_set & actual_route_set
    expected_shared_routes = [key for key in expected_route_keys if key in shared_route_keys]
    actual_shared_routes = [key for key in actual_route_keys if key in shared_route_keys]
    if expected_shared_routes != actual_shared_routes:
        actual_indexes = {key: index for index, key in enumerate(actual_shared_routes)}
        for old_index, key in enumerate(expected_shared_routes):
            new_index = actual_indexes[key]
            if old_index != new_index:
                differences.append(
                    f"Route reordered: {'/'.join(key[1])} {key[0]} {old_index} -> {new_index}"
                )

    expected_operations = _operations(expected_openapi)
    actual_operations = _operations(actual_openapi)
    for method, path in sorted(expected_operations.keys() - actual_operations.keys()):
        differences.append(f"Operation removed: {method} {path}")
    for method, path in sorted(actual_operations.keys() - expected_operations.keys()):
        differences.append(f"Operation added: {method} {path}")
    for method, path in sorted(expected_operations.keys() & actual_operations.keys()):
        if expected_operations[(method, path)] != actual_operations[(method, path)]:
            differences.append(f"Operation changed: {method} {path}")

    expected_schemas = expected_openapi.get("components", {}).get("schemas", {})
    actual_schemas = actual_openapi.get("components", {}).get("schemas", {})
    for name in sorted(expected_schemas.keys() - actual_schemas.keys()):
        differences.append(f"Schema removed: {name}")
    for name in sorted(actual_schemas.keys() - expected_schemas.keys()):
        differences.append(f"Schema added: {name}")
    constraint_keys = (
        "default", "enum", "exclusiveMaximum", "exclusiveMinimum", "maximum",
        "maxItems", "maxLength", "minimum", "minItems", "minLength", "pattern", "type",
    )
    for name in sorted(expected_schemas.keys() & actual_schemas.keys()):
        expected_schema = expected_schemas[name]
        actual_schema = actual_schemas[name]
        expected_required = set(expected_schema.get("required", []))
        actual_required = set(actual_schema.get("required", []))
        for field in sorted(expected_required - actual_required):
            differences.append(f"Required field removed: {name}.{field}")
        for field in sorted(actual_required - expected_required):
            differences.append(f"Required field added: {name}.{field}")
        expected_properties = expected_schema.get("properties", {})
        actual_properties = actual_schema.get("properties", {})
        for field in sorted(expected_properties.keys() - actual_properties.keys()):
            differences.append(f"Schema property removed: {name}.{field}")
        for field in sorted(actual_properties.keys() - expected_properties.keys()):
            differences.append(f"Schema property added: {name}.{field}")
        for field in sorted(expected_properties.keys() & actual_properties.keys()):
            before = expected_properties[field]
            after = actual_properties[field]
            for key in constraint_keys:
                if before.get(key) != after.get(key):
                    differences.append(
                        f"Schema constraint changed: {name}.{field}.{key}: "
                        f"{before.get(key)!r} -> {after.get(key)!r}"
                    )
        if expected_schema != actual_schema and not any(
            line.startswith((f"Required field added: {name}.",
                             f"Required field removed: {name}.",
                             f"Schema property added: {name}.",
                             f"Schema property removed: {name}.",
                             f"Schema constraint changed: {name}."))
            for line in differences
        ):
            differences.append(f"Schema changed: {name}")
    return differences


def check_snapshots(application, contract_dir=CONTRACT_DIR):
    """Compare current contracts to disk without writing either snapshot."""
    contract_dir = Path(contract_dir)
    expected_openapi = json.loads((contract_dir / OPENAPI_SNAPSHOT.name).read_text(encoding="utf-8"))
    expected_routes = json.loads((contract_dir / ROUTES_SNAPSHOT.name).read_text(encoding="utf-8"))
    return compare_contracts(
        expected_openapi, expected_routes, application.openapi(), get_route_manifest(application)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing snapshots")
    args = parser.parse_args()
    import app as app_module

    if args.check:
        differences = check_snapshots(app_module.app)
        if differences:
            print("API contract drift detected:")
            for difference in differences:
                print(f"- {difference}")
            raise SystemExit(1)
        print("API contract snapshots match.")
        return
    openapi_path, routes_path = write_snapshots(app_module.app)
    print(f"Updated {openapi_path}")
    print(f"Updated {routes_path}")


if __name__ == "__main__":
    main()

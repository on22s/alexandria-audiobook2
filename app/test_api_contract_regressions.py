import asyncio
import ast
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app as app_module
import core as core_module
import config_settings
import generate_script
import project as project_module
from routers import preparer as preparer_module
from routers import lora as lora_module
from routers import voice_design as voice_design_module
from routers import voice_library as voice_library_module
from routers import voices as voices_module
from routers import scripts_library as scripts_library_module
from routers import system as system_module
import utils
import hf_utils
import update_api_contract_snapshots as api_contract
from lmstudio_settings import get_effective_max_tokens, TokenBudgetError
from test_support import _Upload


class ApiContractTests(unittest.TestCase):
    def test_app_py_contains_no_http_route_decorators(self):
        app_path = Path(__file__).with_name("app.py")
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        http_decorators = {
            "api_route", "delete", "get", "head", "options", "patch", "post",
            "put", "trace", "websocket", "websocket_route",
        }
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "app"
                        and target.attr in http_decorators):
                    violations.append((node.name, target.attr, node.lineno))

        self.assertEqual([], violations)

    def test_app_router_registration_order_is_stable(self):
        app_path = Path(__file__).with_name("app.py")
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        registered = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if (isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "app"
                    and call.func.attr == "include_router"
                    and call.args
                    and isinstance(call.args[0], ast.Name)):
                registered.append(call.args[0].id)

        self.assertEqual([
            "system_router",
            "script_router",
            "voices_router",
            "editor_router",
            "scripts_library_router",
            "voice_library_router",
            "voice_design_router",
            "lora_router",
            "dataset_builder_router",
            "preparer_router",
            "voicelab_router",
        ], registered)

    def test_api_routes_have_no_duplicate_method_path_pairs(self):
        seen = {}
        duplicates = []
        for route in app_module.app.routes:
            for method in sorted(getattr(route, "methods", []) or []):
                key = (method, route.path)
                if key in seen:
                    duplicates.append((key, seen[key], route.name))
                else:
                    seen[key] = route.name

        self.assertEqual([], duplicates)

    def test_openapi_operation_ids_are_unique(self):
        seen = {}
        duplicates = []
        for path, path_item in app_module.app.openapi()["paths"].items():
            for method, operation in path_item.items():
                if not isinstance(operation, dict) or "operationId" not in operation:
                    continue
                operation_id = operation["operationId"]
                if operation_id in seen:
                    duplicates.append((operation_id, seen[operation_id], (method, path)))
                else:
                    seen[operation_id] = (method, path)

        self.assertEqual([], duplicates)

    def test_api_contract_snapshots_match_and_regenerate_deterministically(self):
        expected_openapi = json.loads(api_contract.OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
        expected_routes = json.loads(api_contract.ROUTES_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(expected_openapi, app_module.app.openapi(),
                         "OpenAPI changed; review it and regenerate the contract snapshot")
        self.assertEqual(expected_routes, api_contract.get_route_manifest(app_module.app),
                         "Routes changed; review them and regenerate the contract snapshot")

        with tempfile.TemporaryDirectory() as tmp:
            openapi_path, routes_path = api_contract.write_snapshots(app_module.app, tmp)
            self.assertEqual(api_contract.OPENAPI_SNAPSHOT.read_bytes(), openapi_path.read_bytes())
            self.assertEqual(api_contract.ROUTES_SNAPSHOT.read_bytes(), routes_path.read_bytes())

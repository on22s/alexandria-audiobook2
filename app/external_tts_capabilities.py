import hashlib
import json
import math
import re
from json_metadata import get_metadata_json_bytes

ADAPTER_VERSION = "external-tts-capabilities-v1"
MAX_METADATA_BYTES = 128 * 1024
MAX_PROTOCOL_RESPONSE_BYTES = 16 * 1024
MAX_REPORT_BYTES = MAX_PROTOCOL_RESPONSE_BYTES - 256
MAX_ENDPOINT_COUNT = 64
MAX_PARAMETER_COUNT = 32
CUSTOM_PARAMETERS = ("text", "language", "speaker", "instruct", "model_size", "seed")
CLONE_PARAMETERS = ("ref_audio", "ref_text", "text", "language", "use_xvector_only", "model_size", "max_chunk_chars", "chunk_gap", "seed")
PARAMETER_ALIASES = {"audio": "ref_audio", "instruct_text": "instruct"}
ENDPOINT_ALIASES = {"custom": ("/generate_custom_voice", "/custom_voice"), "clone": ("/generate_voice_clone", "/voice_clone")}
ALL_ENDPOINT_ALIASES = {alias for aliases in ENDPOINT_ALIASES.values() for alias in aliases}
MODEL_IDS = {"custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}
EXPECTED_KINDS = {"text": "str", "language": "str", "speaker": "str", "instruct": "str", "model_size": "str", "model_id": "str", "seed": "int", "ref_audio": "file", "ref_text": "str", "use_xvector_only": "bool", "max_chunk_chars": "int", "chunk_gap": "int"}
UNSUPPORTED_OPERATIONS = ["design", "ensemble"]
MALFORMED_ERROR = "External TTS metadata is malformed; check that the server exposes a valid Gradio schema."
LIMIT_ERROR = 'External TTS schema exceeds the inspection limits (128 KiB, 64 endpoints, 32 parameters).'
UNAVAILABLE_ERROR = "External TTS capabilities are unavailable; check that the server is running and exposes a Gradio schema."
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()
_VALUE_ERROR = 'External TTS submitted value has an incompatible type for a required parameter.'


def _fingerprint(url, metadata_bytes):
    schema_digest = hashlib.sha256(metadata_bytes).hexdigest() if metadata_bytes is not None else None
    payload = {"url": url, "schema_sha256": schema_digest, "adapter_version": ADAPTER_VERSION}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_report(status, endpoint, parameters, missing, error):
    return {"status": status, "endpoint": endpoint, "adapter": ADAPTER_VERSION, "parameters": list(parameters), "missing": list(missing), "error": error}


def _unknown_report(url, error):
    safe_error = error if error in (MALFORMED_ERROR, UNAVAILABLE_ERROR, LIMIT_ERROR) else UNAVAILABLE_ERROR
    operations = {name: _operation_report("unknown", None, [], [], safe_error) for name in ("custom", "clone")}
    return {"fingerprint": _fingerprint(url, None), "operations": operations, "unsupported_operations": list(UNSUPPORTED_OPERATIONS)}


def make_unknown_capability_report(url, error):
    return _unknown_report(url, error)


def _kind_from_type(type_value):
    if not isinstance(type_value, str) or not type_value.strip():
        raise ValueError(MALFORMED_ERROR)
    kinds = set()
    mapping = {"str": "str", "string": "str", "text": "str", "int": "int", "integer": "int", "float": "number", "number": "number", "double": "number", "bool": "bool", "boolean": "bool", "filedata": "file", "file": "file", "filepath": "file", "dict": "dict"}
    for part in type_value.split("|"):
        token = part.strip().lower()
        if token in ("none", "null", ""):
            continue
        if token not in mapping:
            raise ValueError(MALFORMED_ERROR)
        kinds.add(mapping[token])
    if kinds == {"int", "number"}:
        return "number"
    if len(kinds) != 1:
        raise ValueError(MALFORMED_ERROR)
    return kinds.pop()


def _parameter_details(raw):
    if not isinstance(raw, dict):
        raise ValueError(MALFORMED_ERROR)
    name = raw.get("parameter_name")
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise ValueError(MALFORMED_ERROR)
    python_type = raw.get("python_type")
    if not isinstance(python_type, dict):
        raise ValueError(MALFORMED_ERROR)
    json_type = raw.get("type")
    schema_type = json_type.get("type") if isinstance(json_type, dict) else None
    declared_python_type = python_type.get('type')
    type_value = declared_python_type if declared_python_type in ('file', 'filepath', 'FileData') else schema_type if schema_type in ("string", "integer", "number", "boolean") else declared_python_type
    canonical_names = set(CUSTOM_PARAMETERS + CLONE_PARAMETERS) | set(PARAMETER_ALIASES) | {'model_id'}
    kind = _kind_from_type(type_value) if name in canonical_names else None
    has_default = raw.get("parameter_has_default", False)
    if not isinstance(has_default, bool):
        raise ValueError(MALFORMED_ERROR)
    if has_default and 'parameter_default' not in raw:
        raise ValueError(MALFORMED_ERROR)
    enum_values = json_type.get("enum") if isinstance(json_type, dict) else None
    if enum_values is None:
        enum_values = raw.get('enum')
    if enum_values is None and raw.get('choices') is not None:
        choices = raw['choices']
        if not isinstance(choices, list):
            raise ValueError(MALFORMED_ERROR)
        enum_values = [choice[1] if isinstance(choice, (list, tuple)) and len(choice) == 2 else choice for choice in choices]
    if enum_values is not None:
        if not isinstance(enum_values, list) or not enum_values:
            raise ValueError(MALFORMED_ERROR)
    return {"name": name, "kind": kind, "enum": list(enum_values) if enum_values is not None else None, "has_default": has_default}


def _endpoints(metadata):
    if not isinstance(metadata, dict):
        raise ValueError(MALFORMED_ERROR)
    total = 0
    endpoints = {}
    unnamed = metadata.get("unnamed_endpoints")
    if unnamed is not None:
        if not isinstance(unnamed, dict):
            raise ValueError(MALFORMED_ERROR)
        total += len(unnamed)
        if total > MAX_ENDPOINT_COUNT:
            raise ValueError(LIMIT_ERROR)
    named = metadata.get("named_endpoints")
    if named is None:
        raise ValueError(MALFORMED_ERROR)
    if not isinstance(named, dict):
        raise ValueError(MALFORMED_ERROR)
    for endpoint_name, endpoint_value in named.items():
        total += 1
        if total > MAX_ENDPOINT_COUNT:
            raise ValueError(LIMIT_ERROR)
        if not isinstance(endpoint_name, str) or not endpoint_name.strip():
            raise ValueError(MALFORMED_ERROR)
        normalized = endpoint_name.strip()
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        if normalized not in ALL_ENDPOINT_ALIASES:
            continue
        if not isinstance(endpoint_value, dict):
            raise ValueError(MALFORMED_ERROR)
        raw_parameters = endpoint_value.get("parameters")
        if not isinstance(raw_parameters, list):
            endpoints[normalized] = None
            continue
        if len(raw_parameters) > MAX_PARAMETER_COUNT:
            raise ValueError(LIMIT_ERROR)
        try:
            parameters = [_parameter_details(item) for item in raw_parameters]
        except ValueError:
            endpoints[normalized] = None
            continue
        names = [item["name"] for item in parameters]
        if len(set(names)) != len(names):
            endpoints[normalized] = None
            continue
        if normalized in endpoints and endpoints[normalized] != parameters:
            endpoints[normalized] = None
            continue
        endpoints[normalized] = parameters
    return endpoints


def _analyze_single(operation, endpoint, parameters):
    if parameters is None:
        return _operation_report('unknown', endpoint, [], [], MALFORMED_ERROR), None
    canonical_parameters = CUSTOM_PARAMETERS if operation == "custom" else CLONE_PARAMETERS
    mapping = {}
    extras = []
    model_id_detail = None
    ordered = []
    ambiguous = False
    for parameter in parameters:
        name = parameter["name"]
        if name == "model_id":
            model_id_detail = parameter
            ordered.append((name, "model_id"))
            continue
        canonical = name if name in canonical_parameters else PARAMETER_ALIASES.get(name)
        if canonical not in canonical_parameters:
            extras.append(parameter)
            ordered.append((name, None))
            continue
        if canonical in mapping:
            ambiguous = True
            continue
        mapping[canonical] = parameter
        ordered.append((name, canonical))
    if model_id_detail is not None and "model_size" in mapping:
        ambiguous = True
    present = [item for item in canonical_parameters if item in mapping or (item == "model_size" and model_id_detail is not None)]
    missing = [item for item in canonical_parameters if item not in present]
    missing.extend(item["name"] for item in extras if not item["has_default"])
    if ambiguous:
        return _operation_report("unsupported", endpoint, present, missing, "External TTS endpoint has an ambiguous parameter alias."), None
    for canonical, parameter in mapping.items():
        expected = EXPECTED_KINDS[canonical]
        if canonical == "ref_audio":
            if parameter["kind"] not in ("file", "dict"):
                return _operation_report("unsupported", endpoint, present, missing, "External TTS endpoint declares an incompatible type for a required parameter."), None
        elif parameter["kind"] != expected and not (expected == "int" and parameter["kind"] == "number"):
            return _operation_report("unsupported", endpoint, present, missing, "External TTS endpoint declares an incompatible type for a required parameter."), None
    if model_id_detail is not None and model_id_detail["kind"] != "str":
        return _operation_report("unsupported", endpoint, present, missing, "External TTS endpoint declares an incompatible type for a required parameter."), None
    model_parameter = model_id_detail or mapping.get('model_size')
    if model_parameter is not None and model_parameter['enum'] is not None:
        accepted = ('1.7B', MODEL_IDS[operation]) if model_id_detail is not None else ('1.7B',)
        if not any(value in model_parameter['enum'] for value in accepted):
            return _operation_report('unsupported', endpoint, present, missing, 'This AA2 adapter requires a declared 1.7B model option.'), None
    if missing:
        return _operation_report("unsupported", endpoint, present, missing, "External TTS endpoint is missing required parameters or has required unknown parameters."), None
    internal = {"endpoint": endpoint, "mapping": mapping, "model_id_detail": model_id_detail, "ordered": ordered, "canonical": canonical_parameters}
    return _operation_report("supported", endpoint, present, [], None), internal


def _analyze_operation(operation, endpoints):
    canonical_parameters = CUSTOM_PARAMETERS if operation == "custom" else CLONE_PARAMETERS
    candidates = [alias for alias in ENDPOINT_ALIASES[operation] if alias in endpoints]
    if not candidates:
        return _operation_report("unsupported", None, [], list(canonical_parameters), "External TTS endpoint is not available for this operation."), None
    if len(candidates) == 1:
        return _analyze_single(operation, candidates[0], endpoints[candidates[0]])
    results = [_analyze_single(operation, alias, endpoints[alias]) for alias in candidates]
    supported = [result for result in results if result[0]["status"] == "supported"]
    if len(supported) == 1:
        return supported[0]
    present = list(dict.fromkeys(value for result, _internal in results for value in result['parameters']))
    missing = list(dict.fromkeys(value for result, _internal in results for value in result['missing']))
    details = '; '.join(alias + ': ' + (result['error'] or 'schema supported') for alias, (result, _internal) in zip(candidates, results))
    return _operation_report('unsupported', None, present, missing, 'External TTS server exposes ambiguous known endpoint aliases. ' + details), None


def _analyze_metadata(url, metadata, include_fingerprint=True):
    try:
        encoded = get_metadata_json_bytes(metadata, MAX_METADATA_BYTES, 'External TTS metadata')
    except ValueError as error:
        if str(error) == 'External TTS metadata exceeds its byte limit':
            raise ValueError(LIMIT_ERROR) from error
        raise ValueError(MALFORMED_ERROR) from error
    except (TypeError, RecursionError):
        raise ValueError(MALFORMED_ERROR)
    endpoints = _endpoints(metadata)
    operations = {}
    internals = {}
    for operation in ("custom", "clone"):
        operations[operation], internals[operation] = _analyze_operation(operation, endpoints)
    report = {"fingerprint": _fingerprint(url, encoded) if include_fingerprint else None, "operations": operations, "unsupported_operations": list(UNSUPPORTED_OPERATIONS)}
    return report, internals


def build_capability_report(url, metadata):
    try:
        report, _internals = _analyze_metadata(url, metadata)
    except ValueError as error:
        return _unknown_report(url, str(error))
    if len(json.dumps(report, ensure_ascii=True).encode("utf-8")) > MAX_REPORT_BYTES:
        return _unknown_report(url, LIMIT_ERROR)
    return report


def _validate_value(value, parameter):
    if value is None:
        raise ValueError(_VALUE_ERROR)
    kind = parameter["kind"]
    if kind == "str" and not isinstance(value, str):
        raise ValueError(_VALUE_ERROR)
    if kind == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(_VALUE_ERROR)
    if kind == "bool" and not isinstance(value, bool):
        raise ValueError(_VALUE_ERROR)
    if kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ValueError(_VALUE_ERROR)
    if kind in ('file', 'dict'):
        if not isinstance(value, dict) or not isinstance(value.get('path'), str) or not value['path'] or value['path'].lower().startswith(('http://', 'https://')) or not isinstance(value.get('meta'), dict) or value['meta'].get('_type') != 'gradio.FileData':
            raise ValueError(_VALUE_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(_VALUE_ERROR)
    if parameter["enum"] is not None and value not in parameter["enum"]:
        raise ValueError('External TTS parameter ' + parameter['name'] + ' is not an allowed value for the declared schema.')


def _model_token(value, operation):
    return "1.7B" if value in ("1.7B", MODEL_IDS[operation]) else value


def build_compatible_call(url, metadata, args, kwargs):
    if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
        raise ValueError("External TTS submitted arguments are invalid.")
    api_name = kwargs.get("api_name")
    operation = None
    if api_name is not None:
        if not isinstance(api_name, str):
            raise ValueError("External TTS endpoint is not a known compatible endpoint.")
        for candidate, aliases in ENDPOINT_ALIASES.items():
            if api_name in aliases:
                operation = candidate
                break
        if operation is None:
            raise ValueError("External TTS endpoint is not a known compatible endpoint.")
    try:
        report, internals = _analyze_metadata(url, metadata, include_fingerprint=False)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if operation is None:
        if any(key in kwargs for key in ("speaker", "instruct", "instruct_text")):
            operation = "custom"
        elif any(key in kwargs for key in ("ref_audio", "audio", "ref_text")):
            operation = "clone"
        elif args and isinstance(args[0], dict):
            operation = "clone"
        else:
            supported = [name for name in ("custom", "clone") if report["operations"][name]["status"] == "supported"]
            if len(supported) != 1:
                raise ValueError("External TTS endpoint is not a known compatible endpoint.")
            operation = supported[0]
    operation_report = report["operations"][operation]
    if operation_report["status"] != "supported" or internals[operation] is None:
        raise ValueError(operation_report["error"] or "External TTS operation is not supported by the server schema.")
    internal = internals[operation]
    canonical_parameters = internal["canonical"]
    if len(args) > len(canonical_parameters):
        raise ValueError("External TTS submitted arguments contain an unexpected positional parameter.")
    submitted = {}
    for canonical, value in zip(canonical_parameters, args):
        submitted[canonical] = value
    submitted_model_id = _MISSING
    for key, value in kwargs.items():
        if key == "api_name":
            continue
        if key == "model_id":
            submitted_model_id = value
            continue
        canonical = key if key in canonical_parameters else PARAMETER_ALIASES.get(key)
        if canonical not in canonical_parameters:
            raise ValueError("External TTS submitted parameters contain an unknown parameter.")
        if canonical in submitted:
            raise ValueError("External TTS submitted parameters are ambiguous.")
        submitted[canonical] = value
    values = {}
    for canonical in canonical_parameters:
        if canonical == "model_size":
            continue
        if canonical in submitted:
            values[canonical] = submitted[canonical]
        else:
            raise ValueError('External TTS submitted parameters are missing: ' + canonical)
    size_parameter = internal["mapping"].get("model_size")
    model_id_parameter = internal["model_id_detail"]
    size_value = submitted.get("model_size", _MISSING)
    model_id_value = submitted_model_id
    if size_value is not _MISSING and model_id_value is not _MISSING and _model_token(size_value, operation) != _model_token(model_id_value, operation):
        raise ValueError("External TTS submitted model identifier and model size conflict.")
    if size_parameter is not None:
        if size_value is _MISSING:
            if model_id_value == MODEL_IDS[operation]:
                size_value = "1.7B"
            elif model_id_value is not _MISSING and size_parameter["enum"] is not None and model_id_value in size_parameter["enum"]:
                size_value = model_id_value
            else:
                raise ValueError("External TTS submitted parameters are missing: model_size")
    elif model_id_value is _MISSING:
        if size_value is _MISSING:
            raise ValueError("External TTS submitted parameters are missing: model_size")
        if model_id_parameter["enum"] is not None and size_value in model_id_parameter["enum"]:
            model_id_value = size_value
        elif size_value == "1.7B":
            model_id_value = MODEL_IDS[operation]
        else:
            raise ValueError("External TTS model size is not supported for the server model identifier.")
    values["model_size"] = size_value
    for canonical, value in values.items():
        parameter = internal["mapping"].get(canonical)
        if parameter is not None:
            _validate_value(value, parameter)
    if model_id_parameter is not None and model_id_value is not _MISSING:
        _validate_value(model_id_value, model_id_parameter)
    endpoint = internal["endpoint"]
    call_kwargs = {}
    call_args = []
    has_extras = any(marker is None for _name, marker in internal["ordered"])
    for schema_name, marker in internal["ordered"]:
        if marker is None:
            continue
        forwarded = model_id_value if marker == "model_id" else values[marker]
        call_kwargs[schema_name] = forwarded
        call_args.append(forwarded)
    call_kwargs["api_name"] = endpoint
    if operation == "clone" and not has_extras:
        return tuple(call_args), {"api_name": endpoint}
    return (), call_kwargs

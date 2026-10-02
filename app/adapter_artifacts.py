"""Read-only structural checks, without base-model loading or inference."""
import json
import os
import stat


class AdapterValidationError(ValueError):
    """The artifact cannot be parsed as an adapter."""


class AdapterValidationDependencyError(RuntimeError):
    """The validator itself is unavailable; validity is unknown."""


def validate_adapter_artifacts(path, require_training_meta=False):
    """Require local PEFT config and nonempty, well-formed tensor storage.

    This checks structure, not model compatibility or voice quality. Do not
    fetch missing configs or deserialize a base model during validation.
    """
    try:
        from peft import PeftConfig
        from safetensors import safe_open, SafetensorError
    except ImportError as error:
        raise AdapterValidationDependencyError(
            "Adapter validation requires installed PEFT and safetensors dependencies"
        ) from error

    try:
        required = ["adapter_config.json", "adapter_model.safetensors"]
        if require_training_meta:
            required.append("training_meta.json")
        for filename in required:
            file_path = os.path.join(path, filename)
            if not stat.S_ISREG(os.stat(file_path).st_mode):
                raise ValueError(f"{filename} must be a regular file")
            with open(file_path, "rb"):
                pass
        config_path = os.path.join(path, "adapter_config.json")
        attributes = PeftConfig.from_json_file(config_path)
        if not isinstance(attributes, dict) or not attributes.get("peft_type"):
            raise ValueError("adapter_config.json must name a PEFT type")
        PeftConfig.from_peft_type(**PeftConfig.check_kwargs(**attributes))
        weights = os.path.join(path, "adapter_model.safetensors")
        with safe_open(weights, framework="numpy", device="cpu") as tensors:
            keys = tensors.keys()
            if not keys:
                raise ValueError("adapter_model.safetensors contains no tensors")
            for key in keys:
                tensors.get_slice(key).get_shape()
        if require_training_meta:
            with open(os.path.join(path, "training_meta.json"), encoding="utf-8") as handle:
                if not isinstance(json.load(handle), dict):
                    raise ValueError("training_meta.json must be an object")
    except (OSError, ValueError, TypeError, KeyError, SafetensorError) as error:
        raise AdapterValidationError(f"Invalid adapter artifact at {path}: {error}") from error

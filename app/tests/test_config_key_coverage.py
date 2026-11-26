"""Every config-section key the app reads must be declared on its model.

#2335: review read `generation.review_batch_size`, but `GenerationConfig` did
not declare it, so every Settings save silently dropped the saved value. The
scan is AST-based and name-based: it follows the variables the app binds a
config section to (`generation_config`, `tts_cfg`, an LLM `profile`, ...) and
also chained reads such as `(config.get("generation") or {}).get(key)`. A key
read some other way can still slip through; the documented exceptions below
keep the ones the scan does see explicit and justified.
"""
import ast
from pathlib import Path
import unittest

import config_settings

APP_DIR = Path(__file__).resolve().parent.parent
SKIPPED_DIRECTORIES = {"tests", "__pycache__"}

SECTION_MODEL = {
    "generation": config_settings.GenerationConfig,
    "tts": config_settings.TTSConfig,
    "llm": config_settings.LLMConfig,
    "llm_local": config_settings.LLMConfig,
    "llm_remote": config_settings.LLMConfig,
}

# Variables the app binds a config section to. Limited to names actually used
# for the section: a key read through some other name is out of scope.
NAME_SECTION = {
    "generation": "generation", "generation_config": "generation",
    "generation_cfg": "generation", "gen": "generation", "gen_config": "generation",
    "tts": "tts", "tts_config": "tts", "tts_cfg": "tts", "tts_settings": "tts",
    "llm": "llm", "llm_config": "llm", "llm_cfg": "llm", "llm_profile": "llm",
    "profile": "llm", "llm_local": "llm_local", "llm_remote": "llm_remote",
    "local_llm": "llm", "remote_llm": "llm",
}

# (section, key) -> why it is read but deliberately not a declared setting.
DOCUMENTED_EXCEPTIONS = {
    ("generation", "n_ctx"):
        "llama-server /props reply: props['default_generation_settings'], not app config",
    ("generation", "params"):
        "llama-server /props reply: props['default_generation_settings'], not app config",
    ("llm", "concurrency"):
        "benchmark cache stored beside the profile (BENCHMARK_CACHE_KEYS, llm_bench.py)",
    ("llm", "concurrency_for"):
        "benchmark cache stored beside the profile (BENCHMARK_CACHE_KEYS, llm_bench.py)",
    ("llm", "concurrency_environment"):
        "benchmark cache stored beside the profile (BENCHMARK_CACHE_KEYS, llm_bench.py)",
    ("llm", "provider_extra_body_json"):
        "Setup editor helper field, stripped by AppConfig.restore_exact_provider_body",
    ("llm", "provider_extra_body_raw"):
        "Setup editor helper field, stripped by AppConfig.restore_exact_provider_body",
    ("llm", "max_spend_usd"):
        "experiment-only spend cap (app/experiments/audio_judge_probe.py); not an app setting",
}


def get_receiver_sections(node):
    """Sections named inside a chained receiver such as config.get("generation")."""
    sections = set()
    for item in ast.walk(node):
        if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                and item.func.attr == "get" and item.args
                and isinstance(item.args[0], ast.Constant)
                and isinstance(item.args[0].value, str)
                and item.args[0].value in SECTION_MODEL):
            sections.add(item.args[0].value)
        elif (isinstance(item, ast.Subscript)
                and isinstance(item.slice, ast.Constant)
                and isinstance(item.slice.value, str)
                and item.slice.value in SECTION_MODEL):
            sections.add(item.slice.value)
    return sections


def scan_source(source, filename="<synthetic>"):
    """Return (undeclared reads, exception keys seen) for one Python source."""
    violations, seen_exceptions = [], set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args):
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name):
            sections = {NAME_SECTION[receiver.id]} if receiver.id in NAME_SECTION else set()
        else:
            sections = get_receiver_sections(receiver)
        for section in sorted(sections):
            if key.value in SECTION_MODEL[section].model_fields:
                continue
            if (section, key.value) in DOCUMENTED_EXCEPTIONS:
                seen_exceptions.add((section, key.value))
                continue
            violations.append(f"{filename}:{node.lineno}: undeclared {section}.{key.value}")
    return violations, seen_exceptions


def scan_app():
    """Scan every production module under app/ (tests and caches excluded)."""
    violations, seen_exceptions = [], set()
    for path in sorted(APP_DIR.rglob("*.py")):
        parts = path.relative_to(APP_DIR).parts
        if any(part in SKIPPED_DIRECTORIES for part in parts):
            continue
        found, seen = scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(APP_DIR)))
        violations += found
        seen_exceptions |= seen
    return violations, seen_exceptions


class ConfigReadCoverageTests(unittest.TestCase):
    def test_scan_detects_an_undeclared_generation_key(self):
        source = (
            "def chunk(generation_config):\n"
            "    return generation_config.get('not_a_declared_setting')\n"
        )
        violations, _ = scan_source(source, "synthetic.py")
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"], violations)

    def test_every_read_config_key_is_declared(self):
        violations, seen_exceptions = scan_app()
        self.assertEqual([], violations, "Read settings that no config model declares:\n"
                         + "\n".join(violations))
        stale = sorted(set(DOCUMENTED_EXCEPTIONS) - seen_exceptions)
        self.assertEqual([], stale, "Documented exceptions no longer read: " + repr(stale))

"""Every config-section key the app reads must be declared on its model.

#2335: review read `generation.review_batch_size`, but `GenerationConfig` did
not declare it, so every Settings save silently dropped the saved value.

The scan is AST and name based. It reports these reads as a key on a section:

* `<name>.get("key")` and `<name>.get(key="key")` where `<name>` is one of the
  variables the app binds a config section to (`generation_config`, `tts_cfg`,
  an LLM `profile`, ...).
* `<name>["key"]` for those names, except `profile`: it is also the runtime
  wrapper from `llm_provider` carrying `config`/`is_remote`, so it is followed
  only through `.get`. (`profile["config"]` and `profile["is_remote"]` are not
  config keys.)
* A chained section access in the receiver:
  `(config.get("generation") or {}).get("key")`, `config["generation"]["key"]`,
  `config.get("generation")["key"]`.
* A plain alias: `g = config["generation"]` (or `config.get("generation")`),
  after which `g.get("key")` and `g["key"]` are generation reads.

It does NOT follow a section returned by a function call
(`load_section(config).get("key")`), a name not in `NAME_SECTION` and not a
plain alias, a computed key, or any read that is not a `.get`/subscript on one
of those receivers. Because the rule is name based, a tracked name is assumed
to be its section even where a local variable of that name is something else;
the scan cannot prove otherwise for a function parameter. The documented
exceptions keep the reads the scan does see but that are deliberately not
declared settings.
"""
import ast
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import config_settings
from tests.test_support import app_python_files

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
# for the section: a key read through some other name is out of scope unless it
# is bound by a plain alias (below). A name can be overloaded (`profile` is also
# the runtime wrapper); that is the cost of a name-based rule.
NAME_SECTION = {
    "generation": "generation", "generation_config": "generation",
    "generation_cfg": "generation", "gen": "generation", "gen_config": "generation",
    "tts": "tts", "tts_config": "tts", "tts_cfg": "tts", "tts_settings": "tts",
    "llm": "llm", "llm_config": "llm", "llm_cfg": "llm", "llm_profile": "llm",
    "profile": "llm", "llm_local": "llm_local", "llm_remote": "llm_remote",
    "local_llm": "llm", "remote_llm": "llm",
}

# `profile` is also a runtime wrapper (`profile["config"]`, `profile["is_remote"]`)
# and a listener dict in one experiment (`profile["leads"]`), so a subscript on
# it is not a config key read. Its `.get` reads are still followed.
SUBSCRIPT_NAME_SECTION = {
    name: section for name, section in NAME_SECTION.items() if name != "profile"}

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
        "experiment-only spend cap (audio_judge_probe.py); a Settings save still "
        "drops it, but declaring it would add an app setting - out of scope for #2335",
}


def _constant_string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _section_string(value):
    return value if value in SECTION_MODEL else None


def _get_key(node):
    """The literal key of a `.get("key")` / `.get(key="key")` call, else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"):
        return None
    if node.args:
        return _constant_string(node.args[0])
    for keyword in node.keywords:
        if keyword.arg == "key":
            return _constant_string(keyword.value)
    return None


def get_receiver_sections(node, aliases, names=NAME_SECTION):
    """Sections the receiver expression denotes, from its own shape only.

    Deliberately does not descend into a call's arguments:
    `use(config.get("generation")).get("x")` reads `x` from whatever `use`
    returned, not from the generation section.
    """
    if isinstance(node, ast.Name):
        if node.id in names:
            return {names[node.id]}
        return {aliases[node.id]} if node.id in aliases else set()
    key = _get_key(node)
    if key is not None:
        section = _section_string(key)
        return {section} if section else set()
    if isinstance(node, ast.Subscript):
        section = _section_string(_constant_string(node.slice))
        if section:
            return {section}
        return get_receiver_sections(node.value, aliases, names)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.Or, ast.And)):
        sections = set()
        for value in node.values:
            sections |= get_receiver_sections(value, aliases, names)
        return sections
    return set()


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _single_section(node):
    """The one section a value expression denotes, if it is exactly one."""
    if isinstance(node, ast.Subscript):
        return _section_string(_constant_string(node.slice))
    key = _get_key(node)
    if key is not None:
        return _section_string(key)
    if isinstance(node, ast.Name) and node.id in NAME_SECTION:
        return NAME_SECTION[node.id]
    return None


def collect_aliases(tree):
    """Names bound to one section by a plain assignment, module wide.

    `g = config["generation"]` makes a later `g.get(...)`/`g[...]` a generation
    read. A name bound any other way anywhere in the module (reassigned to a
    non-section, a loop/with/except target, an augmented assignment) is left
    unaliased: ambiguity could only produce a false positive.
    """
    bindings = {}

    def bind(names, section):
        for name in names:
            bindings.setdefault(name, set()).update({section} if section else {None})

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            section = _single_section(node.value)
            for target in node.targets:
                bind(_target_names(target), section)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind(_target_names(node.target), _single_section(node.value))
        elif isinstance(node, ast.NamedExpr):
            bind(_target_names(node.target), _single_section(node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bind(_target_names(node.target), None)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                bind(_target_names(node.optional_vars), None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind([node.name], None)
        elif isinstance(node, ast.AugAssign):
            bind(_target_names(node.target), None)
    return {name: next(iter(sections)) for name, sections in bindings.items()
            if len(sections) == 1 and None not in sections}


def read_sections(node, aliases):
    """(section, key) pairs this node reads as config keys."""
    key = _get_key(node)
    if key is not None:
        return {(section, key) for section in
                get_receiver_sections(node.func.value, aliases)}
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        key = _constant_string(node.slice)
        if key is None:
            return set()
        return {(section, key) for section in
                get_receiver_sections(node.value, aliases, SUBSCRIPT_NAME_SECTION)}
    return set()


def scan_source(source, filename="<synthetic>"):
    """Return (undeclared reads, exception keys seen) for one Python source."""
    tree = ast.parse(source, filename=filename)
    aliases = collect_aliases(tree)
    violations, seen_exceptions = set(), set()
    for node in ast.walk(tree):
        for section, key in read_sections(node, aliases):
            if key in SECTION_MODEL[section].model_fields:
                continue
            if (section, key) in DOCUMENTED_EXCEPTIONS:
                seen_exceptions.add((section, key))
                continue
            violations.add(f"{filename}:{node.lineno}: undeclared {section}.{key}")
    return sorted(violations), seen_exceptions


def scan_app():
    """Scan every production module under app/ (tests and the venv excluded)."""
    violations, seen_exceptions = [], set()
    for path in app_python_files(APP_DIR):
        parts = path.relative_to(APP_DIR).parts
        if any(part in SKIPPED_DIRECTORIES for part in parts):
            continue
        found, seen = scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(APP_DIR)))
        violations += found
        seen_exceptions |= seen
    return violations, seen_exceptions


class ConfigReadCoverageTests(unittest.TestCase):
    def _violations(self, source):
        return scan_source(source, "synthetic.py")[0]

    def test_scan_detects_an_undeclared_generation_key(self):
        source = (
            "def chunk(generation_config):\n"
            "    return generation_config.get('not_a_declared_setting')\n"
        )
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_scan_detects_a_subscript_read(self):
        source = (
            "def chunk(generation_config):\n"
            "    return generation_config['not_a_declared_setting']\n"
        )
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_scan_detects_a_get_keyword_read(self):
        source = (
            "def chunk(generation_config):\n"
            "    return generation_config.get(key='not_a_declared_setting')\n"
        )
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_scan_follows_a_chained_get_receiver(self):
        source = (
            "def chunk(config):\n"
            "    return (config.get('generation') or {}).get('not_a_declared_setting')\n"
        )
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_scan_follows_a_chained_subscript_receiver(self):
        source = (
            "def chunk(config):\n"
            "    return config['generation']['not_a_declared_setting']\n"
        )
        self.assertEqual(
            ["synthetic.py:2: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_scan_follows_a_plain_alias(self):
        source = (
            "def chunk(config):\n"
            "    g = config['generation']\n"
            "    return g.get('not_a_declared_setting'), g['not_a_declared_setting']\n"
        )
        self.assertEqual(
            ["synthetic.py:3: undeclared generation.not_a_declared_setting"],
            self._violations(source))

    def test_a_declared_key_is_not_reported(self):
        source = (
            "def chunk(generation_config, config):\n"
            "    return (generation_config.get('chunk_size'), generation_config['chunk_size'],\n"
            "            (config['generation'])['chunk_size'])\n"
        )
        self.assertEqual([], self._violations(source))

    def test_receiver_call_arguments_are_not_followed(self):
        # `use(...)` returns something, not the generation section. Only the
        # receiver's own shape is followed, not the arguments of its calls.
        source = (
            "def chunk(config, use):\n"
            "    return use(config.get('generation')).get('not_a_declared_setting')\n"
        )
        self.assertEqual([], self._violations(source))

    def test_a_reassigned_alias_is_not_assumed_to_be_a_section(self):
        source = (
            "def chunk(config):\n"
            "    g = config['generation']\n"
            "    g = {'unrelated': 1}\n"
            "    return g.get('not_a_declared_setting')\n"
        )
        self.assertEqual([], self._violations(source))

    def test_profile_subscript_is_not_a_config_key(self):
        source = (
            "def classify(profile):\n"
            "    return profile['leads'], profile['is_remote']\n"
        )
        self.assertEqual([], self._violations(source))

    def test_a_name_off_the_list_is_not_followed(self):
        source = (
            "def chunk(other):\n"
            "    return other.get('not_a_declared_setting')\n"
        )
        self.assertEqual([], self._violations(source))

    def test_a_computed_key_is_not_followed(self):
        source = (
            "def chunk(generation_config, name):\n"
            "    return generation_config.get(name)\n"
        )
        self.assertEqual([], self._violations(source))

    def test_a_tracked_name_is_assumed_to_be_its_section(self):
        # Kept, not narrowed. A function parameter named `llm` can only be
        # identified by name, so the scan treats it as the llm section even if
        # the caller passed something else. This is the documented cost of the
        # name-based rule; narrowing it would lose every config section passed
        # as a parameter.
        source = (
            "def chunk(llm):\n"
            "    return llm.get('not_a_declared_setting')\n"
        )
        self.assertEqual(["synthetic.py:2: undeclared llm.not_a_declared_setting"],
                         self._violations(source))

    def test_every_read_config_key_is_declared(self):
        violations, seen_exceptions = scan_app()
        self.assertEqual([], violations, "Read settings that no config model declares:\n"
                         + "\n".join(violations))
        stale = sorted(set(DOCUMENTED_EXCEPTIONS) - seen_exceptions)
        self.assertEqual([], stale, "Documented exceptions no longer read: " + repr(stale))


class ScanScopeTests(unittest.TestCase):
    def test_scan_reads_our_modules_and_not_a_virtualenv_beside_them(self):
        # The live checkout keeps app/env inside the scanned directory, with a
        # non-UTF-8 joblib file. A bare rglob reads it and raises; the helper
        # does not. A synthetic tree keeps this test independent of a venv.
        root = Path(tempfile.mkdtemp())
        (root / "tts.py").write_text(
            "def chunk(tts_config):\n    return tts_config.get('not_a_declared_setting')\n",
            encoding="utf-8")
        venv = root / "env"
        (venv / "lib" / "python3.10" / "site-packages" / "joblib").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        (venv / "lib" / "python3.10" / "site-packages" / "joblib" / "binary.py"
         ).write_bytes(b"\xff\xfe not utf-8")
        with patch.object(sys.modules[__name__], "APP_DIR", root):
            violations, _ = scan_app()
        self.assertEqual(["tts.py:2: undeclared tts.not_a_declared_setting"],
                         violations)

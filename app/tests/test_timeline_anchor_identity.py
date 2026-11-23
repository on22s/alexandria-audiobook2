"""#2160: stable timeline anchors across routes, batches, scope and drift."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

import tts
import voice_drift
from project import ProjectManager
from project_scope_voices import get_scope_voice_plan
from routers import editor, voices as voices_router
from book_state_transaction import get_book_operation_token
from chunk_status_journal import get_chunk_status_journal_path
from timeline_anchors import build_uid_index, full_uid_index_context, get_anchor_uid, resolve_timeline_anchors
from tests import test_voice_followups_733 as v733
from tests import test_drift_voice_states as drift_tests


def style_config(point):
    return {"A": {"type": "custom", "voice": "Ryan", "character_style": "young",
                  "style_timeline": [point]}}


def make_rows(count=4):
    return [{"id": index, "uid": f"u{index}", "speaker": "A", "text": f"line {index}",
             "instruct": "soft"} for index in range(count)]


def style_at(voices, row, index):
    return tts.get_chunk_voice_inputs(voices, row, index)["A"]["character_style"]


def collect_ready_card(snapshot, ready, speaker="A"):
    from tests.test_state_voices_js import VoiceStatesJsTests
    return VoiceStatesJsTests().run_js(r'''
const snapshot=JSON.parse(payload.snapshot);
const voice=snapshot.voices.find(row=>row.name===payload.speaker);
const fields={'.voice-type:checked':{value:'custom'},'.voice-select':{value:voice.config.voice},
    '.character-style':{value:voice.config.character_style},'.voice-ready':{checked:payload.ready}};
const card={dataset:{voice:payload.speaker},querySelector:selector=>fields[selector]||null};
Object.assign(context,{document:{querySelectorAll:()=>[card]},getLoraModelsById:()=>new Map()});
context.window._voicesByName={[payload.speaker]:voice};
for (const [first,last] of [
    ['function getVoiceCardMetadata(', 'function getVoiceCardDescription('],
    ['function collectVoiceConfig(', 'function onVoiceReadyChange(']
]) {
    const start=source.indexOf(first);
    vm.runInContext(source.slice(start,source.indexOf(last,start)),context);
}
console.log(JSON.stringify({voices:context.collectVoiceConfig(),revision:snapshot.revision,book_token:snapshot.book_token}));
''', {"snapshot": snapshot.text, "ready": ready, "speaker": speaker})


def send_state_controls(response, token, action="apply", locale="en"):
    """Render with the real locale catalog, then execute the rendered handlers."""
    from tests.test_state_voices_js import VoiceStatesJsTests
    vm = VoiceStatesJsTests()
    markup = vm.run_js(r'''
const path=require('path');
const assets=path.dirname(path.dirname(process.argv[1]));
context.document={getElementById:()=>null, querySelectorAll:()=>[]};
context.fetch=async url=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(
    path.join(assets,'i18n',path.basename(url)),'utf8'))});
vm.runInContext(fs.readFileSync(path.join(assets,'js','app-i18n.js'),'utf8'),context);
await context.applyUiLocale(payload.locale);
const data=JSON.parse(payload.response);
console.log(JSON.stringify(context.renderVoiceStateRows(data,data.speaker)));
''', {"response": response.text, "locale": locale})

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.handlers = [], {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "div" and "data-from-index" in attrs:
                self.rows.append({"dataset": {key: attrs[name] for key, name in
                    (("fromIndex", "data-from-index"), ("uid", "data-uid"), ("age", "data-age"))}})
            if tag == "select":
                self.rows[-1]["select"] = {"disabled": "disabled" in attrs, "dataset": {},
                                           "value": "main", "label": attrs["aria-label"]}
            if tag == "option" and "selected" in attrs:
                self.rows[-1]["select"]["value"] = attrs["value"]
            if tag == "button" and attrs.get("onclick") in (
                    "chooseMainVoiceStates(this)", "applyVoiceStates(this)", "clearVoiceStates(this)"):
                self.handlers[attrs["onclick"].split("(")[0]] = attrs["onclick"]

    controls = Controls()
    controls.feed(markup)
    if action == "main":
        handlers = [controls.handlers["chooseMainVoiceStates"], controls.handlers["applyVoiceStates"]]
    elif action == "clear":
        handlers = [controls.handlers["clearVoiceStates"]]
    else:
        handlers = [controls.handlers["applyVoiceStates"]]
    writes = vm.run_js(r'''
const data=JSON.parse(payload.response);
context.window._voiceStateSuggestions={[data.speaker]:data};
context._voiceSaveSnapshot={book_token:payload.token};context.showConfirm=async()=>true;
const rows=payload.rows.map(row=>({dataset:row.dataset,querySelector:()=>row.select}));
const card={dataset:{voice:data.speaker},querySelectorAll:selector=>
    selector==='.voice-state-row'?rows:payload.rows.map(row=>row.select)};
const button={closest:()=>card};
for (const handler of payload.handlers) {
    await vm.runInContext('(function(){return '+handler+'})',context).call(button);
}
console.log(JSON.stringify(calls.filter(call=>call[0]==='POST'||call[0]==='DEL')));
''', {"response": response.text, "rows": controls.rows, "token": token, "handlers": handlers})
    return controls.rows, writes


class ResolverTests(unittest.TestCase):
    def test_sparse_batch_uses_absolute_book_index(self):
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        rows = [{"index": 2, "uid": "u2", "speaker": "A", "text": "two"},
                {"index": 4, "uid": "u4", "speaker": "A", "text": "four"}]
        full = [{"uid": f"u{index}"} for index in range(6)]
        full_index = build_uid_index(full)
        self.assertEqual((2, 4), (full_index["u2"], full_index["u4"]))
        with full_uid_index_context(build_uid_index(full)):
            resolved = resolve_timeline_anchors(voices, rows, sparse=True)
        self.assertEqual("young", style_at(resolved, rows[0], 2))
        self.assertEqual("aged", style_at(resolved, rows[1], 4))
        self.assertEqual("u4", get_anchor_uid(full, 4))

    def test_duplicate_uid_is_ambiguous_and_falls_back_to_recorded_index(self):
        rows = make_rows()
        rows[2]["uid"] = "u0"
        voices = style_config({"from_index": 2, "from_uid": "u0", "character_style": "aged"})
        self.assertNotIn("u0", build_uid_index(rows))
        resolved = resolve_timeline_anchors(voices, rows)
        self.assertEqual("aged", style_at(resolved, rows[2], 2))
        self.assertEqual("young", style_at(resolved, rows[1], 1))

    def test_missing_and_regenerated_uid_keep_legacy_index(self):
        voices = style_config({"from_index": 2, "from_uid": "old", "character_style": "aged"})
        full = resolve_timeline_anchors(voices, make_rows())
        sparse = resolve_timeline_anchors(voices, [{"index": 3, "uid": "fresh", "speaker": "A"}], sparse=True)
        for resolved in (full, sparse):
            self.assertEqual(voices["A"]["style_timeline"], resolved["A"]["style_timeline"])
            self.assertEqual("aged", style_at(resolved, make_rows()[3], 3))

    def test_legacy_null_point_does_not_raise_for_unrelated_speaker(self):
        voices = {"A": {"style_timeline": [None,
                        {"from_index": None, "character_style": "aged"}]},
                  "B": {"type": "custom", "voice": "Ryan"}}
        resolved = resolve_timeline_anchors(voices, make_rows())
        self.assertEqual(voices, resolved)
        self.assertEqual("Ryan", tts.get_chunk_voice_inputs(resolved, {"speaker": "B"}, 0)["B"]["voice"])
        with self.assertRaises(ValueError):
            tts.get_chunk_voice_inputs(resolved, {"speaker": "A"}, 0)


class DispatchTests(unittest.TestCase):
    def test_local_and_external_batch_share_absolute_anchor(self):
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        chunks = [{"index": 2, "uid": "u2", "speaker": "A", "text": "two", "instruct": "soft"},
                  {"index": 4, "uid": "u4", "speaker": "A", "text": "four", "instruct": "soft"}]
        from tests.test_batch_style_timeline import get_engine
        seen = []
        def local(rows, config, output, seed):
            seen.append(("local", [p["from_index"] for p in config["A"]["style_timeline"]]))
            return {"completed": [row["index"] for row in rows], "failed": []}
        def external(rows, config, output, kind, seed):
            seen.append(("external", [p["from_index"] for p in config["A"]["style_timeline"]]))
            return {"completed": [row["index"] for row in rows], "failed": []}
        full = [{"uid": f"u{index}"} for index in range(6)]
        with tempfile.TemporaryDirectory() as directory, \
                full_uid_index_context(build_uid_index(full)):
            engine = get_engine()
            with patch.object(engine, "get_voice_backend", return_value="local"), \
                 patch.object(engine, "_local_batch_custom", local):
                engine.generate_batch(chunks, voices, directory)
            external_engine = get_engine()
            with patch.object(external_engine, "get_voice_backend", return_value="external"), \
                 patch.object(external_engine, "_external_batch", external):
                external_engine.generate_batch(chunks, voices, directory)
        self.assertEqual([("local", [4]), ("external", [4])], seen)

    def test_direct_batch_uses_recorded_position_for_duplicate_uid(self):
        voices = style_config({"from_index": 4, "from_uid": "shared", "character_style": "aged"})
        chunks = [{"index": index, "uid": "shared", "speaker": "A", "text": f"line {index}"}
                  for index in (2, 4)]
        from tests.test_batch_style_timeline import get_engine
        with tempfile.TemporaryDirectory() as directory:
            for backend, method in (("local", "_local_batch_custom"), ("external", "_external_batch")):
                for order in (chunks, chunks[::-1]):
                    with self.subTest(backend=backend, first=order[0]["index"]):
                        engine = get_engine()
                        seen = {}
                        def render(rows, config, *_args):
                            seen.update({row["index"]: style_at(config, row, row["index"]) for row in rows})
                            return {"completed": [row["index"] for row in rows], "failed": []}
                        with patch.object(engine, "get_voice_backend", return_value=backend), \
                             patch.object(engine, method, side_effect=render):
                            result = engine.generate_batch(order, voices, directory)
                        self.assertEqual([], result["failed"])
                        self.assertEqual({2: "young", 4: "aged"}, seen)


class RouteTests(unittest.TestCase):
    def fixture(self, directory, stack, config=None):
        root, manager, client, path, stored, rows = v733.VoiceTimelineAudioTests.fixture(
            self, directory, stack)
        if config is not None:
            path.write_text(json.dumps(config))
        return root, manager, client, path, stored, rows

    def test_ready_collector_keeps_positional_and_orphan_styles_through_editor_insert(self):
        for uid in (None, "deleted-anchor"):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root, manager, client, path, config, rows = self.fixture(directory, stack)
                client.app.include_router(editor.router)
                stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
                stack.enter_context(patch.object(editor, "project_manager", manager))
                stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
                point = {"from_index": "002", "character_style": "aged", "extension": {"audit": 7}}
                if uid is not None:
                    point["from_uid"] = uid
                config["A"].update(style_timeline=[point], alias_of=None, ready=False,
                                   seed="-1", character_style="")
                path.write_text(json.dumps(config, indent=4) + "\n")
                chunk_path = Path(manager.chunks_path)
                original = path.read_bytes(), chunk_path.read_bytes()
                snapshot = client.get("/api/voice_config/snapshot")
                self.assertEqual(200, snapshot.status_code, snapshot.text)
                untouched = collect_ready_card(snapshot, False)
                self.assertIn("delete_key", untouched["voices"]["A"]["style_timeline"][0])
                saved = client.post("/api/voice_config/save", json=untouched)
                self.assertEqual(200, saved.status_code, saved.text)
                self.assertEqual(original, (path.read_bytes(), chunk_path.read_bytes()))
                ready = client.post("/api/voice_config/save", json=collect_ready_card(snapshot, True))
                self.assertEqual(200, ready.status_code, ready.text)
                config["A"]["ready"] = True
                self.assertEqual(config, json.loads(path.read_bytes()))
                self.assertEqual(original[1], chunk_path.read_bytes())
                voice_bytes = path.read_bytes()
                response = client.post("/api/chunks/0/insert", params={
                    "expected_book_token": get_book_operation_token(str(root)), "expected_uid": rows[0]["uid"]})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(voice_bytes, path.read_bytes())
                current = manager.get_saved_chunks()
                self.assertEqual(rows[2]["uid"], current[3]["uid"])
                active = resolve_timeline_anchors(json.loads(voice_bytes), current)
                self.assertEqual("", tts.active_character_style(active["A"], 1))
                self.assertEqual("aged", tts.active_character_style(active["A"], 2))

    def test_browser_replay_preserves_unsafe_indices_and_nested_metadata_bytes(self):
        from tests.test_state_review_fixes import two_state_script

        for has_chunks in (False, True):
            with self.subTest(has_chunks=has_chunks), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
                script = two_state_script()
                Path(manager.script_path).write_text(json.dumps(script))
                chunks = Path(manager.chunks_path)
                if has_chunks:
                    manager.save_chunks([{**row, "uid": f"u{index}", "id": index}
                                         for index, row in enumerate(script)])
                else:
                    chunks.unlink()
                precise = 9007199254740993
                extra = {"nested": [{"large": precise, "negative": -precise, "ratio": 1.0}]}
                version_points = [
                    {"from_index": "001", "version_id": None, "extension": extra},
                    {"from_index": precise, "version_id": None, "extension": extra}]
                style = {"from_index": precise, "character_style": "aged", "extension": extra}
                path.write_text(json.dumps({"MIRA": {"type": "custom", "voice": "Ryan",
                    "character_style": "", "alias_of": None, "seed": "-1", "ready": False,
                    "style_timeline": [style], "version_timeline": version_points}}, indent=4) + "\n")
                stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
                before = path.read_bytes(), chunks.read_bytes() if has_chunks else None
                snapshot = client.get("/api/voice_config/snapshot")
                self.assertEqual(200, snapshot.status_code, snapshot.text)
                packet = collect_ready_card(snapshot, False, speaker="MIRA")
                echoed = client.post("/api/voice_config/save", json=packet)
                self.assertEqual(200, echoed.status_code, echoed.text)
                state = client.get("/api/voices/MIRA/state_timeline")
                self.assertEqual(200, state.status_code, state.text)
                _controls, writes = send_state_controls(state, snapshot.json()["book_token"])
                (method, url, body), = writes
                self.assertEqual(str(precise), body["points"][1]["from_index"])
                self.assertEqual(str(-precise), body["points"][0]["extension"]["nested"][0]["negative"])
                echoed = client.request(method, url, json=body)
                self.assertEqual(200, echoed.status_code, echoed.text)
                self.assertEqual(version_points, echoed.json()["version_timeline"])
                # A reference cannot authorize rewriting one unsafe metadata value.
                body["points"][0]["extension"]["nested"][0]["large"] = str(precise - 1)
                refused = client.request(method, url, json=body)
                self.assertEqual(409, refused.status_code, refused.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes() if chunks.exists() else None))

    def test_apply_uses_displayed_default_when_the_saved_effective_version_differs(self):
        from tests.test_state_review_fixes import two_state_script

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _old, _rows = self.fixture(directory, stack)
            script = two_state_script()
            Path(manager.script_path).write_text(json.dumps(script))
            rows = [{**row, "id": i, "uid": f"u{i}"} for i, row in enumerate(script)]
            manager.save_chunks(rows)
            config = {"MIRA": {"type": "custom", "voice": "Ryan", "versions": {
                "manual": {"type": "custom", "voice": "Serena", "age_group": "child"},
                "adult": {"type": "custom", "voice": "Aiden", "age_group": "adult"}},
                "version_timeline": [{"from_index": 2, "from_uid": "u2", "version_id": "manual"}]}}
            path.write_text(json.dumps(config))
            stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            snapshot = client.get("/api/voice_config/snapshot")
            state = client.get("/api/voices/MIRA/state_timeline")
            self.assertEqual(200, state.status_code, state.text)
            earlier, later = [row["from_index"] for row in state.json()["states"]]
            previous = resolve_timeline_anchors(config, rows)
            self.assertEqual("Serena", tts.voice_config_for_chunk(previous, "MIRA", later)["MIRA"]["voice"])
            controls, writes = send_state_controls(state, snapshot.json()["book_token"])
            self.assertEqual(["main", "version:adult"], [row["select"]["value"] for row in controls])
            (method, url, body), = writes
            self.assertEqual([{"from_index": later, "from_uid": f"u{later}", "version_id": "adult"}],
                             body["points"])
            response = client.request(method, url, json=body)
            self.assertEqual(200, response.status_code, response.text)
            current = resolve_timeline_anchors(json.loads(path.read_bytes()), manager.get_saved_chunks())
            for index, voice in ((earlier, "Ryan"), (2, "Ryan"), (later, "Aiden")):
                self.assertEqual(voice, tts.voice_config_for_chunk(current, "MIRA", index)["MIRA"]["voice"])

    def test_localized_main_handlers_cover_a_partially_mapped_book(self):
        from tests.test_state_review_fixes import two_state_script
        from speaker_traits import get_persona_state_targets

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _old, _rows = self.fixture(directory, stack)
            script = two_state_script()
            Path(manager.script_path).write_text(json.dumps(script))
            rows = [{**row, "uid": f"u{index}"} for index, row in enumerate(script)]
            adult = get_persona_state_targets(script)["MIRA"][1]["from_entry"]
            previous = max(index for index in range(adult) if rows[index]["speaker"] == "MIRA")
            rows[previous]["text"] += " " + rows.pop(adult)["text"]
            for index, row in enumerate(rows):
                row["id"] = index
            manager.save_chunks(rows)
            dormant = {"from_index": len(rows) + 5, "version_id": "manual", "from_uid": "missing"}
            config = {"MIRA": {"type": "custom", "voice": "Ryan", "versions": {
                "manual": {"type": "custom", "voice": "Serena", "age_group": "adult"}},
                "version_timeline": [{"from_index": 2, "from_uid": "u2", "version_id": "manual"}, dormant]}}
            stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            for locale in ("en", "es-419", "ja", "zh-CN"):
                with self.subTest(locale=locale):
                    path.write_text(json.dumps(config))
                    snapshot = client.get("/api/voice_config/snapshot")
                    state = client.get("/api/voices/MIRA/state_timeline")
                    self.assertEqual(200, state.status_code, state.text)
                    self.assertEqual([1, None], [item["from_index"] for item in state.json()["states"]])
                    active = resolve_timeline_anchors(config, rows)
                    self.assertEqual("Serena", tts.voice_config_for_chunk(active, "MIRA", 2)["MIRA"]["voice"])
                    controls, writes = send_state_controls(state, snapshot.json()["book_token"], "main", locale)
                    self.assertEqual([False, True], [row["select"]["disabled"] for row in controls])
                    if locale != "en":
                        self.assertNotIn("from line", controls[0]["select"]["label"])
                    (method, url, body), = writes
                    self.assertEqual([dormant], [
                        {key: value for key, value in point.items() if key != "display_index"}
                        for point in body["points"]])
                    response = client.request(method, url, json=body)
                    self.assertEqual(200, response.status_code, response.text)
                    saved = json.loads(path.read_bytes())
                    self.assertEqual([dormant], saved["MIRA"]["version_timeline"])
                    active = resolve_timeline_anchors(saved, manager.get_saved_chunks())
                    self.assertTrue(all(tts.voice_config_for_chunk(active, "MIRA", index)["MIRA"]["voice"] == "Ryan"
                                        for index in range(1, len(rows))))

    def test_state_read_and_replay_share_admission_for_siblings_and_null_speakers(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            config["B"]["style_timeline"] = {"2": {"character_style": "recover this object"}}
            path.write_text(json.dumps(config, indent=4))
            before = path.read_bytes(), chunks.read_bytes()
            get_url, post_url = "/api/voices/A/state_timeline", "/api/voices/A/version_timeline"
            view = client.get(get_url)
            replay = client.post(post_url, json={"points": []})
            self.assertEqual((409, 409), (view.status_code, replay.status_code))
            self.assertEqual(view.json()["detail"], replay.json()["detail"])
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

            config["A"] = None
            del config["B"]["style_timeline"]
            path.write_text(json.dumps(config, indent=4) + "\n")
            before = path.read_bytes(), chunks.read_bytes()
            view = client.get(get_url)
            self.assertEqual(200, view.status_code, view.text)
            body = {"points": view.json()["applied"],
                    "expected_timeline_revision": view.json()["timeline_revision"]}
            replay = client.post(post_url, json=body)
            self.assertEqual(200, replay.status_code, replay.text)
            self.assertEqual([], replay.json()["version_timeline"])
            rejected = client.post(post_url, json={**body, "points": [{"from_index": 2, "version_id": "different"}]})
            self.assertEqual(422, rejected.status_code, rejected.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_and_dedicated_saves_match_numeric_spelling_before_semantics(self):
        for kind, route in (("style_timeline", "guarded"), ("version_timeline", "guarded"),
                            ("version_timeline", "dedicated")):
            with self.subTest(kind=kind, route=route), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, _manager, client, path, config, _rows = self.fixture(directory, stack)
                field, value = ("character_style", "aged") if kind == "style_timeline" else ("version_id", "different")
                ascii_point = {"from_index": "002", field: value, "audit": "same"}
                full_width = {**ascii_point, "from_index": "２"}
                config["A"][kind] = [ascii_point, full_width]
                path.write_text(json.dumps(config, indent=4) + "\n")
                snapshot = client.get("/api/voice_config/snapshot").json()
                before = path.read_bytes()
                if route == "guarded":
                    body = {"voices": snapshot["config"], "revision": snapshot["revision"],
                            "book_token": snapshot["book_token"]}
                    url = "/api/voice_config/save"
                    points = body["voices"]["A"][kind]
                else:
                    points = client.get("/api/voices/A/state_timeline").json()["applied"]
                    body = {"points": points, "book_token": snapshot["book_token"]}
                    url = "/api/voices/A/version_timeline"
                echoed = client.post(url, json=body)
                self.assertEqual(200, echoed.status_code, echoed.text)
                self.assertEqual(before, path.read_bytes())
                points.pop(0)
                changed = client.post(url, json=body)
                self.assertEqual(200, changed.status_code, changed.text)
                self.assertEqual([full_width], json.loads(path.read_bytes())["A"][kind])

    def test_clear_handler_cannot_remove_a_concurrent_timeline_save(self):
        from tests.test_state_review_fixes import two_state_script

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _old, _rows = self.fixture(directory, stack)
            script = two_state_script()
            Path(manager.script_path).write_text(json.dumps(script))
            manager.save_chunks([{**row, "id": i, "uid": f"u{i}"} for i, row in enumerate(script)])
            config = {"MIRA": {"type": "custom", "voice": "Ryan", "versions": {
                "manual": {"type": "custom", "voice": "Serena"}},
                "version_timeline": [{"from_index": 2, "from_uid": "u2", "version_id": "manual"}]}}
            path.write_text(json.dumps(config))
            stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            state_url = "/api/voices/MIRA/state_timeline"
            version_url = "/api/voices/MIRA/version_timeline"
            state = client.get(state_url)
            self.assertEqual(200, state.status_code, state.text)
            revision = state.json()["timeline_revision"]
            _controls, writes = send_state_controls(state, token, "clear")
            (method, stale_clear), = writes
            self.assertEqual("DEL", method)
            self.assertIn(f"expected_timeline_revision={revision}", stale_clear)
            newer = client.post(version_url, json={"book_token": token, "expected_timeline_revision": revision,
                "points": [{"from_index": 3, "from_uid": "u3", "version_id": "manual"}]})
            self.assertEqual(200, newer.status_code, newer.text)
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            openapi = client.get("/openapi.json").json()
            clear = openapi["paths"]["/api/voices/{speaker}/version_timeline"]["delete"]
            revision_query = next(p for p in clear["parameters"] if p["name"] == "expected_timeline_revision")
            self.assertEqual(("query", True, voices_router.BOOK_TOKEN_PATTERN),
                             (revision_query["in"], revision_query["required"], revision_query["schema"]["pattern"]))
            for parameters, status in (({"book_token": token}, 422),
                                       ({"book_token": token, "expected_timeline_revision": "bad"}, 422)):
                result = client.delete(version_url, params=parameters)
                self.assertEqual(status, result.status_code, result.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            result = client.delete(stale_clear)
            self.assertEqual(409, result.status_code, result.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            _controls, writes = send_state_controls(client.get(state_url), token, "clear")
            (method, fresh_clear), = writes
            self.assertEqual("DEL", method)
            response = client.delete(fresh_clear)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn("version_timeline", json.loads(path.read_bytes())["MIRA"])

    def test_new_version_point_preserves_valid_extensions_and_strips_view_fields(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, _manager, client, path, _config, rows = self.fixture(directory, stack)
            point = {"from_index": 2, "version_id": "different", "from_uid": rows[2]["uid"],
                     "extension": {"roles": ["reader", {"owner": None}], "revision": 9007199254740993},
                     "note": None}
            response = client.post("/api/voices/A/version_timeline", json={"points": [
                {**point, "display_index": 20, "delete_key": "view only"}]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([point], response.json()["version_timeline"])
            self.assertEqual([point], json.loads(path.read_bytes())["A"]["version_timeline"])

    def test_state_and_guarded_replay_select_the_exact_index_type_and_presence(self):
        from tests.test_state_voices_js import VoiceStatesJsTests

        for kind, facade in (("style_timeline", "guarded"), ("version_timeline", "guarded"),
                             ("version_timeline", "state")):
            for first, second in ((2.0, 2), (2, 2.0), (None, 0), (0, None), ("2", 2.0)):
                with self.subTest(kind=kind, facade=facade, indices=(first, second)), (
                        tempfile.TemporaryDirectory()) as directory, ExitStack() as stack:
                    _root, manager, client, path, config, _rows = self.fixture(directory, stack)
                    field, value = (("character_style", "aged") if kind == "style_timeline"
                                    else ("version_id", "different"))
                    points = [{field: value, **({"from_index": index} if index is not None else {})}
                              for index in (first, second)]
                    config["A"].update({kind: points}, alias_of=None, ready=False,
                                       seed="-1", character_style="")
                    path.write_text(json.dumps(config, indent=4) + "\n")
                    chunks = Path(manager.chunks_path)
                    before = path.read_bytes(), chunks.read_bytes()
                    if facade == "guarded":
                        view = client.get("/api/voice_config/snapshot")
                        body = collect_ready_card(view, False)
                        submitted = body["voices"]["A"][kind]
                        url = "/api/voice_config/save"
                    else:
                        view = client.get("/api/voices/A/state_timeline")
                        submitted = VoiceStatesJsTests().run_js(
                            "console.log(JSON.stringify(JSON.parse(payload).applied));", view.text)
                        body = {"points": submitted, "expected_timeline_revision": view.json()["timeline_revision"]}
                        url = "/api/voices/A/version_timeline"
                    self.assertEqual(200, view.status_code, view.text)
                    response = client.post(url, json=body)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                    del submitted[0]
                    response = client.post(url, json=body)
                    self.assertEqual(200, response.status_code, response.text)
                    persisted = json.loads(path.read_bytes())["A"][kind]
                    self.assertEqual(json.dumps([points[1]], sort_keys=True),
                                     json.dumps(persisted, sort_keys=True))
                    self.assertEqual(before[1], chunks.read_bytes())

    def test_replay_selects_exact_optional_field_presence_before_legacy_equivalence(self):
        from tests.test_state_voices_js import VoiceStatesJsTests

        cases = (
            ("state", "version_timeline", "from_uid", None),
            ("state", "version_timeline", "version_id", None),
            ("guarded", "version_timeline", "from_uid", None),
            ("guarded", "version_timeline", "version_id", None),
            ("guarded", "style_timeline", "from_uid", None),
            ("guarded", "style_timeline", "character_style", None),
            ("guarded", "style_timeline", "character_style", ""),
        )
        for facade, kind, field, value in cases:
            with self.subTest(facade=facade, kind=kind, field=field, value=value), (
                    tempfile.TemporaryDirectory()) as directory, ExitStack() as stack:
                _root, manager, client, path, config, _rows = self.fixture(directory, stack)
                absent = {"from_index": 2}
                present = {**absent, field: value}
                config["A"].update({kind: [absent, present]}, alias_of=None, ready=False,
                                   seed="-1", character_style="")
                path.write_text(json.dumps(config, indent=4) + "\n")
                chunks = Path(manager.chunks_path)
                before = path.read_bytes(), chunks.read_bytes()

                if facade == "guarded":
                    view = client.get("/api/voice_config/snapshot")
                    body = collect_ready_card(view, False)
                    points = body["voices"]["A"][kind]
                    url = "/api/voice_config/save"
                else:
                    view = client.get("/api/voices/A/state_timeline")
                    points = VoiceStatesJsTests().run_js(
                        "console.log(JSON.stringify(JSON.parse(payload).applied));", view.text)
                    body = {"points": points, "expected_timeline_revision": view.json()["timeline_revision"]}
                    url = "/api/voices/A/version_timeline"

                self.assertEqual(200, view.status_code, view.text)
                response = client.post(url, json=body)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                self.assertNotIn(field, points[0])
                self.assertIn(field, points[1])
                del points[0]
                response = client.post(url, json=body)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([present], json.loads(path.read_bytes())["A"][kind])
                self.assertEqual(before[1], chunks.read_bytes())

    def test_malformed_nested_lora_adapter_ids_refuse_state_reads_and_mutations(self):
        from tests.test_state_review_fixes import two_state_script

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            script = two_state_script()
            script.append({"speaker": "B", "text": "A separate character."})
            Path(manager.script_path).write_text(json.dumps(script))
            rows = [{**row, "id": i} for i, row in enumerate(script)]
            chunks = Path(manager.chunks_path)
            chunks.write_text(json.dumps(rows, indent=4))
            original = {
                name: {"type": "custom", "voice": "Ryan", "versions": {
                    "legacy": {"type": "lora", "adapter_id": "fixture", "age_group": "adult"}}}
                for name in ("MIRA", "B")}
            catalog = stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            state_url = "/api/voices/MIRA/state_timeline"
            timeline_url = "/api/voices/MIRA/version_timeline"
            expected = "A LoRA adapter ID must be a valid adapter name."
            for speaker in original:
                for adapter_type, bad_id in (("lora", ["legacy"]), ("builtin_lora", []),
                                             ("lora", {"id": "legacy"}), ("builtin_lora", 7),
                                             (None, ["legacy"]), ("lora", "../fixture")):
                    with self.subTest(speaker=speaker, adapter_type=adapter_type, bad_id=bad_id):
                        config = copy.deepcopy(original)
                        version = config[speaker]["versions"]["legacy"]
                        if adapter_type is None:
                            config[speaker].update(type="lora", adapter_id="base")
                            version.pop("type")
                        else:
                            version["type"] = adapter_type
                        version["adapter_id"] = bad_id
                        path.write_text(json.dumps(config, indent=4) + "\n")
                        before = path.read_bytes(), chunks.read_bytes()
                        for response in (client.get(state_url), client.post(timeline_url, json={
                                "points": [{"from_index": 2, "version_id": "legacy"}]})):
                            self.assertEqual(400, response.status_code, response.text)
                            self.assertEqual(expected, response.json()["detail"])
                            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            catalog.assert_not_called()

            for speaker in original:
                with self.subTest(speaker=speaker, control="unconsumed age metadata"):
                    config = copy.deepcopy(original)
                    config[speaker]["versions"]["legacy"]["age_group"] = ["legacy"]
                    path.write_text(json.dumps(config, indent=4) + "\n")
                    before = path.read_bytes(), chunks.read_bytes()
                    view = client.get(state_url)
                    self.assertEqual(200, view.status_code, view.text)
                    replay = client.post(timeline_url, json={"points": view.json()["applied"]})
                    self.assertEqual(200, replay.status_code, replay.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_and_dedicated_version_writes_refuse_new_malformed_adapter_ids(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            for name in config:
                for kind, invalid in (("lora", ["legacy"]), ("builtin_lora", False), ("lora", "a/b")):
                    with self.subTest(name=name, kind=kind, invalid=invalid):
                        version = {"type": kind, "adapter_id": invalid}
                        edited = copy.deepcopy(snapshot["config"])
                        edited[name].setdefault("versions", {})["invalid"] = version
                        rejected = [
                            client.post("/api/voice_config/save", json={
                                "voices": edited, "revision": snapshot["revision"],
                                "book_token": snapshot["book_token"]}),
                            client.post(f"/api/voices/{name}/versions", json={
                                "version_id": "invalid", "config": version, "book_token": snapshot["book_token"]}),
                        ]
                        for response in rejected:
                            self.assertEqual(422, response.status_code, response.text)
                            self.assertEqual("A LoRA adapter ID must be a valid adapter name.", response.json()["detail"])
                            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            valid = {"type": "builtin_lora", "adapter_id": "fixture_adapter", "extension": {"keep": True}}
            response = client.post("/api/voices/A/versions", json={
                "version_id": "valid", "config": valid, "book_token": snapshot["book_token"]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({**valid, "age_group": "adult"},
                             json.loads(path.read_bytes())["A"]["versions"]["valid"])

    def test_version_saves_validate_the_effective_adapter_id_for_each_speaker(self):
        cases = (
            ("custom", "a/b", {"type": "lora"}, 422, None),
            ("custom", "a/b", {"type": "builtin_lora", "adapter_id": "override"}, 200, "override"),
            ("custom", "a/b", {"type": "lora", "adapter_id": None}, 200, None),
            ("lora", "base", {"description": "inherits the base adapter"}, 200, "base"),
            ("lora", "base", {"adapter_id": "a/b"}, 422, None),
            ("lora", "base", {"adapter_id": None}, 200, None),
            ("custom", "a/b", {"type": None}, 200, "a/b"),
        )
        for speaker in ("A", "B"):
            for base_type, adapter_id, version, status, effective_id in cases:
                with self.subTest(speaker=speaker, base_type=base_type, version=version), (
                        tempfile.TemporaryDirectory()) as directory, ExitStack() as stack:
                    _root, manager, client, path, config, rows = self.fixture(directory, stack)
                    config[speaker].update(type=base_type, adapter_id=adapter_id)
                    original = json.dumps(config, indent=4).encode() + b"\n"
                    touched_index = next(i for i, row in enumerate(rows) if row["speaker"] == speaker)
                    chunks = Path(manager.chunks_path)
                    chunk_bytes = chunks.read_bytes()
                    for route in ("dedicated", "guarded"):
                        with self.subTest(route=route):
                            path.write_bytes(original)
                            chunks.write_bytes(chunk_bytes)
                            snapshot = client.get("/api/voice_config/snapshot").json()
                            token = snapshot["book_token"]
                            if route == "dedicated":
                                response = client.post(f"/api/voices/{speaker}/versions", json={
                                    "version_id": "fixture", "config": version, "book_token": token})
                            else:
                                submitted = copy.deepcopy(snapshot["config"])
                                submitted[speaker].setdefault("versions", {})["fixture"] = version
                                response = client.post("/api/voice_config/save", json={
                                    "voices": submitted, "revision": snapshot["revision"], "book_token": token})
                            self.assertEqual(status, response.status_code, response.text)
                            if status != 200:
                                self.assertEqual("A LoRA adapter ID must be a valid adapter name.",
                                                 response.json()["detail"])
                                self.assertEqual((original, chunk_bytes), (path.read_bytes(), chunks.read_bytes()))
                                continue
                            response = client.post(f"/api/voices/{speaker}/version_timeline", json={
                                "book_token": token, "points": [
                                    {"from_index": touched_index, "from_uid": rows[touched_index]["uid"],
                                     "version_id": "fixture"}]})
                            self.assertEqual(200, response.status_code, response.text)
                            saved = json.loads(path.read_bytes())
                            effective = tts.voice_config_for_chunk(saved, speaker, touched_index)[speaker]
                            self.assertEqual(effective_id, effective["adapter_id"])
                            self.assertEqual(version.get("type", base_type), effective["type"])

    def test_state_admission_validates_effective_adapter_ids_in_target_and_sibling_versions(self):
        from tests.test_state_review_fixes import two_state_script

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _old, _rows = self.fixture(directory, stack)
            script = two_state_script() + [{"speaker": "B", "text": "A separate line."}]
            Path(manager.script_path).write_text(json.dumps(script))
            manager.save_chunks([{**row, "id": i, "uid": f"u{i}"} for i, row in enumerate(script)])
            chunks = Path(manager.chunks_path)
            candidates = stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            for speaker in ("MIRA", "B"):
                for base_type, adapter_id, version, status in (
                        ("custom", "a/b", {"type": "lora"}, 400),
                        ("custom", "a/b", {"type": "lora", "adapter_id": "override"}, 200),
                        ("custom", "a/b", {"type": "builtin_lora", "adapter_id": None}, 200),
                        ("custom", "a/b", {"type": None}, 200),
                        ("lora", "base", {"description": "inherits the adapter"}, 200),
                        ("lora", "base", {"adapter_id": "a/b"}, 400),
                        ("lora", "base", {"adapter_id": None}, 200)):
                    with self.subTest(speaker=speaker, base_type=base_type, version=version):
                        config = {name: {"type": "custom", "voice": "Ryan"} for name in ("MIRA", "B")}
                        config[speaker].update(type=base_type, adapter_id=adapter_id, versions={"legacy": version})
                        path.write_text(json.dumps(config, indent=4) + "\n")
                        before = path.read_bytes(), chunks.read_bytes()
                        calls = candidates.call_count
                        view = client.get("/api/voices/MIRA/state_timeline")
                        replay = client.post("/api/voices/MIRA/version_timeline", json={"points": []})
                        self.assertEqual((status, status), (view.status_code, replay.status_code))
                        if status == 400:
                            self.assertEqual("A LoRA adapter ID must be a valid adapter name.", view.json()["detail"])
                            self.assertEqual(view.json()["detail"], replay.json()["detail"])
                            self.assertEqual(calls, candidates.call_count)
                        else:
                            self.assertEqual(2, len(view.json()["states"]))
                        self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_base_id_refusal_precedes_any_generation_pin_comparison(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            config["A"].update(type="lora", adapter_id="base", adapter_generation_sha256="a" * 64)
            path.write_text(json.dumps(config, indent=4) + "\n")
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            alias_map = stack.enter_context(patch.object(voices_router, "get_resolved_adapter_id_mapping"))
            for pin in ({}, {"adapter_generation_sha256": None}):
                with self.subTest(pin=pin):
                    response = client.post("/api/voice_config/save", json={
                        "voices": {"A": {"adapter_id": "a/b", **pin}}, "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]})
                    self.assertEqual(422, response.status_code, response.text)
                    self.assertEqual("A LoRA adapter ID must be a valid adapter name.", response.json()["detail"])
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            alias_map.assert_not_called()

    def test_nested_json_type_change_is_not_a_guarded_or_dedicated_noop(self):
        for kind, facade in (("style_timeline", "guarded"), ("version_timeline", "guarded"),
                             ("version_timeline", "state")):
            with self.subTest(kind=kind, facade=facade), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, config, rows = self.fixture(directory, stack)
                point = {"from_index": 2, "from_uid": rows[2]["uid"], "extra": {"list": [True]}}
                point.update({"character_style": "aged"} if kind == "style_timeline" else {"version_id": "different"})
                config["A"][kind] = [point]
                path.write_text(json.dumps(config, indent=4))
                chunks = Path(manager.chunks_path)
                before = path.read_bytes(), chunks.read_bytes()
                if facade == "guarded":
                    snapshot = client.get("/api/voice_config/snapshot").json()
                    submitted = snapshot["config"]["A"][kind]
                    body = {"voices": snapshot["config"], "revision": snapshot["revision"],
                            "book_token": snapshot["book_token"]}
                    url = "/api/voice_config/save"
                else:
                    submitted = client.get("/api/voices/A/state_timeline").json()["applied"]
                    body = {"points": submitted}
                    url = "/api/voices/A/version_timeline"
                submitted[0]["extra"]["list"][0] = 1
                response = client.post(url, json=body)
                self.assertEqual(200, response.status_code, response.text)
                persisted = json.loads(path.read_bytes())["A"][kind]
                self.assertIs(int, type(persisted[0]["extra"]["list"][0]))
                self.assertNotEqual(before[0], path.read_bytes())
                self.assertEqual(before[1], chunks.read_bytes())

    def test_new_same_uid_version_point_does_not_inherit_a_separate_records_extensions(self):
        for retain, new_index in ((True, 2), (True, 3), (False, 3)):
            with self.subTest(retain=retain, new_index=new_index), (
                    tempfile.TemporaryDirectory()) as directory, ExitStack() as stack:
                _root, manager, client, path, config, rows = self.fixture(directory, stack)
                uid = rows[2]["uid"]
                old = {"from_index": 2, "from_uid": uid, "version_id": "different", "private": {"tag": "old"}}
                config["A"]["version_timeline"] = [old]
                path.write_text(json.dumps(config))
                manager.insert_chunk(0)
                new = {"from_index": new_index, "from_uid": uid, "version_id": "same", "note": "new"}
                response = client.post("/api/voices/A/version_timeline",
                                       json={"points": [new, old] if retain else [new]})
                self.assertEqual(200, response.status_code, response.text)
                expected = [dict(new, from_index=3)] + ([old] if retain else [])
                self.assertEqual(expected, response.json()["version_timeline"])
                self.assertEqual(expected, json.loads(path.read_bytes())["A"]["version_timeline"])

    def test_version_limit_counts_new_records_against_grandfathered_history(self):
        for facade in ("state", "guarded"):
            with self.subTest(facade=facade), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, config, _rows = self.fixture(directory, stack)
                rows = make_rows(55)
                manager.save_chunks(rows)
                old = [{"from_index": i, "from_uid": row["uid"], "version_id": "different", "tag": "old"}
                       for i, row in enumerate(rows)]
                config["A"]["version_timeline"] = old
                path.write_text(json.dumps(config, indent=4) + "\n")
                chunks = Path(manager.chunks_path)
                before = path.read_bytes(), chunks.read_bytes()
                new = [dict(point, tag="new") for point in old]
                snapshot = client.get("/api/voice_config/snapshot").json()
                state = client.get("/api/voices/A/state_timeline").json()

                def submit(points):
                    if facade == "state":
                        return client.post("/api/voices/A/version_timeline", json={
                            "points": points, "expected_timeline_revision": state["timeline_revision"]})
                    snapshot["config"]["A"]["version_timeline"] = points
                    return client.post("/api/voice_config/save", json={
                        "voices": snapshot["config"], "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]})

                replay = submit(state["applied"])
                self.assertEqual(200, replay.status_code, replay.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                refused = submit(new[:51] + old[51:])
                self.assertEqual(422, refused.status_code, refused.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                admitted = submit(new[:50] + old[50:])
                self.assertEqual(200, admitted.status_code, admitted.text)
                self.assertEqual(new[:50] + old[50:], json.loads(path.read_bytes())["A"]["version_timeline"])
                self.assertEqual(before[1], chunks.read_bytes())

    def test_editor_refusals_keep_legacy_chunk_bytes_and_unsafe_voice_entry(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, rows = self.fixture(directory, stack)
            client.app.include_router(editor.router)
            stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
            stack.enter_context(patch.object(editor, "project_manager", manager))
            stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
            chunks = Path(manager.chunks_path)
            rows[1].pop("uid")
            chunks.write_text(json.dumps(rows, indent=4) + "\n")
            original = path.read_bytes(), chunks.read_bytes()
            token = get_book_operation_token(str(root))
            for method, route in (("POST", "/api/chunks/0/insert"), ("DELETE", "/api/chunks/0")):
                for params in ({"expected_uid": "old"}, {"expected_book_token": "0" * 64}):
                    response = client.request(method, route, params=params)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(original, (path.read_bytes(), chunks.read_bytes()))
            path.unlink()
            target = root / "missing-voices.json"
            path.symlink_to(target)
            for method, route in (("POST", "/api/chunks/0/insert"), ("DELETE", "/api/chunks/0")):
                for params in ({}, {"expected_uid": rows[0]["uid"], "expected_book_token": token}):
                    response = client.request(method, route, params=params)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(original[1], chunks.read_bytes())
                    self.assertEqual(target, path.readlink())
                    self.assertFalse(target.exists())

    def test_version_roundtrip_does_not_replace_bare_point_with_uid_sibling(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            bare = {"from_index": "002", "version_id": "different", "source": "manual"}
            anchored = {**bare, "from_uid": rows[2]["uid"]}
            config["A"]["version_timeline"] = [anchored, None, bare]
            path.write_text(json.dumps(config, indent=4) + "\n")
            before = path.read_bytes(), Path(manager.chunks_path).read_bytes()
            url = "/api/voices/A/version_timeline"
            output = client.get("/api/voices/A/state_timeline").json()["applied"]
            response = client.post(url, json={"points": output})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), Path(manager.chunks_path).read_bytes()))
            response = client.post(url, json={"points": [{"from_index": 2, "version_id": "different"}]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([bare], json.loads(path.read_bytes())["A"]["version_timeline"])
            manager.insert_chunk(0)
            resolved = resolve_timeline_anchors(json.loads(path.read_bytes()), manager.get_saved_chunks())
            self.assertEqual("different", tts.active_version_id(resolved["A"], 2))

    def test_editor_style_post_makes_the_selected_style_effective_at_a_tied_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            target = {"from_index": 3, "from_uid": rows[2]["uid"], "character_style": "selected", "audit": "keep"}
            shadow = {"from_index": 2, "from_uid": "missing", "character_style": "shadow"}
            config["A"]["style_timeline"] = [target, shadow]
            path.write_text(json.dumps(config, indent=4))
            chunks = Path(manager.chunks_path)
            old = resolve_timeline_anchors(json.loads(path.read_bytes()), manager.get_saved_chunks())
            self.assertEqual("shadow", tts.active_character_style(old["A"], 2))
            url = "/api/voices/A/style_timeline"
            body = {"from_index": 3, "from_uid": rows[2]["uid"], "character_style": "selected"}
            response = client.post(url, json=body)
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual([shadow, {**target, "from_index": 2}], saved["A"]["style_timeline"])
            resolved = resolve_timeline_anchors(saved, manager.get_saved_chunks())
            self.assertEqual("selected", tts.active_character_style(resolved["A"], 2))
            before = path.read_bytes(), chunks.read_bytes()
            self.assertEqual(200, client.post(url, json=body).status_code)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_ascii_badge_click_deletes_only_its_exact_saved_record(self):
        from html.parser import HTMLParser
        from tests.test_inline_string_arguments_js import InlineStringArgumentsJsTests

        class Clicks(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a" and "onclick" in attrs:
                    self.clicks.append({"handler": attrs["onclick"], "deleteKey": attrs["data-delete-key"]})

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            arabic = {"from_index": "١٠", "character_style": "preserved"}
            ascii_point = {"from_index": "010", "character_style": "remove"}
            config["A"]["style_timeline"] = [arabic, ascii_point, ascii_point.copy()]
            path.write_text(json.dumps(config, indent=4))
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            url = "/api/voices/A/style_timeline/10"
            self.assertEqual(409, client.delete(url, params={"legacy_only": True}).status_code)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            snapshot = client.get("/api/voice_config/snapshot").json()
            card = next(row for row in snapshot["voices"] if row["name"] == "A")
            js = InlineStringArgumentsJsTests()
            markup = js.run_js("console.log(JSON.stringify(context.renderStyleTimeline('A',payload)));", card["config"])
            parser = Clicks()
            parser.clicks = []
            parser.feed(markup)
            self.assertEqual(2, len(parser.clicks))
            request, = js.run_js(r'''
const urls=[];context.API={del:async url=>urls.push(url)};context.loadVoices=async()=>{};
vm.runInContext('(function(){'+payload.handler+'})',context).call({dataset:{deleteKey:payload.deleteKey}});
await new Promise(resolve=>setImmediate(resolve));console.log(JSON.stringify(urls));
''', parser.clicks[0])
            response = client.delete(request)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([arabic, ascii_point], json.loads(path.read_bytes())["A"]["style_timeline"])
            before = path.read_bytes(), chunks.read_bytes()
            self.assertEqual(409, client.delete(request).status_code)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_state_get_on_cold_book_and_catalog_never_publishes_files(self):
        import core
        import hf_utils
        from tests.test_state_review_fixes import two_state_script

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, _path, _config, _rows = self.fixture(directory, stack)
            builtins = root / "builtin_lora"
            builtins.mkdir()
            catalog = builtins / "manifest.json"
            catalog.write_text('[{"id":"fixture","name":"Local voice","gender":"female","age_group":"adult"}]\n')
            adapter = builtins / "builtin_fixture"
            adapter.mkdir()
            for name in hf_utils.REQUIRED_ADAPTER_FILES:
                (adapter / name).write_bytes(b"x")
            (adapter / ".download_complete.json").write_text(json.dumps({
                name: 1 for name in hf_utils.REQUIRED_ADAPTER_FILES}))
            models = root / "lora_models"
            models.mkdir()
            (models / "manifest.json").write_text("[]\n")
            designed = root / "designed_voices"
            designed.mkdir()
            (designed / "manifest.json").write_text("[")
            stack.enter_context(patch.object(core, "BUILTIN_LORA_DIR", str(builtins)))
            stack.enter_context(patch.object(voices_router, "LORA_MODELS_MANIFEST", str(models / "manifest.json")))
            stack.enter_context(patch.object(voices_router, "LORA_MODELS_DIR", str(models)))
            stack.enter_context(patch.object(voices_router, "DESIGNED_VOICES_DIR", str(designed)))
            stack.enter_context(patch.object(hf_utils, "_manifest_cache", None))
            download = stack.enter_context(patch("huggingface_hub.hf_hub_download", side_effect=AssertionError))
            diagnostic = stack.enter_context(patch("review_startup.record_failure", side_effect=AssertionError))
            # Force the fallback manager as well as a cold set of filesystem locks.
            audio_dir = Path(manager.voicelines_dir)
            for wav in audio_dir.glob("*.wav"):
                wav.unlink()
            audio_dir.rmdir()
            Path(manager.chunks_path).unlink()
            stack.enter_context(patch.object(voices_router, "project_manager", SimpleNamespace(chunks_path=None)))
            for lock in root.rglob("*.lock"):
                lock.unlink()

            def snapshot():
                return {str(entry.relative_to(root)): entry.read_bytes() if entry.is_file() else None
                        for entry in root.rglob("*")}

            before = snapshot()
            response = client.get("/api/voices/A/state_timeline")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], response.json()["states"])
            self.assertEqual(before, snapshot())
            Path(manager.script_path).write_text(json.dumps(two_state_script()))
            before = snapshot()
            response = client.get("/api/voices/MIRA/state_timeline")
            self.assertEqual(200, response.status_code, response.text)
            states = response.json()["states"]
            self.assertEqual(2, len(states))
            self.assertIn("builtin_fixture", [option["adapter_id"] for state in states
                          for option in state["sources"]["library_unused"]])
            self.assertEqual(before, snapshot())
            download.assert_not_called()
            diagnostic.assert_not_called()

    def test_choose_main_and_apply_remove_versions_inside_picker_intervals(self):
        from tests.test_state_review_fixes import two_state_script
        from tests.test_state_voices_js import VoiceStatesJsTests

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            script = two_state_script()
            Path(manager.script_path).write_text(json.dumps(script))
            rows = [{**row, "id": index, "uid": f"u{index}"} for index, row in enumerate(script)]
            manager.save_chunks(rows)
            path.write_text(json.dumps({"MIRA": {"type": "custom", "voice": "Ryan",
                "versions": {"manual": {"type": "custom", "voice": "Serena"}},
                "version_timeline": [{"from_index": 2, "from_uid": "u2", "version_id": "manual"}, None]}}))
            stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            response = client.get("/api/voices/MIRA/state_timeline")
            self.assertEqual(200, response.status_code, response.text)
            suggestion = response.json()
            first, second = (state["from_index"] for state in suggestion["states"])
            self.assertLess(first, 2)
            self.assertLess(2, second)
            old = resolve_timeline_anchors(json.loads(path.read_bytes()), rows)
            self.assertEqual("manual", tts.active_version_id(old["MIRA"], 2))
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            method, url, body = VoiceStatesJsTests().run_js(r'''
const data=payload.suggestion;
context.window._voiceStateSuggestions={MIRA:data};
context._voiceSaveSnapshot={book_token:payload.token};
const selects=data.states.map((state,index)=>({dataset:{}, value:context.getVoiceStateDefault(state,data.applied,index)}));
const rows=data.states.map((state,index)=>({dataset:{fromIndex:String(state.from_index),uid:state.uid,age:state.age_group},
    querySelector:()=>selects[index]}));
const card={dataset:{voice:'MIRA'},querySelectorAll:selector=>selector==='.voice-state-source'?selects:rows};
const button={closest:()=>card};
context.chooseMainVoiceStates(button);
await context.applyVoiceStates(button);
console.log(JSON.stringify(calls.find(call=>call[0]==='POST')));
''', {"suggestion": suggestion, "token": token})
            self.assertEqual([None], body["points"])
            response = client.request(method, url, json=body)
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual([None], saved["MIRA"]["version_timeline"])
            resolved = resolve_timeline_anchors(saved, manager.get_saved_chunks())
            self.assertTrue(all(tts.active_version_id(resolved["MIRA"], index) is None
                                for index in range(first, len(rows))))
            before = path.read_bytes(), Path(manager.chunks_path).read_bytes()
            response = client.request(method, url, json=body)
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), Path(manager.chunks_path).read_bytes()))

    def test_state_get_can_replay_null_and_long_saved_timelines_exactly(self):
        from tests.test_state_voices_js import VoiceStatesJsTests

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            for count in (None, 50, 51):
                with self.subTest(count=count):
                    points = None if count is None else [
                        {"from_index": str(index), "from_uid": f"removed-{index}",
                         "version_id": "different", "extension": [index]}
                        for index in range(count)]
                    config["A"]["version_timeline"] = points
                    path.write_text(json.dumps(config, indent=4) + "\n")
                    token = client.get("/api/voice_config/snapshot").json()["book_token"]
                    chunks = Path(manager.chunks_path)
                    before = path.read_bytes(), chunks.read_bytes()
                    response = client.get("/api/voices/A/state_timeline")
                    self.assertEqual(200, response.status_code, response.text)
                    suggestion = response.json()
                    method, url, body = VoiceStatesJsTests().run_js(r'''
context.window._voiceStateSuggestions={A:payload.suggestion};
context._voiceSaveSnapshot={book_token:payload.token};
await context.applyVoiceStates({closest:()=>({dataset:{voice:'A'},querySelectorAll:()=>[]})});
console.log(JSON.stringify(calls.find(call=>call[0]==='POST')));
''', {"suggestion": suggestion, "token": token})
                    self.assertEqual(suggestion["applied"], body["points"])
                    response = client.request(method, url, json=body)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(points, response.json()["version_timeline"])
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                    if count is not None:
                        refused = client.post(url, json={"points": None})
                        self.assertEqual(409, refused.status_code, refused.text)
                        over_limit = client.post(url, json={"points": [*points, points[-1]]})
                        self.assertEqual(422, over_limit.status_code, over_limit.text)
                        self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_style_write_keeps_the_legacy_mutation_callable_and_forwards_tokens(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, _manager, client, path, _config, rows = self.fixture(directory, stack)
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            original = voices_router._mutate_book_voice_entry
            seen = []
            def legacy_mutate(speaker, mutator, book_token=None):
                seen.append(book_token)
                return original(speaker, mutator, book_token)
            with patch.object(voices_router, "_mutate_book_voice_entry", legacy_mutate):
                for style, scope in (("warm", {}), ("aged", {"book_token": token})):
                    response = client.post("/api/voices/A/style_timeline", json={
                        "from_index": 2, "character_style": style, **scope})
                    self.assertEqual(200, response.status_code, response.text)
                    saved = json.loads(path.read_bytes())["A"]["style_timeline"]
                    self.assertEqual([{"from_index": 2, "from_uid": rows[2]["uid"],
                                       "character_style": style}], saved)
            self.assertEqual([None, token], seen)

    def test_state_get_validates_serializable_voice_data_before_backfill(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            for row in rows:
                row.pop("uid")
            chunks.write_text(json.dumps(rows))
            for bad in ("\ud800", float("nan")):
                with self.subTest(value=ascii(bad)):
                    config["A"]["version_timeline"] = [{"from_index": 2, "version_id": bad}]
                    path.write_text(json.dumps(config))
                    before = (path.read_bytes(), chunks.read_bytes())
                    response = client.get("/api/voices/A/state_timeline")
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_malformed_voice_document_refuses_every_timeline_route_with_same_detail(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            scope = {"book_token": snapshot["book_token"]}
            revision = client.get("/api/voices/A/state_timeline").json()["timeline_revision"]
            chunks = Path(manager.chunks_path)
            def exercise():
                return [
                    client.get("/api/voices/A/state_timeline"),
                    client.post("/api/voices/A/style_timeline", json={
                        **scope, "from_index": 2, "character_style": "aged"}),
                    client.post("/api/voices/A/version_timeline", json={
                        **scope, "points": [{"from_index": 2, "version_id": "different"}]}),
                    client.delete("/api/voices/A/style_timeline/2", params=scope),
                    client.delete("/api/voices/A/version_timeline", params={
                        **scope, "expected_timeline_revision": revision}),
                    client.post("/api/voice_config/save", json={
                        **scope, "revision": snapshot["revision"], "voices": snapshot["config"]}),
                ]
            for raw in (b"{", b"[]", b"\xff"):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    before = (path.read_bytes(), chunks.read_bytes())
                    responses = exercise()
                    self.assertEqual([400] * len(responses), [r.status_code for r in responses])
                    self.assertEqual(1, len({r.json()["detail"] for r in responses}))
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            from project_scope_files import capture_scope_file
            def deny_voice_reads(root, name, **kwargs):
                if Path(root) / name == path:
                    raise PermissionError("fixture path should not appear in the response")
                return capture_scope_file(root, name, **kwargs)
            with patch("project_scope_files.capture_scope_file", side_effect=deny_voice_reads):
                responses = exercise()
            self.assertEqual([409] * len(responses), [r.status_code for r in responses])
            self.assertEqual({"Active voices could not be read."}, {r.json()["detail"] for r in responses})
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_unsafe_voice_file_never_looks_missing_to_timeline_handlers(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            chunks = Path(manager.chunks_path)
            before = chunks.read_bytes()
            target = root / "missing.json"
            path.unlink()
            path.symlink_to(target)
            def check_routes():
                responses = [
                    client.get("/api/voices/A/state_timeline"),
                    client.get("/api/voice_config/snapshot"),
                    client.post("/api/voices/A/style_timeline", json={
                        "from_index": 2, "character_style": "aged"}),
                    client.post("/api/voice_config/save", json={
                        "voices": snapshot["config"], "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]}),
                ]
                self.assertEqual([409] * len(responses), [response.status_code for response in responses])
                self.assertEqual(before, chunks.read_bytes())
            check_routes()
            self.assertEqual(target, path.readlink())
            self.assertFalse(target.exists())
            target.write_bytes(b'{"A": {}}')
            check_routes()
            self.assertEqual(target, path.readlink())
            self.assertEqual(b'{"A": {}}', target.read_bytes())
            path.unlink()
            path.mkdir()
            check_routes()
            self.assertTrue(path.is_dir())

    def test_style_edits_refuse_bad_history_and_preserve_supported_nulls(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            route = "/api/voices/A/style_timeline"
            point = {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}
            for history in ({"recovery": [point]}, False, 9, "retained", [1], [{"from_index": "bad"}]):
                with self.subTest(history=history):
                    config["A"]["style_timeline"] = history
                    path.write_text(json.dumps(config, indent=4))
                    before = path.read_bytes(), chunks.read_bytes()
                    for response in (client.post(route, json=point), client.delete(route + "/2")):
                        self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            config["A"]["style_timeline"] = None
            path.write_text(json.dumps(config, indent=4))
            before = path.read_bytes(), chunks.read_bytes()
            response = client.post(route, json={**point, "character_style": ""})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            config["A"]["style_timeline"] = [None]
            path.write_text(json.dumps(config))
            response = client.post(route, json=point)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([None, point], json.loads(path.read_bytes())["A"]["style_timeline"])

    def test_two_orphans_at_the_same_fallback_survive_a_version_edit(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            first = {"from_index": "002", "from_uid": "removed-a", "version_id": "different",
                     "extension": "first"}
            second = {"from_index": 2, "from_uid": "removed-b", "version_id": None,
                      "extension": "second"}
            config["A"]["version_timeline"] = [first, None, second]
            path.write_text(json.dumps(config, indent=4))
            point = {"from_index": 3, "from_uid": rows[3]["uid"], "version_id": "same"}
            response = client.post("/api/voices/A/version_timeline",
                                   json={"points": [first, None, second, point]})
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual([first, None, second, point], saved["A"]["version_timeline"])
            resolved = resolve_timeline_anchors(saved, manager.get_saved_chunks())
            self.assertIsNone(tts.active_version_id(resolved["A"], 2))

    def test_guarded_null_cannot_clear_configured_timeline(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, config, rows = self.fixture(directory, stack)
            config["A"].update(
                style_timeline=[{"from_index": 2, "character_style": "aged"}],
                version_timeline=[{"from_index": 2, "version_id": "different"}])
            path.write_text(json.dumps(config))
            chunks = Path(manager.chunks_path)
            audio = root / rows[2]["audio_path"]
            snapshot = client.get("/api/voice_config/snapshot").json()
            before = (path.read_bytes(), chunks.read_bytes(), audio.read_bytes())
            for key in ("style_timeline", "version_timeline"):
                with self.subTest(kind=key):
                    voices = copy.deepcopy(snapshot["config"])
                    voices["A"][key] = None
                    response = client.post("/api/voice_config/save", json={
                        "voices": voices, "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]})
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes(), audio.read_bytes()))
            voices["A"]["version_timeline"] = []
            response = client.post("/api/voice_config/save", json={
                "voices": voices, "revision": snapshot["revision"], "book_token": snapshot["book_token"]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], json.loads(path.read_bytes())["A"]["version_timeline"])

    def test_editor_blank_or_replace_consumes_the_legacy_point_at_the_selected_line(self):
        for style in ("", "new"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, config, rows = self.fixture(directory, stack)
                legacy = {"from_index": "002", "character_style": "old", "source": {"keep": True}}
                neighbor = {"from_index": 3, "from_uid": rows[3]["uid"], "character_style": "next"}
                config["A"]["style_timeline"] = [legacy, neighbor]
                path.write_text(json.dumps(config))
                response = client.post("/api/voices/A/style_timeline", json={
                    "from_index": 2, "from_uid": rows[2]["uid"], "character_style": style})
                self.assertEqual(200, response.status_code, response.text)
                expected = ([dict(legacy, from_index=2, from_uid=rows[2]["uid"], character_style=style)]
                            if style else []) + [neighbor]
                saved = json.loads(path.read_bytes())
                self.assertEqual(expected, saved["A"]["style_timeline"])
                manager.insert_chunk(0)
                resolved = resolve_timeline_anchors(saved, manager.get_saved_chunks())
                self.assertEqual("", tts.active_character_style(resolved["A"], 2))
                self.assertEqual(style, tts.active_character_style(resolved["A"], 3))
                self.assertEqual("next", tts.active_character_style(resolved["A"], 4))

    def test_index_only_style_post_and_delete_are_idempotent_and_badges_are_precise(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, rows = self.fixture(directory, stack)
            target = "/api/voices/A/style_timeline"
            self.assertEqual(200, client.post(target, json={
                "from_index": 2, "character_style": "aged"}).status_code)
            voices = json.loads(path.read_bytes())
            saved = voices["A"]["style_timeline"][0]
            self.assertEqual(rows[2]["uid"], saved["from_uid"])
            voices["A"]["style_timeline"].insert(0, {"from_index": "002", "character_style": "legacy"})
            path.write_text(json.dumps(voices))
            deleted = client.delete(target + "/2", params={"legacy_only": True})
            self.assertEqual(200, deleted.status_code, deleted.text)
            self.assertEqual([saved], json.loads(path.read_bytes())["A"]["style_timeline"])
            chunks = Path(manager.chunks_path)
            for attempt in range(2):
                response = client.delete(target + "/2")
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([], json.loads(path.read_bytes())["A"]["style_timeline"])
                if attempt:
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
                before = (path.read_bytes(), chunks.read_bytes())

    def test_unsafe_style_delete_index_cannot_change_persisted_records(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            config["A"]["style_timeline"] = [
                {"from_index": "9007199254740992", "character_style": "neighbor"},
                {"from_index": "9007199254740993", "character_style": "target"},
                {"from_index": "١٠", "character_style": "unicode"}]
            path.write_text(json.dumps(config))
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            for index in ("9007199254740993", "9007199254740992", "١٠", "NaN"):
                response = client.delete(f"/api/voices/A/style_timeline/{index}")
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_invalid_saved_uid_refuses_index_only_writes_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            chunks = Path(manager.chunks_path)
            for malformed in ("x" * 129, 47):
                with self.subTest(uid=malformed):
                    rows[2]["uid"] = malformed
                    chunks.write_text(json.dumps(rows))
                    before = (path.read_bytes(), chunks.read_bytes())
                    point = {"from_index": 2, "character_style": "aged"}
                    responses = [
                        client.get("/api/voices/A/state_timeline"),
                        client.post("/api/voices/A/style_timeline", json=point),
                        client.post("/api/voice_config/save", json={
                            "voices": {"A": {"style_timeline": [point]}},
                            "revision": snapshot["revision"], "book_token": snapshot["book_token"]}),
                    ]
                    self.assertEqual([409] * len(responses), [r.status_code for r in responses])
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_orphan_with_omitted_optional_style_echoes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, _rows = self.fixture(directory, stack)
            config["A"]["style_timeline"] = [{"from_index": "002", "from_uid": "removed",
                                              "source": {"keep": True}}]
            path.write_text(json.dumps(config, indent=4))
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            snapshot = client.get("/api/voice_config/snapshot").json()
            response = client.post("/api/voice_config/save", json={
                "voices": snapshot["config"], "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_delete_removes_only_one_point_after_insert(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"},
                {"from_index": 3, "from_uid": rows[3]["uid"], "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            response = client.delete(f"/api/voices/A/style_timeline/3?from_uid={rows[3]['uid']}")
            self.assertEqual(200, response.status_code, response.text)
            points = response.json()["style_timeline"]
            self.assertEqual([rows[2]["uid"]], [point["from_uid"] for point in points])

    def test_editor_insert_delete_and_render_preserve_content_voice_and_prior_audio(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, rows = self.fixture(directory, stack)
            client.app.include_router(editor.router)
            stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
            stack.enter_context(patch.object(editor, "project_manager", manager))
            stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
            stack.enter_context(patch.object(editor, "_deleted_chunk_receipts", {}))
            token = get_book_operation_token(str(root))
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged",
                "expected_book_token": token})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("pending", manager.get_saved_chunks()[2]["status"])
            seen = []
            def render(text, _instruct, speaker, config, output):
                seen.append((text, config[speaker]["character_style"]))
                sf.write(output, np.full(240, .2), 24000)
                return True
            manager.engine = SimpleNamespace(generate_voice=render)
            self.assertTrue(manager.generate_chunk_audio(2)[0])
            before = manager.get_saved_chunks()[2]
            original = (root / before["audio_path"]).read_bytes()
            voice_bytes = path.read_bytes()
            response = client.post("/api/chunks/0/insert", params={
                "expected_book_token": token, "expected_uid": rows[0]["uid"]})
            self.assertEqual(200, response.status_code, response.text)
            after = manager.get_saved_chunks()[3]
            self.assertEqual((before["uid"], "done", before["audio_path"]),
                             (after["uid"], after["status"], after["audio_path"]))
            self.assertEqual(original, (root / after["audio_path"]).read_bytes())
            self.assertTrue(manager.generate_chunk_audio(3)[0])
            for uid in (rows[0]["uid"], manager.get_saved_chunks()[1]["uid"]):
                response = client.delete("/api/chunks/0", params={
                    "expected_book_token": token, "expected_uid": uid})
                self.assertEqual(200, response.status_code, response.text)
            after_delete = manager.get_saved_chunks()[1]
            self.assertEqual((before["uid"], "done"), (after_delete["uid"], after_delete["status"]))
            self.assertEqual(original, (root / after_delete["audio_path"]).read_bytes())
            self.assertTrue(manager.generate_chunk_audio(1)[0])
            self.assertEqual([("Changed.", "aged")] * 3, seen)
            self.assertEqual(voice_bytes, path.read_bytes())

    def test_editor_delete_of_duplicate_uid_invalidates_the_survivor_and_excludes_its_wav(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, config, rows = self.fixture(directory, stack)
            client.app.include_router(editor.router)
            stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
            stack.enter_context(patch.object(editor, "project_manager", manager))
            stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
            stack.enter_context(patch.object(editor, "_deleted_chunk_receipts", {}))
            rows[2]["uid"] = rows[0]["uid"]
            config["A"]["style_timeline"] = [
                {"from_index": 2, "from_uid": rows[0]["uid"], "character_style": "older"}]
            path.write_text(json.dumps(config))
            manager.save_chunks(rows)
            wav = root / rows[0]["audio_path"]
            before = wav.read_bytes()
            old_voices = resolve_timeline_anchors(config, rows)
            self.assertEqual("", tts.active_character_style(old_voices["A"], 0))
            response = client.delete("/api/chunks/2", params={
                "expected_book_token": get_book_operation_token(str(root)),
                "expected_uid": rows[2]["uid"]})
            self.assertEqual(200, response.status_code, response.text)
            persisted = json.loads(Path(manager.chunks_path).read_bytes())
            self.assertEqual("older", tts.active_character_style(
                resolve_timeline_anchors(config, persisted)["A"], 0))
            self.assertEqual((rows[0]["text"], "pending", None),
                             (persisted[0]["text"], persisted[0]["status"], persisted[0]["audio_path"]))
            self.assertEqual(before, wav.read_bytes())
            audio, skipped = manager._load_chunks_with_audio(chunks=persisted)
            self.assertGreaterEqual(skipped, 1)
            self.assertNotIn(rows[0]["uid"], {row["uid"] for row, _wave in audio})
            self.assertIn(rows[1]["uid"], {row["uid"] for row, _wave in audio})

    def test_clear_timeline_invalidates_moved_anchor_audio(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "versions": {
                "different": {"type": "custom", "voice": "Serena"}},
                "version_timeline": [{"from_index": 2, "from_uid": rows[2]["uid"],
                                      "version_id": "different"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.delete_chunk(0)
            self.assertEqual("done", manager.load_chunks()[1]["status"])
            view = client.get("/api/voices/A/state_timeline").json()
            response = client.delete("/api/voices/A/version_timeline", params={
                "expected_timeline_revision": view["timeline_revision"]})
            self.assertEqual(200, response.status_code, response.text)
            moved = manager.load_chunks()[1]
            self.assertEqual("pending", moved["status"])
            self.assertIsNone(moved["audio_path"])

    def test_guarded_save_refuses_mismatched_uid(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            body = copy.deepcopy(snapshot["config"])
            body["A"]["style_timeline"] = [{"from_index": 12, "from_uid": rows[1]["uid"],
                                            "character_style": "aged"}]
            refused_mismatch = client.post("/api/voice_config/save", json={"voices": body,
                "revision": snapshot["revision"], "book_token": snapshot["book_token"]})
            self.assertEqual(400, refused_mismatch.status_code, refused_mismatch.text)
            fresh = client.get("/api/voice_config/snapshot").json()
            body = copy.deepcopy(fresh["config"])
            body["A"]["style_timeline"] = [{"from_index": 12, "character_style": "aged"}]
            refused = client.post("/api/voice_config/save", json={"voices": body,
                "revision": fresh["revision"], "book_token": fresh["book_token"]})
            self.assertEqual(400, refused.status_code, refused.text)

    def test_stale_explicit_uid_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            manager.delete_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 0, "from_uid": rows[0]["uid"], "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_damaged_authoritative_rows_refuse_reads_and_echoes_without_writes(self):
        for damage in ("missing_snapshot", "invalid_snapshot", "wrong_shape", "invalid_journal", "dangling_snapshot"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, config, rows = self.fixture(directory, stack)
                point = {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}
                config["A"]["style_timeline"] = [point]
                path.write_text(json.dumps(config))
                snapshot = client.get("/api/voice_config/snapshot").json()
                chunks = Path(manager.chunks_path)
                journal = Path(get_chunk_status_journal_path(chunks))
                if damage == "missing_snapshot":
                    chunks.unlink()
                    journal.write_bytes(b'{}\n')
                elif damage == "invalid_journal":
                    journal.write_bytes(b'{}\n')
                elif damage == "dangling_snapshot":
                    chunks.unlink()
                    chunks.symlink_to(chunks.with_name("absent.json"))
                else:
                    chunks.write_text("[" if damage == "invalid_snapshot" else "{}")
                def saved_bytes():
                    return {p.name: p.readlink() if p.is_symlink() else p.read_bytes()
                            for p in (path, chunks, journal) if p.is_symlink() or p.exists()}
                before = saved_bytes()
                responses = [
                    client.get("/api/voices/A/state_timeline"),
                    client.get("/api/voice_config/snapshot"),
                    client.post("/api/voices/A/style_timeline", json=point),
                    client.post("/api/voice_config/save", json={
                        "voices": snapshot["config"], "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]}),
                ]
                for response in responses:
                    self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, saved_bytes())

    def test_empty_chunk_snapshot_refuses_new_point(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            manager.save_chunks([])
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 20, "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_rejected_state_and_version_requests_never_backfill_legacy_chunks(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            for row in rows:
                row.pop("uid")
            chunks.write_text(json.dumps(rows))
            for speaker in ("A", "B"):
                with self.subTest(speaker=speaker):
                    malformed = copy.deepcopy(config)
                    malformed[speaker]["versions"] = []
                    path.write_text(json.dumps(malformed))
                    before = (path.read_bytes(), chunks.read_bytes())
                    view = client.get("/api/voices/A/state_timeline")
                    replay = client.post("/api/voices/A/version_timeline", json={"points": []})
                    self.assertEqual((400, 400), (view.status_code, replay.status_code))
                    self.assertEqual(view.json()["detail"], replay.json()["detail"])
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            for malformed_entry in (3, {**config["A"], "version_timeline": [{"from_index": 0, "version_id": ""}]}):
                with self.subTest(entry=malformed_entry):
                    path.write_text(json.dumps({**config, "A": malformed_entry}))
                    before = path.read_bytes(), chunks.read_bytes()
                    view = client.get("/api/voices/A/state_timeline")
                    replay = client.post("/api/voices/A/version_timeline", json={"points": []})
                    self.assertEqual((400, 400), (view.status_code, replay.status_code))
                    self.assertEqual(view.json()["detail"], replay.json()["detail"])
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            path.write_text(json.dumps(config))
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/version_timeline", json={
                "points": [{"from_index": 2, "version_id": "unknown"}]})
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_narrator_state_view_uses_the_same_legacy_entry_as_version_save(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            script = json.loads(Path(manager.script_path).read_text())
            for row in script:
                if row["speaker"] == "A":
                    row["speaker"] = "Narrator"
            Path(manager.script_path).write_text(json.dumps(script))
            entry = config.pop("A")
            entry["version_timeline"] = [{"from_index": 2, "from_uid": rows[2]["uid"], "version_id": "different"}]
            config["Narrator"] = entry
            path.write_text(json.dumps(config, indent=4) + "\n")
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            alias = client.get("/api/voices/NARRATOR/state_timeline")
            literal = client.get("/api/voices/Narrator/state_timeline")
            self.assertEqual((200, 200), (alias.status_code, literal.status_code))
            self.assertEqual(literal.json()["applied"], alias.json()["applied"])
            self.assertEqual(literal.json()["timeline_revision"], alias.json()["timeline_revision"])
            replay = client.post("/api/voices/NARRATOR/version_timeline", json={
                "points": alias.json()["applied"], "expected_timeline_revision": alias.json()["timeline_revision"]})
            self.assertEqual(200, replay.status_code, replay.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_snapshot_echo_keeps_nullable_and_distinct_point_records(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            config["A"].update(
                style_timeline=[{"from_index": "002", "character_style": "aged"},
                                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}],
                version_timeline=[None, {"from_index": "002", "version_id": "different"},
                                  {"from_index": 2, "from_uid": rows[2]["uid"], "version_id": "different"}])
            config["B"]["style_timeline"] = None
            config["C"] = None
            path.write_text(json.dumps(config, indent=4) + "\n")
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            snapshot = client.get("/api/voice_config/snapshot").json()
            self.assertIsNone(snapshot["config"]["B"]["style_timeline"])
            self.assertIsNone(snapshot["config"]["A"]["version_timeline"][0])
            request = {"voices": snapshot["config"], "revision": snapshot["revision"],
                       "book_token": snapshot["book_token"]}
            response = client.post("/api/voice_config/save", json=request)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(snapshot["revision"], response.json()["revision"])
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            request["voices"]["A"] = None
            response = client.post("/api/voice_config/save", json=request)
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_dedicated_echoes_keep_bytes_and_reject_stale_book_tokens(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, config, rows = self.fixture(directory, stack)
            style = {"from_index": "002", "from_uid": rows[2]["uid"], "character_style": "aged"}
            version = {"from_index": "002", "from_uid": rows[2]["uid"], "version_id": "different"}
            extension = {"extra": {"source": "fixture", "reviewed": True}}
            config["A"].update(style_timeline=[dict(style, character_style=" aged ", **extension)],
                               version_timeline=[dict(version, **extension)])
            path.write_text(json.dumps(config, indent=4) + "\n")
            manager.insert_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            for route, request in (("style_timeline", style), ("version_timeline", {"points": [version]})):
                with self.subTest(route=route):
                    url = f"/api/voices/A/{route}"
                    self.assertEqual(409, client.post(url, json={**request, "book_token": "0" * 64}).status_code)
                    response = client.post(url, json=request)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            old_token = get_book_operation_token(str(root))
            (root / "state.json").write_text('{"active_book_id":"book","book_generation":"two"}')
            response = client.post("/api/voices/A/style_timeline", json={
                **style, "expected_book_token": old_token})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_targeted_style_and_version_changes_keep_unknown_point_fields(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, _manager, client, path, config, rows = self.fixture(directory, stack)
            uid = rows[2]["uid"]
            style = {"from_index": "002", "from_uid": uid, "character_style": "aged", "audit": {"a": 1}}
            version = {"from_index": "002", "from_uid": uid, "version_id": "different", "audit": {"v": 2}}
            config["A"].update(style_timeline=[style], version_timeline=[version])
            path.write_text(json.dumps(config))
            self.assertEqual(200, client.post("/api/voices/A/style_timeline", json={
                "from_index": 2, "from_uid": uid, "character_style": "warm"}).status_code)
            self.assertEqual(200, client.post("/api/voices/A/version_timeline", json={
                "points": [{"from_index": 2, "from_uid": uid, "version_id": "same"}]}).status_code)
            saved = json.loads(path.read_bytes())["A"]
            self.assertEqual([dict(style, from_index=2, character_style="warm")], saved["style_timeline"])
            self.assertEqual([dict(version, from_index=2, version_id="same")], saved["version_timeline"])

    def test_style_echo_waits_for_chunk_edit_and_preserves_the_insert(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            point = {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}
            config["A"]["style_timeline"] = [point]
            path.write_text(json.dumps(config, indent=4))
            before = path.read_bytes()
            entered = Event()
            mutate = voices_router._mutate_book_voice_entry
            def notify(*args, **kwargs):
                entered.set()
                return mutate(*args, **kwargs)
            with ThreadPoolExecutor(max_workers=1) as pool, \
                 patch.object(voices_router, "_mutate_book_voice_entry", side_effect=notify):
                with manager._chunks_lock:
                    pending = pool.submit(client.post, "/api/voices/A/style_timeline", json=point)
                    self.assertTrue(entered.wait(5))
                    modified = rows[:1] + [{"uid": "inserted", "speaker": "A", "text": ""}] + rows[1:]
                    for index, row in enumerate(modified):
                        row["id"] = index
                    manager._save_chunks_locked(modified, uids_backfilled=True)
                    inserted_bytes = Path(manager.chunks_path).read_bytes()
                response = pending.result(timeout=5)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(3, response.json()["style_timeline"][0]["display_index"])
            self.assertEqual(inserted_bytes, Path(manager.chunks_path).read_bytes())
            self.assertEqual(before, path.read_bytes())

    def test_persona_version_echo_uses_moved_boundary_and_refuses_a_different_uid(self):
        from speaker_traits import get_persona_state_targets
        from tests.test_state_review_fixes import two_state_script
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            script = two_state_script()
            (root / "annotated_script.json").write_text(json.dumps(script))
            manager.save_chunks([{**row, "id": index, "uid": f"u{index}"}
                                 for index, row in enumerate(script)])
            target = get_persona_state_targets(script)["MIRA"][1]
            index = target["from_entry"]
            version_id = target["version_id"]
            point = {"from_index": index, "from_uid": f"u{index}", "version_id": version_id}
            path.write_text(json.dumps({"MIRA": {"type": "custom", "voice": "Ryan",
                "versions": {version_id: {"type": "custom", "voice": "Serena", "persona_state": target}},
                "version_timeline": [point]}}))
            manager.insert_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            route = "/api/voices/MIRA/version_timeline"
            response = client.post(route, json={"points": [point]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            response = client.post(route, json={"points": [{**point, "from_uid": f"u{index + 1}"}]})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_invalid_supplied_uids_are_rejected_consistently(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            for uid in (" ", "a" * 129, 7):
                with self.subTest(uid=uid):
                    style = {"from_index": 2, "from_uid": uid, "character_style": "aged"}
                    version = {"from_index": 2, "from_uid": uid, "version_id": "different"}
                    for response in (
                        client.post("/api/voices/A/style_timeline", json=style),
                        client.post("/api/voices/A/version_timeline", json={"points": [version]}),
                        client.post("/api/voice_config/save", json={
                            "voices": {"A": {"style_timeline": [style]}}, "revision": snapshot["revision"],
                            "book_token": snapshot["book_token"]}),
                    ):
                        self.assertEqual(400, response.status_code, response.text)
                    if isinstance(uid, str):
                        response = client.delete("/api/voices/A/style_timeline/2", params={"from_uid": uid})
                        self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))


class ScopeAndDriftTests(unittest.TestCase):
    def test_scope_from_full_book_keeps_active_anchor_without_orphan(self):
        rows = make_rows(5)
        voices = style_config({"from_index": 0, "from_uid": "u0", "character_style": "aged"})
        voices["A"]["type"] = "custom"
        plan = get_scope_voice_plan(voices, rows, 2)
        point = plan["voices"]["A"]["style_timeline"][0]
        self.assertEqual({"from_index": 0, "character_style": "aged"}, point)
        self.assertEqual("aged", style_at(plan["voices"], rows[2], 0))

    def test_drift_groups_follow_moved_anchor(self):
        helper = drift_tests.DriftVoiceStateTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drift_tests.save_pcm(root / "young.wav", 11)
            drift_tests.save_pcm(root / "adult.wav", 22)
            config = {"Hero": {"type": "clone", "ref_audio": "young.wav",
                "versions": {"adult": {"type": "clone", "ref_audio": "adult.wav"}},
                "version_timeline": [{"from_index": 1, "from_uid": "u1",
                                      "version_id": "adult"}]}}
            rows = []
            for index, uid in enumerate(("u0", "other", "u1", "u2")):
                path = root / f"line-{index}.wav"
                drift_tests.save_pcm(path, 100 + index)
                rows.append({"id": index, "uid": uid, "speaker": "Hero",
                             "text": f"line {index}", "status": "done", "audio_path": path.name})
            groups, keys = voice_drift.get_drift_voice_groups(rows, config)
            self.assertEqual(keys[2], keys[3])
            self.assertEqual(keys[0], keys[1])
            self.assertNotEqual(keys[1], keys[2])


class IdentityContractTests(unittest.TestCase):
    def fixture(self, directory, stack, config=None):
        return v733.VoiceTimelineAudioTests.fixture(self, directory, stack)

    def test_blank_then_replacement_keep_the_neighbouring_point(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"},
                {"from_index": 3, "from_uid": rows[3]["uid"], "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            blank = client.post("/api/voices/A/style_timeline", json={
                "from_index": 3, "from_uid": rows[2]["uid"], "character_style": ""})
            self.assertEqual(200, blank.status_code, blank.text)
            self.assertEqual([rows[3]["uid"]],
                             [p["from_uid"] for p in blank.json()["style_timeline"]])
            replace = client.post("/api/voices/A/style_timeline", json={
                "from_index": 3, "from_uid": rows[2]["uid"], "character_style": "replaced"})
            self.assertEqual(200, replace.status_code, replace.text)
            points = replace.json()["style_timeline"]
            self.assertEqual({rows[2]["uid"], rows[3]["uid"]}, {p["from_uid"] for p in points})
            self.assertEqual("replaced", next(p["character_style"] for p in points
                                              if p["from_uid"] == rows[2]["uid"]))

    def test_delete_unknown_uid_never_falls_back_to_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            response = client.delete("/api/voices/A/style_timeline/0?from_uid=missing")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(config["A"]["style_timeline"], json.loads(path.read_text())["A"]["style_timeline"])
            self.assertEqual(config["A"]["style_timeline"], json.loads(path.read_text())["A"]["style_timeline"])

    def test_legacy_points_sharing_an_index_are_all_removed(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "character_style": "first"},
                {"from_index": 2, "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            response = client.delete("/api/voices/A/style_timeline/2")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], response.json()["style_timeline"])

    def test_dedicated_post_refuses_out_of_range_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, _path, _stored, _rows = self.fixture(directory, stack)
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 12, "from_uid": "u2", "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)

    def test_sparse_batch_uses_full_book_after_anchor_moves_outside_batch(self):
        import contextlib
        import io
        import sys
        from tests.test_batch_style_timeline import get_engine, get_torch
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        seen = []
        def render(**kwargs):
            seen.extend(kwargs["instruct"])
            return [np.full(400, 0.1, dtype="float32") for _ in kwargs["text"]], 16000
        engine = get_engine()
        engine._init_local_custom = Mock(return_value=SimpleNamespace(generate_custom_voice=render))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"torch": get_torch()}), \
                contextlib.redirect_stdout(io.StringIO()):
            manager = ProjectManager(directory)
            uids = ["u0", "u1", "u2", "u3", "fresh", "u4"]
            rows = [{"id": index, "uid": uid, "speaker": "A", "text": f"line {index}",
                     "instruct": "soft", "status": "pending"} for index, uid in enumerate(uids)]
            manager.save_chunks(rows)
            Path(manager.voice_config_path).write_text(json.dumps(voices))
            with patch.object(manager, "get_engine", return_value=engine):
                result = manager.generate_chunks_batch([2, 4], batch_size=4, batch_seed=0,
                                                       batch_group_by_type=False)
            self.assertEqual([], result["failed"])
        self.assertEqual(["young soft", "young soft"], seen[:2])

    def test_empty_state_get_does_not_backfill_and_legacy_write_is_revision_fenced(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            for row in rows:
                row.pop("uid", None)
            manager.save_chunks(rows)
            chunks = Path(manager.chunks_path)
            before = path.read_bytes(), chunks.read_bytes()
            state = client.get("/api/voices/A/state_timeline")
            self.assertEqual(200, state.status_code, state.text)
            self.assertEqual([], state.json()["states"])
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            point = {"from_index": 2, "version_id": "different"}
            before_revision = state.json()["chunks_revision"]
            manager.insert_chunk(0)
            before = path.read_bytes(), chunks.read_bytes()
            response = client.post("/api/voices/A/version_timeline", json={
                "points": [point], "expected_chunks_revision": before_revision})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            manager.save_chunks(rows)
            fresh = client.get("/api/voices/A/state_timeline").json()["chunks_revision"]
            response = client.post("/api/voices/A/version_timeline", json={
                "points": [point], "expected_chunks_revision": fresh})
            self.assertEqual(200, response.status_code, response.text)
            persisted = json.loads(chunks.read_bytes())
            saved_point = json.loads(path.read_bytes())["A"]["version_timeline"][0]
            self.assertTrue(persisted[2]["uid"])
            self.assertEqual(persisted[2]["uid"], saved_point["from_uid"])

    def test_state_view_apply_and_replay_keep_legacy_fallback_and_null_neighbor(self):
        from speaker_traits import get_persona_state_targets
        from tests.test_state_review_fixes import two_state_script
        from tests.test_state_voices_js import VoiceStatesJsTests
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            script = two_state_script()
            (root / "annotated_script.json").write_text(json.dumps(script))
            manager.save_chunks([{**row, "id": index, "uid": f"u{index}"}
                                 for index, row in enumerate(script)])
            boundary = get_persona_state_targets(script)["MIRA"][1]
            index = boundary["from_entry"]
            version_id = boundary["version_id"]
            point = {"from_index": str(index), "from_uid": f"u{index}", "version_id": version_id,
                     "extension": {"keep": True}}
            path.write_text(json.dumps({"MIRA": {"type": "custom", "voice": "Ryan",
                "versions": {version_id: {"type": "custom", "voice": "Serena", "persona_state": boundary}},
                "version_timeline": [point, None]}}, indent=4))
            stack.enter_context(patch.object(voices_router, "get_state_voice_candidates", return_value=[]))
            client.app.include_router(editor.router)
            stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
            stack.enter_context(patch.object(editor, "project_manager", manager))
            stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
            state_url = "/api/voices/MIRA/state_timeline"
            before = path.read_bytes(), Path(manager.chunks_path).read_bytes()
            response = client.get(state_url)
            self.assertEqual(200, response.status_code, response.text)
            suggestion = response.json()
            self.assertEqual([{**point, "display_index": index}, None], suggestion["applied"])
            self.assertEqual(before, (path.read_bytes(), Path(manager.chunks_path).read_bytes()))
            calls = VoiceStatesJsTests().run_js(r'''
const assert=require('assert');
const data=payload.suggestion;
const html=context.renderVoiceStateRows(data,'MIRA');
assert.match(html, new RegExp('value="version:'+payload.version+'" selected'));
context.window._voiceStateSuggestions={MIRA:data};
const rows=data.states.map((state,index)=>({
 dataset:{fromIndex:String(state.from_index),uid:state.uid,age:state.age_group},
 querySelector:()=>({value:context.getVoiceStateDefault(state,data.applied,index)})
}));
await context.applyVoiceStates({closest:()=>({dataset:{voice:'MIRA'},querySelectorAll:()=>rows})});
console.log(JSON.stringify(calls.filter(call=>call[0]==='POST')));
''', {"suggestion": suggestion, "version": version_id})
            self.assertEqual(suggestion["applied"], calls[0][2]["points"])
            response = client.post("/api/chunks/0/insert", params={
                "expected_book_token": get_book_operation_token(str(root)), "expected_uid": "u0"})
            self.assertEqual(200, response.status_code, response.text)
            after = path.read_bytes(), Path(manager.chunks_path).read_bytes()
            self.assertEqual(before[0], after[0])
            url = calls[0][1]
            token = client.get("/api/voice_config/snapshot").json()["book_token"]
            response = client.post(url, json={**calls[0][2], "book_token": token})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(after, (path.read_bytes(), Path(manager.chunks_path).read_bytes()))
            moved = client.get(state_url).json()["applied"]
            self.assertEqual([{**point, "display_index": index + 1}, None], moved)
            response = client.post(url, json={"points": moved, "book_token": token})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(after, (path.read_bytes(), Path(manager.chunks_path).read_bytes()))

    def test_snapshot_reports_current_line_without_rewriting_stored_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            snapshot = client.get("/api/voice_config/snapshot").json()
            point = snapshot["config"]["A"]["style_timeline"][0]
            self.assertEqual(2, point["from_index"])
            self.assertEqual(3, point["display_index"])
            self.assertEqual(2, json.loads(path.read_text())["A"]["style_timeline"][0]["from_index"])


if __name__ == "__main__":
    unittest.main()

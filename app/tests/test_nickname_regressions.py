"""Alias discovery must reject group mappings and malformed input shapes."""

from tests.test_support import assert_file_lock_released
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import find_nicknames
import generate_script
import llm_provider


class SetupReached(Exception):
    pass


class NicknameRegressionTests(unittest.TestCase):
    @staticmethod
    def client(contents):
        results = iter(contents)
        def create(**_kwargs):
            item = next(results)
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=item), finish_reason="stop")], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=create))))

    def test_chunk_retry_recovers_and_exhaustion_is_not_empty_success(self):
        entries = [{"speaker": "ALICE", "text": "Hello."},
                   {"speaker": "BETTY", "text": "Good day."}]
        error = RuntimeError("rate limited"); error.status_code = 429
        params = generate_script.LLMGenParams(api_retry_limit=1, retry_initial_delay_seconds=0)
        for replies, succeeds in (([error, '{"aliases":{"BETTY":"ALICE"}}'], True),
                                  ([error, error], False)):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(generate_script, "get_response_log_path", return_value=str(Path(tmp, "response.log"))), \
                 redirect_stdout(io.StringIO()):
                client = self.client(replies)
                if succeeds:
                    aliases, _ = find_nicknames.find_nicknames(client, "model", entries, params=params)
                    self.assertEqual({"BETTY": "ALICE"}, aliases)
                else:
                    with self.assertRaisesRegex(RuntimeError, "chunk 1/1"):
                        find_nicknames.find_nicknames(client, "model", entries, params=params)
                self.assertEqual(2, client.chat.completions.create.call_count)

    def run_cli(self, tmp, config, primary, secondary=None, append=False, output=None):
        from contextlib import ExitStack
        script = Path(tmp, "script.json")
        script.write_text(json.dumps([{"speaker": "ALICE", "text": "Hello."},
                                      {"speaker": "BETTY", "text": "Good day."}]))
        aliases = Path(tmp, "aliases.json")
        if not aliases.exists():
            aliases.write_text('{"OLD":"CANONICAL"}')
        argv = ["find_nicknames.py", "--input", str(script), "--aliases-file", str(aliases)]
        if append:
            argv.append("--append")
        output = output if output is not None else io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(find_nicknames, "get_runtime_data_dir", return_value=tmp))
            stack.enter_context(patch.object(find_nicknames, "load_app_config", return_value=config))
            stack.enter_context(patch.object(find_nicknames, "ensure_ideal_settings",
                                            return_value=(False, {"loaded": True, "context_length": 8192}, "test")))
            stack.enter_context(patch.object(find_nicknames, "get_cached_or_benchmarked_concurrency", return_value=1))
            stack.enter_context(patch.object(find_nicknames, "make_llm_client", return_value=primary, create=True))
            stack.enter_context(patch.object(llm_provider, "make_llm_client",
                                            side_effect=[primary, secondary] if secondary else [primary]))
            stack.enter_context(patch.object(generate_script, "get_response_log_path",
                                            return_value=str(Path(tmp, "response.log"))))
            find_nicknames.main()
        return aliases, output

    def test_cli_uses_actual_run_factory_and_switches_to_secondary_profile(self):
        error = RuntimeError("rate limited"); error.status_code = 429
        primary = self.client([error])
        secondary = self.client(['{"aliases":{"BETTY":"ALICE"}}'])
        config = {"llm_mode": "local", "llm_failover": True,
                  "llm_local": {"base_url": "http://localhost:1234/v1", "model_name": "primary",
                                "api_retry_limit": 0, "retry_initial_delay_seconds": 0},
                  "llm_remote": {"base_url": "http://remote:1234/v1", "model_name": "secondary"}}
        with tempfile.TemporaryDirectory() as tmp:
            aliases, output = self.run_cli(tmp, config, primary, secondary)
            self.assertEqual({"BETTY": "ALICE", "OLD": "CANONICAL"},
                             json.loads(aliases.read_text()))
            self.assertIn("completed successfully", output.getvalue())
        primary.chat.completions.create.assert_called_once()
        secondary.chat.completions.create.assert_called_once()
        self.assertEqual("secondary", secondary.chat.completions.create.call_args.kwargs["model"])

    def test_cli_failure_preserves_previous_alias_file_and_does_not_report_success(self):
        error = RuntimeError("unavailable"); error.status_code = 503
        config = {"llm_local": {"model_name": "primary", "api_retry_limit": 1,
                                "retry_initial_delay_seconds": 0}}
        with tempfile.TemporaryDirectory() as tmp:
            aliases = Path(tmp, "aliases.json")
            aliases.write_text('{"OLD":"CANONICAL"}')
            before = aliases.read_bytes()
            output = io.StringIO()
            with self.assertRaisesRegex(RuntimeError, "chunk 1/1"):
                self.run_cli(tmp, config, self.client([error, error]), output=output)
            self.assertEqual(before, aliases.read_bytes())
            self.assertNotIn("completed successfully", output.getvalue())

    def test_later_chunk_failure_does_not_publish_successful_partial_aliases(self):
        error = RuntimeError("unavailable"); error.status_code = 503
        config = {"llm_local": {"model_name": "primary", "api_retry_limit": 0,
                                "retry_initial_delay_seconds": 0}}
        client = self.client(['{"aliases":{"BETTY":"ALICE"}}', error])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(find_nicknames, "_chunk_evidence", return_value=[["- first"], ["- second"]]):
            aliases = Path(tmp, "aliases.json")
            aliases.write_text('{"OLD":"CANONICAL"}')
            before = aliases.read_bytes()
            output = io.StringIO()
            with self.assertRaisesRegex(RuntimeError, "chunk 2/2"):
                self.run_cli(tmp, config, client, output=output)
            self.assertEqual(before, aliases.read_bytes())
            self.assertNotIn("completed successfully", output.getvalue())
            self.assertEqual(2, client.chat.completions.create.call_count)

    def test_valid_existing_registry_is_normalized_without_mutating_input(self):
        entries = [{"speaker": "ALICE", "text": "Hello."},
                   {"speaker": "BETTY", "text": "Good day."}]
        registry = {" ALICE ": " BETTY "}
        client = self.client(["{}"])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generate_script, "get_response_log_path", return_value=str(Path(tmp, "response.log"))), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(({}, {}), find_nicknames.find_nicknames(
                client, "model", entries, existing_aliases=registry))
        self.assertEqual({" ALICE ": " BETTY "}, registry)
        prompt = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertIn('"ALICE": "BETTY"', prompt)

    def test_batch_review_does_not_review_or_clear_checkpoint_after_nickname_failure(self):
        import asyncio
        from fastapi import BackgroundTasks
        from routers import script as routes
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp, "book.json")
            book.write_text('[{"speaker":"ALICE","text":"Keep this."}]')
            checkpoint = Path(str(book) + ".review_checkpoint.json")
            checkpoint.write_text("preserve checkpoint")
            state = {"batch_review": {"running": False, "cancel": False}}
            background = BackgroundTasks()
            import core
            with patch.object(core, "DATA_DIR", tmp), \
                 patch.object(core, "process_state", state), \
                 patch.object(core, "_task_claims", {}), \
                 patch.object(routes, "SCRIPTS_DIR", tmp), \
                 patch.object(routes, "process_state", state), \
                 patch.object(routes, "check_global_gpu_lock"), \
                 patch.object(routes, "claim_gpu_task"), \
                 patch.object(routes, "_init_task_log", return_value=str(Path(tmp, "task.log"))), \
                 patch.object(routes, "_write_batch_review_report", return_value=None), \
                 patch.object(routes, "_stream_subprocess_to_logs", return_value=(1, ["Nickname discovery failed"])) as stream:
                asyncio.run(routes.review_script_batch_start(routes.BatchReviewRequest(
                    script_names=["book"], dedupe_speakers=True, find_nicknames=True,
                    bidirectional=False), background))
                # Execute the registered ownership wrapper, including lease cleanup.
                asyncio.run(background())
                self.assertEqual({}, core._task_claims)
                self.assertFalse(state["batch_review"]["running"])
            stream.assert_called_once()
            self.assertEqual("failed", state["batch_review"]["tasks"][0]["status"])
            self.assertEqual("preserve checkpoint", checkpoint.read_text())
            self.assertEqual('[{"speaker":"ALICE","text":"Keep this."}]', book.read_text())
            self.assertTrue(any("nickname" in line.lower() and "failed" in line.lower()
                                for line in state["batch_review"]["logs"]))

    def test_bad_existing_registry_is_rejected_before_client_setup(self):
        for value in (["ALICE"], {"ALICE": 7}, {"ALICE": None}, {"ALICE": ["BETTY"]}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "script.json")
                script.write_text('[{"speaker":"ALICE","text":"Hi."}]')
                aliases = Path(tmp, "aliases.json")
                aliases.write_text(json.dumps(value))
                before = aliases.read_bytes()
                with patch.object(sys, "argv", ["nicknames", "--input", str(script), "--append",
                                                "--aliases-file", str(aliases)]), \
                     patch.object(find_nicknames, "load_app_config", side_effect=SetupReached) as setup, \
                     redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as error:
                        find_nicknames.main()
                self.assertEqual(1, error.exception.code)
                self.assertIn("alias", output.getvalue().lower())
                setup.assert_not_called()
                self.assertEqual(before, aliases.read_bytes())

    def test_direct_invalid_registry_raises_controlled_error_before_scanning(self):
        for value in (["ALICE"], {7: "BETTY"}, {"ALICE": 7}):
            with self.subTest(value=value), patch.object(find_nicknames, "collect_context") as scan:
                with self.assertRaisesRegex(ValueError, "alias"):
                    find_nicknames.find_nicknames(object(), "model", [], existing_aliases=value)
                scan.assert_not_called()

    def test_groups_on_either_side_are_rejected(self):
        speakers = ["ALICE", "BETTY", "TWINS", "RAM AND REM", "CROWD"]
        for aliases in ({"ALICE": "TWINS"}, {"ALICE": "RAM AND REM"},
                        {"TWINS": "ALICE"}, {"TWINS": "CROWD"}):
            with self.subTest(aliases=aliases), redirect_stdout(io.StringIO()):
                actual, _ = find_nicknames._parse_alias_response(json.dumps({"aliases": aliases}), speakers)
                self.assertEqual({}, actual)
        actual, _ = find_nicknames._parse_alias_response(
            json.dumps({"aliases": {"ALICE": "BETTY"}}), speakers)
        self.assertEqual({"ALICE": "BETTY"}, actual)

    def test_word_prefixes_do_not_fabricate_name_cooccurrence(self):
        entries = [{"speaker": "ANN", "text": "Hello."},
                   {"speaker": "BEAT", "text": "Good day."},
                   {"speaker": "NARRATOR", "text": "The annual report described a beating."}]
        self.assertEqual([], find_nicknames.collect_context(entries)[2])
        entries[-1]["text"] = "Ann and Beat met."
        self.assertEqual(["Ann and Beat met."], find_nicknames.collect_context(entries)[2])

    def test_apostrophe_ending_name_token_still_counts_as_a_mention(self):
        entries = [{"speaker": "JAMES'", "text": "Hello."},
                   {"speaker": "ANN", "text": "Good day."},
                   {"speaker": "NARRATOR", "text": "James' and Ann met."}]
        self.assertEqual(["James' and Ann met."], find_nicknames.collect_context(entries)[2])

    def test_bad_evidence_is_reported_and_does_not_crash_wave_merge(self):
        entries = [{"speaker": "ALICE", "text": "Hello."},
                   {"speaker": "BETTY", "text": "Good day."}]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "aliases": {"BETTY": "ALICE"}, "evidence": "oops"})), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=Mock(return_value=response))))
        with patch.object(find_nicknames, "warn_unparseable_llm_json") as warn:
            aliases, evidence = find_nicknames.find_nicknames(client, "model", entries)
        self.assertEqual({"BETTY": "ALICE"}, aliases)
        self.assertEqual({}, evidence)
        warn.assert_called_once()

    def test_valid_evidence_mapping_is_preserved(self):
        expected = {"BETTY": "Alice called her Betty."}
        aliases, evidence = find_nicknames._parse_alias_response(json.dumps({
            "aliases": {"BETTY": "ALICE"}, "evidence": expected}), ["ALICE", "BETTY"])
        self.assertEqual({"BETTY": "ALICE"}, aliases)
        self.assertEqual(expected, evidence)

    def test_cli_rejects_bad_script_shape_before_model_setup(self):
        for value in ({"speaker": "ALICE"}, [None]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "script.json")
                script.write_text(json.dumps(value))
                with patch.object(sys, "argv", ["find_nicknames.py", "--input", str(script)]), \
                     patch.object(find_nicknames, "get_runtime_data_dir", return_value=tmp), \
                     patch.object(find_nicknames, "load_app_config", side_effect=SetupReached) as load, \
                     redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as raised:
                        find_nicknames.main()
                self.assertEqual(1, raised.exception.code)
                load.assert_not_called()
                self.assertIn("JSON list", output.getvalue())

    def test_cli_accepts_entry_objects_before_model_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "script.json")
            script.write_text(json.dumps([{"speaker": "ALICE", "text": "Hi."}]))
            with patch.object(sys, "argv", ["find_nicknames.py", "--input", str(script)]), \
                 patch.object(find_nicknames, "get_runtime_data_dir", return_value=tmp), \
                 patch.object(find_nicknames, "load_app_config", side_effect=SetupReached), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(SetupReached):
                    find_nicknames.main()


if __name__ == "__main__":
    unittest.main()


class NicknameResponseShapeTests(unittest.TestCase):
    client = staticmethod(NicknameRegressionTests.client)
    run_cli = NicknameRegressionTests.run_cli

    def test_invalid_alias_container_refuses_without_overwriting_human_registry(self):
        for aliases in ([], ['BETTY'], 'BETTY', 7, 0, True, False, None):
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, 'aliases.json')
                path.write_bytes(b'{"OLD":"CANONICAL"}')
                before = path.read_bytes()
                output = io.StringIO()
                config = {'llm_local': {'model_name': 'primary', 'api_retry_limit': 0,
                                        'retry_initial_delay_seconds': 0}}
                reply = json.dumps({'aliases': aliases, 'evidence': {}})
                with self.assertRaisesRegex(ValueError, 'aliases must be an object'):
                    self.run_cli(tmp, config, self.client([reply]), output=output)
                self.assertEqual(before, path.read_bytes())
                self.assertNotIn('completed successfully', output.getvalue())

    def test_valid_empty_object_is_success_without_registry_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, output = self.run_cli(tmp, {}, self.client(['{"aliases":{}}']))
            self.assertEqual({'OLD': 'CANONICAL'}, json.loads(path.read_text()))
            self.assertIn('completed successfully', output.getvalue())


class SharedNameContextEvidenceTests(unittest.TestCase):
    def test_two_full_names_reach_discovery_but_one_ambiguous_name_is_not_two_characters(self):
        import copy
        passages = ["Harry Potter met Harry Dunbar near the gate.",
                    "Potter and Dunbar spoke quietly."]
        ambiguous = "Harry walked alone down the street."
        entries = [{"speaker": "HARRY POTTER", "text": "I will wait."},
                   {"speaker": "HARRY DUNBAR", "text": "I am here."},
                   {"speaker": "NARRATOR", "text": ambiguous}] + [
                       {"speaker": "NARRATOR", "text": text} for text in passages]
        before = copy.deepcopy(entries)
        for ordered in (entries, list(reversed(entries))):
            with self.subTest(reversed=ordered is not entries):
                speakers, samples, evidence = find_nicknames.collect_context(ordered)
                self.assertEqual(["HARRY DUNBAR", "HARRY POTTER", "NARRATOR"], speakers)
                self.assertEqual(set(passages), set(evidence))
                self.assertNotIn(ambiguous, evidence)
                with patch.object(find_nicknames, "call_llm_for_object", return_value={}) as request, \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(({}, {}), find_nicknames.find_nicknames(object(), "fixture", ordered))
                prompt = request.call_args.args[3]
                context = prompt.split("CONTEXT PASSAGES", 1)[1]
                self.assertTrue(all(text in context for text in passages))
                self.assertNotIn(ambiguous, context)
                self.assertIn('"HARRY POTTER"', prompt)
                self.assertIn('"HARRY DUNBAR"', prompt)
        self.assertEqual(before, entries)


class ReservedNarratorAliasTests(unittest.TestCase):
    @staticmethod
    def entries():
        return [{"speaker": "NARRATION", "text": "Alice watched Betty enter."},
                {"speaker": "NARRATIVE", "text": "Betty waved at Alice."},
                {"speaker": "ALICE", "text": "Hello, Betty."},
                {"speaker": "BETTY", "text": "Hello, Alice."}]

    def test_shared_review_guard_blocks_every_reserved_narrator_in_both_directions(self):
        import review_script
        for label in ("NARRATOR", "NARRATION", "NARRATIVE", "Narration", "narrative"):
            with self.subTest(label=label):
                self.assertFalse(review_script.is_speaker_merge_allowed(label, "ALICE"))
                self.assertFalse(review_script.is_speaker_merge_allowed("ALICE", label))
                self.assertEqual({}, review_script.get_resolved_speaker_merge_map({label: "ALICE"}))
                self.assertEqual({}, review_script.get_resolved_speaker_merge_map({"ALICE": label}))
        self.assertTrue(review_script.is_speaker_merge_allowed("BETTY", "ALICE"))
        self.assertFalse(review_script.is_speaker_merge_allowed("ALICE AND BETTY", "ALICE"))
        self.assertFalse(review_script.is_speaker_merge_allowed("ALICE", "UNKNOWN"))

    def test_actual_nickname_cli_publishes_character_alias_without_narrator_aliases(self):
        from contextlib import ExitStack
        proposals = {"NARRATION": "ALICE", "ALICE": "NARRATIVE", "BETTY": "ALICE"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "script.json"
            script.write_text(json.dumps(self.entries()))
            original = script.read_bytes()
            registry = root / "aliases.json"
            registry.write_text('{"OLD":"HUMAN ROOT"}')
            client = NicknameRegressionTests.client([json.dumps({"aliases": proposals})])
            output = io.StringIO()
            config = {"llm_mode": "local", "llm_local": {"model_name": "fixture"}}
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(output))
                stack.enter_context(patch.object(sys, "argv", ["find_nicknames.py", "--input", str(script),
                    "--aliases-file", str(registry)]))
                stack.enter_context(patch.object(find_nicknames, "get_runtime_data_dir", return_value=tmp))
                stack.enter_context(patch.object(find_nicknames, "load_app_config", return_value=config))
                stack.enter_context(patch.object(find_nicknames, "make_run_client", return_value=client))
                stack.enter_context(patch.object(find_nicknames, "ensure_ideal_settings",
                    return_value=(False, {"loaded": True, "context_length": 8192}, "fixture")))
                stack.enter_context(patch.object(find_nicknames, "get_cached_or_benchmarked_concurrency", return_value=1))
                stack.enter_context(patch.object(generate_script, "get_response_log_path", return_value=str(root / "responses.log")))
                find_nicknames.main()
            self.assertEqual({"OLD": "HUMAN ROOT", "BETTY": "ALICE"}, json.loads(registry.read_text()))
            self.assertEqual(original, script.read_bytes())
            self.assertIn("protected speaker mapping", output.getvalue())
            self.assertIn("completed successfully", output.getvalue())
            client.chat.completions.create.assert_called_once()
            assert_file_lock_released(str(registry))

    def test_actual_review_dedupe_ignores_protected_registry_and_model_proposals(self):
        import copy
        import review_script
        proposals = {"NARRATION": "ALICE", "ALICE": "NARRATIVE", "BETTY": "ALICE"}
        for with_registry in (False, True):
            with self.subTest(with_registry=with_registry), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "aliases.json"
                registry.write_text(json.dumps({"NARRATION": "ALICE", "ALICE": "NARRATIVE"}))
                original_registry = registry.read_bytes()
                entries = self.entries()
                before = copy.deepcopy(entries)
                client = NicknameRegressionTests.client([json.dumps(proposals)])
                with patch.object(generate_script, "get_response_log_path", return_value=str(root / "responses.log")), \
                     redirect_stdout(io.StringIO()):
                    mapping, renamed, changes = review_script.dedupe_speakers(
                        client, "fixture", entries, registry_path=str(registry) if with_registry else None)
                if with_registry:
                    # The human ALICE→NARRATIVE edge stays on disk. Automatic
                    # BETTY→ALICE must not reach that protected narrator root.
                    self.assertEqual(({}, 0, []), (mapping, renamed, changes))
                    self.assertEqual(original_registry, registry.read_bytes())
                else:
                    self.assertEqual({"BETTY": "ALICE"}, mapping)
                    self.assertEqual(1, renamed)
                    self.assertEqual([(3, "speaker", "ALICE")], changes)
                    self.assertEqual(original_registry, registry.read_bytes())
                self.assertEqual(before, entries)



class NicknameAliasGraphSafetyTests(unittest.TestCase):
    def test_actual_discovery_rejects_cycles_across_and_within_waves_and_resolves_chains(self):
        from unittest.mock import patch
        entries=[{'speaker':name,'text':'Line '+name} for name in ('A','B','C','D')]
        cases=[([{'A':'B','B':'A','C':'A'}],1,{}),
               ([{'A':'B'},{'B':'A'}],1,{}),
               ([{'A':'B'},{'B':'A'},{'C':'A'}],3,{}),
               ([{'A':'B'},{'B':'C'}],1,{'A':'C','B':'C'}),
               ([{'A':'B'},{'a':'C'}],2,{})]
        for proposals,concurrency,expected in cases:
            with self.subTest(proposals=proposals,concurrency=concurrency),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);script=root/'script.json';registry=root/'aliases.json'
                script.write_text(json.dumps(entries));source_before=script.read_bytes()
                registry.write_text('{}')
                with patch.object(find_nicknames,'_chunk_evidence',return_value=[[] for _ in proposals]), \
                     patch.object(find_nicknames,'call_llm_for_object',side_effect=[{'aliases':p} for p in proposals]):
                    aliases,_=find_nicknames.find_nicknames(object(),'fixture-model',json.loads(script.read_text()),concurrency=concurrency)
                self.assertEqual(expected,aliases)
                saved=find_nicknames.save_discovered_aliases(str(registry),aliases,roster=["A","B","C","D"])
                self.assertEqual(expected,saved)
                if expected:
                    self.assertEqual(expected,json.loads(registry.read_text()))
                self.assertEqual(source_before,script.read_bytes())

    def test_parser_refuses_unknown_canonical_and_retains_roster_chains(self):
        cases=[({'A':'INVENTED'},{}),({'A':'B','a':'C'},{}),({'A':'B','B':'C'},{'A':'C','B':'C'}),
               ({'A':'B','B':'A','C':'A'},{}),({'A':'UNKNOWN'},{}),
               ({'A':'A AND B'},{}),({'A AND B':'A'}, {})]
        for proposal,expected in cases:
            with self.subTest(proposal=proposal):
                actual,_=find_nicknames._parse_alias_response(json.dumps({'aliases':proposal}),['A','B','C','A AND B'])
                self.assertEqual(expected,actual)

    def test_locked_publication_keeps_normalized_human_keys_and_resolves_human_roots(self):
        for human,proposals,expected in (
                ({'Alice':'Betty'},{'A-L-I-C-E':'Betty'},{'Alice':'Betty'}),
                ({'A':'B'},{'B':'A'},{'A':'B'}),
                ({'Beth':'Elizabeth'},{'Liz':'Beth'},{'Beth':'Elizabeth','Liz':'Elizabeth'}),
                ({'Human':'APPROVED_ROOT'},{'Auto':'APPROVED_ROOT'},{'Human':'APPROVED_ROOT','Auto':'APPROVED_ROOT'}),
                ({},{'Auto':'UNAPPROVED_ROOT'},{})):
            with self.subTest(human=human,proposals=proposals),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp,'aliases.json');path.write_text(json.dumps(human,indent=3)+'\n')
                before=path.read_bytes();original_proposals=json.loads(json.dumps(proposals))
                result=find_nicknames.save_discovered_aliases(str(path),proposals)
                self.assertEqual(expected,result)
                self.assertEqual(expected,json.loads(path.read_text()))
                if expected==human:self.assertEqual(before,path.read_bytes())
                self.assertEqual(original_proposals,proposals)
                assert_file_lock_released(str(path))

    def test_invalid_human_graph_is_refused_without_changing_registry_bytes(self):
        for human in ({'A':'B','B':'A'},{'A':'B','a':'C'},{'A':None},['A']):
            with self.subTest(human=human),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp,'aliases.json');path.write_text(json.dumps(human,indent=3)+'\n');before=path.read_bytes()
                with self.assertRaises(ValueError):
                    find_nicknames.save_discovered_aliases(str(path),{'Auto':'A'})
                self.assertEqual(before,path.read_bytes())
                assert_file_lock_released(str(path))

    def test_out_of_book_human_root_is_available_to_real_discovery_without_prompt_bloat(self):
        entries=[{'speaker':'Liz','text':'Hello.'},{'speaker':'OTHER','text':'Reply.'}]
        human={'Beth':'Elizabeth'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'aliases.json');path.write_text(json.dumps(human));before=path.read_bytes()
            with patch.object(find_nicknames,'call_llm_for_object',return_value={'aliases':{'Liz':'Elizabeth'}}) as call:
                aliases,_=find_nicknames.find_nicknames(object(),'fixture-model',entries,existing_aliases=human)
            self.assertEqual({'Liz':'Elizabeth'},aliases)
            self.assertNotIn('"Beth":',call.call_args.args[3])
            self.assertEqual(before,path.read_bytes())
            saved=find_nicknames.save_discovered_aliases(str(path),aliases)
            self.assertEqual({'Beth':'Elizabeth','Liz':'Elizabeth'},saved)


    def test_latest_human_edit_during_discovery_cannot_be_closed_into_an_automatic_cycle(self):
        from utils import atomic_json_write,file_lock
        entries=[{'speaker':'A','text':'First.'},{'speaker':'B','text':'Second.'}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'aliases.json');path.write_text('{}')
            existing=find_nicknames.get_existing_aliases(str(path))
            def reply(*args,**kwargs):
                # A human edit lands after discovery captured its initial registry.
                with file_lock(str(path)):
                    atomic_json_write({'A':'B'},str(path))
                return {'aliases':{'B':'A'}}
            with patch.object(find_nicknames,'call_llm_for_object',side_effect=reply):
                aliases,_=find_nicknames.find_nicknames(object(),'fixture-model',entries,existing_aliases=existing)
            self.assertEqual({'B':'A'},aliases)
            human_bytes=path.read_bytes()
            saved=find_nicknames.save_discovered_aliases(str(path),aliases,roster=['A','B'])
            self.assertEqual({'A':'B'},saved)
            self.assertEqual(human_bytes,path.read_bytes())
            self.assertEqual({},existing)
            assert_file_lock_released(str(path))

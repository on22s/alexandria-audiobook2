"""Manual transport (#593): the user is the model. The client hands each
request to the data dir and waits; the endpoints are how the reply comes
back; the pipeline's own validation gates it."""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
import llm_provider
from llm_provider import ManualClient, make_llm_client
from routers import script


def _answer_when_pending(client, content, mismatch_first=False, previous_id=None, seen=None):
    """A thread standing in for the user: waits for pending.json, replies."""
    from utils import atomic_json_write, file_lock, safe_load_json
    stopped = threading.Event()
    failures = []
    def run():
        try:
            for _ in range(200):
                if stopped.wait(.02):
                    return
                if not os.path.isdir(client.dir):
                    continue
                with file_lock(client.pending_path):
                    req = safe_load_json(client.pending_path, None)
                    if isinstance(req, dict) and req.get("id") and req["id"] != previous_id:
                        break
            else:
                raise TimeoutError("manual fixture did not receive a pending request")
            if seen is not None:
                seen.update(req)
            if mismatch_first:
                with file_lock(client.pending_path):
                    atomic_json_write({"id": "not-this-one", "content": "stale"}, client.response_path)
                if stopped.wait(.1):
                    return
            with file_lock(client.pending_path):
                atomic_json_write({"id": req["id"], "content": content}, client.response_path)
        except Exception as error:
            failures.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread._manual_reply_stop = stopped
    thread._manual_reply_failures = failures
    thread.start()
    return thread


@contextlib.contextmanager
def _answer_pending(client, content, **kwargs):
    thread = _answer_when_pending(client, content, **kwargs)
    try:
        yield thread
    finally:
        thread._manual_reply_stop.set()
        thread.join()
        if thread._manual_reply_failures:
            raise AssertionError("manual reply fixture failed") from thread._manual_reply_failures[0]


class ManualClientTests(unittest.TestCase):

    def test_unreadable_pending_preserves_live_owner_and_answerability(self):
        from pathlib import Path
        from manual_llm_context import apply_manual_cancellation
        from utils import file_lock
        with tempfile.TemporaryDirectory() as directory:
            first = ManualClient(directory)
            second = ManualClient(directory)
            first.POLL_SECONDS = second.POLL_SECONDS = .01
            stopped = threading.Event()
            answers = {}
            failures = []
            def request_first():
                try:
                    with apply_manual_cancellation(stopped.is_set):
                        answers['first'] = first.create(model='fixture', messages=[]).choices[0].message.content
                except Exception as error:
                    failures.append(error)
            worker = threading.Thread(target=request_first, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 3
                while not os.path.exists(first.pending_path) and time.monotonic() < deadline:
                    time.sleep(.01)
                with file_lock(first.pending_path):
                    original = Path(first.pending_path).read_bytes()
                pending = json.loads(original)
                original_open = open
                def fail_pending_read(path, *args, **kwargs):
                    if os.fspath(path) == first.pending_path:
                        raise PermissionError('synthetic sharing violation')
                    return original_open(path, *args, **kwargs)
                deadline = time.monotonic() + 1
                with patch('builtins.open', side_effect=fail_pending_read), \
                     apply_manual_cancellation(lambda: time.monotonic() >= deadline):
                    with self.assertRaises(core.AppError) as raised:
                        second.create(model='fixture', messages=[])
                self.assertEqual(503, raised.exception.status_code)
                self.assertEqual('error.manual_llm.pending_read_failed', raised.exception.message_key)
                self.assertEqual(original, Path(first.pending_path).read_bytes())
                self.assertTrue(llm_provider.is_manual_request_owner_alive(pending))
                with patch.object(script, 'DATA_DIR', directory):
                    accepted = asyncio.run(script.manual_llm_response(
                        script.ManualReply(id=pending['id'], content='Still answerable')))
                self.assertTrue(accepted['accepted'])
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual({'first': 'Still answerable'}, answers)
                self.assertEqual([], failures)
                self.assertFalse(os.path.exists(first.pending_path))
            finally:
                stopped.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())

    def test_corrupt_pending_is_not_replaceable(self):
        from pathlib import Path
        from manual_llm_context import apply_manual_cancellation
        for contents in (b'{', b'null', b'[]', b'{}', b'{"id": ""}', b'\xff'):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                client = ManualClient(directory)
                os.makedirs(client.dir, exist_ok=True)
                Path(client.pending_path).write_bytes(contents)
                deadline = time.monotonic() + 1
                with apply_manual_cancellation(lambda: time.monotonic() >= deadline):
                    with self.assertRaises(core.AppError) as raised:
                        client.create(model='fixture', messages=[])
                self.assertEqual(503, raised.exception.status_code)
                self.assertEqual('error.manual_llm.pending_corrupt', raised.exception.message_key)
                self.assertIn('manual_llm/pending.json', raised.exception.detail)
                self.assertIn('Cancel the manual request (or restart the app)', raised.exception.detail)
                self.assertIn('delete or rename this file', raised.exception.detail)
                self.assertNotIn(directory, raised.exception.detail)
                self.assertEqual(contents, Path(client.pending_path).read_bytes())
                self.assertFalse(os.path.exists(client.response_path))

    def test_cleanup_storage_error_preserves_original_failure_and_logs_request(self):
        from pathlib import Path
        for failure in (PermissionError('synthetic sharing violation'), ValueError('synthetic corrupt JSON')):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                client = ManualClient(directory)
                primary = RuntimeError('synthetic body failure')
                observed = {}
                original_open = open
                def fail_reads(path, *args, **kwargs):
                    if os.fspath(path) == client.response_path:
                        observed['bytes'] = Path(client.pending_path).read_bytes()
                        observed['pending'] = json.loads(observed['bytes'])
                        raise primary
                    if os.fspath(path) == client.pending_path and observed:
                        raise failure
                    return original_open(path, *args, **kwargs)
                with patch('builtins.open', side_effect=fail_reads), \
                     self.assertLogs('AlexandriaUI', level='ERROR') as logs:
                    with self.assertRaises(RuntimeError) as raised:
                        client.create(model='fixture', messages=[])
                self.assertIs(primary, raised.exception)
                self.assertEqual(observed['bytes'], Path(client.pending_path).read_bytes())
                self.assertFalse(llm_provider.is_manual_request_owner_alive(observed['pending']))
                self.assertIn(observed['pending']['id'], '\n'.join(logs.output))
                expected_key = ('error.manual_llm.pending_read_failed' if isinstance(failure, OSError)
                                else 'error.manual_llm.pending_corrupt')
                self.assertIn(expected_key, '\n'.join(logs.output))

    def test_cleanup_storage_error_after_success_is_logged_and_still_raised(self):
        import io
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            client = ManualClient(directory)
            observed = {}
            original_open = open
            def answer_then_fail_cleanup(path, *args, **kwargs):
                if os.fspath(path) == client.response_path:
                    observed['bytes'] = Path(client.pending_path).read_bytes()
                    observed['pending'] = json.loads(observed['bytes'])
                    return io.StringIO(json.dumps({'id': observed['pending']['id'], 'content': 'Answer'}))
                if os.fspath(path) == client.pending_path and observed:
                    raise PermissionError('synthetic cleanup sharing violation')
                return original_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=answer_then_fail_cleanup), \
                 self.assertLogs('AlexandriaUI', level='ERROR') as logs:
                with self.assertRaises(core.AppError) as raised:
                    client.create(model='fixture', messages=[])
            self.assertEqual('error.manual_llm.pending_read_failed', raised.exception.message_key)
            self.assertEqual(observed['bytes'], Path(client.pending_path).read_bytes())
            self.assertFalse(llm_provider.is_manual_request_owner_alive(observed['pending']))
            self.assertIn(observed['pending']['id'], '\n'.join(logs.output))

    def test_failed_atomic_replacement_preserves_stale_pending(self):
        import errno
        from pathlib import Path
        from utils import atomic_json_write
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            os.makedirs(client.dir)
            stale = {"id": "abandoned", "owner_pid": 99999999, "owner_thread": 1}
            atomic_json_write(stale, client.pending_path)
            original = Path(client.pending_path).read_bytes()
            with patch("os.replace", side_effect=OSError(errno.EIO, "synthetic replace failure")):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    client.create(model="fixture", messages=[])
            self.assertEqual(original, Path(client.pending_path).read_bytes())
            self.assertEqual([], list(Path(client.dir).glob(".tmp_*")))

    def test_stale_replacement_waits_for_pending_reader_to_close(self):
        import builtins
        from utils import atomic_json_write, file_lock
        reader_open = threading.Event()
        release_reader = threading.Event()
        writer_waiting = threading.Event()
        mutated = threading.Event()
        cancelled = threading.Event()
        failures = []
        results = {}
        real_open = builtins.open
        real_remove = os.remove
        real_replace = os.replace
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            client.POLL_SECONDS = .01
            os.makedirs(client.dir)
            stale = {"id": "abandoned", "owner_pid": 99999999, "owner_thread": 1}
            atomic_json_write(stale, client.pending_path)

            @contextlib.contextmanager
            def hold_reader(path, *args, **kwargs):
                with real_open(path, *args, **kwargs) as handle:
                    if (os.fspath(path) == client.pending_path
                            and threading.current_thread() is reader):
                        reader_open.set()
                        if not release_reader.wait(3):
                            raise TimeoutError("pending reader was not released")
                    yield handle

            @contextlib.contextmanager
            def observe_lock(path, *args, **kwargs):
                if threading.current_thread() is writer:
                    writer_waiting.set()
                with file_lock(path, *args, **kwargs):
                    yield

            def remove(path, *args, **kwargs):
                if os.fspath(path) == client.pending_path:
                    mutated.set()
                return real_remove(path, *args, **kwargs)

            def replace(source, target, *args, **kwargs):
                if os.fspath(target) == client.pending_path:
                    mutated.set()
                return real_replace(source, target, *args, **kwargs)

            def read():
                try:
                    results["stale"] = script.read_manual_pending()
                except Exception as error:
                    failures.append(error)

            def create():
                try:
                    with llm_provider.apply_manual_cancellation(cancelled.is_set):
                        results["reply"] = client.create(model="fixture", messages=[])
                except llm_provider.ManualRequestCancelled:
                    pass
                except Exception as error:
                    failures.append(error)

            reader = threading.Thread(target=read)
            writer = threading.Thread(target=create)
            with patch.object(script, "DATA_DIR", tmp), \
                 patch("builtins.open", side_effect=hold_reader), \
                 patch("utils.file_lock", side_effect=observe_lock), \
                 patch.object(script, "file_lock", side_effect=observe_lock), \
                 patch("os.remove", side_effect=remove), \
                 patch("os.replace", side_effect=replace):
                reader.start()
                try:
                    self.assertTrue(reader_open.wait(3))
                    writer.start()
                    self.assertTrue(writer_waiting.wait(3))
                    self.assertFalse(mutated.wait(.15), "writer mutated an open pending reader")
                    release_reader.set()
                    reader.join(3)
                    self.assertFalse(reader.is_alive())
                    deadline = time.monotonic() + 3
                    pending = None
                    while time.monotonic() < deadline:
                        pending = script.read_manual_pending()
                        if pending:
                            break
                        time.sleep(.01)
                    self.assertIsNotNone(pending)
                    self.assertNotEqual(stale["id"], pending["id"])
                    with file_lock(client.pending_path):
                        atomic_json_write({"id": pending["id"], "content": "fresh"}, client.response_path)
                    writer.join(3)
                    self.assertFalse(writer.is_alive())
                    self.assertEqual([], failures)
                    self.assertIsNone(results["stale"])
                    self.assertEqual("fresh", results["reply"].choices[0].message.content)
                    self.assertFalse(os.path.exists(client.pending_path))
                    self.assertFalse(os.path.exists(client.response_path))
                finally:
                    release_reader.set()
                    cancelled.set()
                    reader.join(3)
                    if writer.ident is not None:
                        writer.join(3)
                    self.assertFalse(reader.is_alive())
                    self.assertFalse(writer.is_alive())

    def test_review_portion_reassignment_preserves_metadata_through_manual_transport(self):
        from contextlib import redirect_stdout
        import io
        from generate_script import LLMGenParams
        from review_script import review_batch
        source = [{'speaker': 'NARRATOR', 'text': 'Rain fell. Please open the gate.',
                   'instruct': '', 'spoken': False, 'source_span': [0, 32]},
                  {'speaker': 'ALICE', 'text': 'Take the blue cup.', 'instruct': '',
                   'spoken': True, 'source_span': [34, 52], 'pause_after': 700}]
        reply = [{'speaker': 'NARRATOR', 'text': 'Rain fell.', 'instruct': ''},
                 {'speaker': 'ALICE', 'text': 'Please open the gate. Take the blue cup.', 'instruct': ''}]
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            client = ManualClient(directory); client.POLL_SECONDS = .01
            with _answer_pending(client, json.dumps(reply)):
                output = review_batch(client, 'manual', source, 1, 1, LLMGenParams(), max_retries=0,
                    source_text='Rain fell. Please open the gate.\n\nTake the blue cup.')
            self.assertEqual([{**source[0], 'text': 'Rain fell.', 'source_span': [0, 10]},
                              {**source[0], 'text': 'Please open the gate.', 'speaker': 'ALICE',
                               'spoken': True, 'source_span': [11, 32]},
                              source[1]], output)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))

    def test_cancelled_call_does_not_bind_cancellation_to_a_reused_client(self):
        from llm_provider import apply_manual_cancellation, ManualRequestCancelled
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp);client.POLL_SECONDS = .01
            with apply_manual_cancellation(lambda: True):
                with self.assertRaises(ManualRequestCancelled):
                    client.create(model='fixture',messages=[])
            self.assertFalse(os.path.exists(client.pending_path))
            with _answer_pending(client, 'fresh response'):
                response = client.create(model='fixture',messages=[])
            self.assertEqual('fresh response',response.choices[0].message.content)
            self.assertFalse(os.path.exists(client.pending_path))

    def test_exception_releases_owned_request_and_thread_ident_reuse_cannot_revive_it(self):
        from pathlib import Path
        from types import SimpleNamespace
        import builtins
        real_open = builtins.open
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp); client.POLL_SECONDS = .01
            observed = []
            def fail_response(path, *args, **kwargs):
                if os.path.abspath(os.fspath(path)) == os.path.abspath(client.response_path):
                    observed.append(json.loads(Path(client.pending_path).read_text()))
                    raise PermissionError('synthetic response-read failure')
                return real_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=fail_response):
                with self.assertRaisesRegex(PermissionError, 'response-read'):
                    client.create(model='fixture', messages=[])
            abandoned = observed[0]
            unrelated = SimpleNamespace(ident=abandoned['owner_thread'], is_alive=lambda: True)
            with patch.object(llm_provider.threading, 'enumerate', return_value=[unrelated]):
                self.assertFalse(llm_provider.is_manual_request_owner_alive(abandoned))
            self.assertFalse(os.path.exists(client.pending_path))
            with _answer_pending(client, 'fresh response'):
                result = client.create(model='fixture', messages=[])
            self.assertEqual('fresh response', result.choices[0].message.content)
            self.assertFalse(os.path.exists(client.pending_path))

    def test_posix_probe_does_not_terminate_real_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        identity = llm_provider.get_manual_process_identity(child.pid)
        try:
            self.assertTrue(llm_provider.is_manual_request_owner_alive({'owner_pid': child.pid, 'owner_process_identity': identity}))
            self.assertIsNone(child.poll())
        finally:
            child.terminate(); child.wait(timeout=3)
        self.assertFalse(llm_provider.is_manual_request_owner_alive({'owner_pid': child.pid, 'owner_process_identity': identity}))

    def test_windows_owner_probe_queries_birth_time_without_sending_a_signal(self):
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        from unittest.mock import Mock
        def read_times(_handle, created, *_rest):
            ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents.dwLowDateTime = 42
            return 1
        for wait_result, alive in ((258, True), (0, False)):
            with self.subTest(wait_result=wait_result):
                api = SimpleNamespace(OpenProcess=Mock(return_value=123),
                    WaitForSingleObject=Mock(return_value=wait_result),
                    GetProcessTimes=Mock(side_effect=read_times), CloseHandle=Mock(return_value=1))
                with patch.object(llm_provider.os, 'name', 'nt'), \
                     patch.object(ctypes, 'WinDLL', return_value=api, create=True), \
                     patch.object(llm_provider.os, 'kill', side_effect=AssertionError('destructive signal')):
                    self.assertEqual(alive, llm_provider.is_manual_request_owner_alive(
                        {'owner_pid':98765, 'owner_process_identity':'windows:42'}))
                api.OpenProcess.assert_called_once_with(0x101000, False, 98765)
                api.WaitForSingleObject.assert_called_once_with(123, 0)
                api.CloseHandle.assert_called_once_with(123)
        for error in (87, 5, 6):
            api = SimpleNamespace(OpenProcess=Mock(return_value=0), WaitForSingleObject=Mock(),
                                  GetProcessTimes=Mock(), CloseHandle=Mock())
            with patch.object(llm_provider.os, 'name', 'nt'), \
                 patch.object(ctypes, 'WinDLL', return_value=api, create=True), \
                 patch.object(ctypes, 'get_last_error', return_value=error, create=True):
                if error == 87:
                    self.assertIsNone(llm_provider.get_manual_process_identity(98765))
                else:
                    with self.assertRaises(OSError):
                        llm_provider.get_manual_process_identity(98765)
            api.GetProcessTimes.assert_not_called(); api.CloseHandle.assert_not_called()
        for wait_result, read_result in ((0xffffffff, 1), (258, 0)):
            api = SimpleNamespace(OpenProcess=Mock(return_value=123),
                WaitForSingleObject=Mock(return_value=wait_result), GetProcessTimes=Mock(return_value=read_result),
                CloseHandle=Mock(return_value=1))
            with patch.object(llm_provider.os, 'name', 'nt'), \
                 patch.object(ctypes, 'WinDLL', return_value=api, create=True), \
                 patch.object(ctypes, 'get_last_error', return_value=6, create=True):
                with self.assertRaises(OSError):
                    llm_provider.get_manual_process_identity(98765)
            api.CloseHandle.assert_called_once_with(123)

    def test_non_object_replies_keep_request_pending_until_valid_reply(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            bad_replies = [None, "bad", ["bad"]]
            pending_id = None
            polls = 0

            def publish_response(_seconds):
                nonlocal pending_id, polls
                polls += 1
                self.assertLessEqual(polls, 4, "client never accepted the valid response")
                self.assertFalse(os.path.exists(client.response_path))
                with open(client.pending_path) as handle:
                    pending = json.load(handle)
                if pending_id is None:
                    pending_id = pending["id"]
                self.assertEqual(pending_id, pending["id"])
                reply = bad_replies.pop(0) if bad_replies else {"id": pending_id, "content": "valid"}
                with open(client.response_path, "w") as handle:
                    json.dump(reply, handle)

            output = io.StringIO()
            with patch.object(llm_provider.time, "sleep", side_effect=publish_response), \
                 redirect_stdout(output):
                response = client.create(model="test", messages=[])
            self.assertEqual("valid", response.choices[0].message.content)
            self.assertEqual(4, polls)
            self.assertEqual(3, output.getvalue().count("JSON object"))
            self.assertEqual(1, client.sequence)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))


    def test_dead_request_owner_is_replaced_when_a_new_request_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            client.POLL_SECONDS = 0.01
            os.makedirs(client.dir, exist_ok=True)
            with open(client.pending_path, "w", encoding="utf-8") as target:
                json.dump({"id": "abandoned", "owner_pid": 99999999,
                           "owner_thread": 1}, target)
            with open(client.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": "abandoned", "content": "stale"}, target)
            with _answer_pending(client, "fresh", previous_id="abandoned"):
                answer = client.create(model="m", messages=[])
            self.assertEqual("fresh", answer.choices[0].message.content)
            self.assertFalse(os.path.exists(client.pending_path))

    def test_second_client_waits_without_erasing_first_pending_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ManualClient(tmp)
            first.POLL_SECONDS = 0.01
            answers = {}
            one = threading.Thread(target=lambda: answers.update(
                first=first.create(model="m", messages=[{"role": "user", "content": "first"}])
                .choices[0].message.content), daemon=True)
            one.start()
            for _ in range(200):
                if os.path.exists(first.pending_path):
                    break
                time.sleep(0.01)
            with open(first.pending_path, encoding="utf-8") as source:
                first_request = json.load(source)

            with patch("utils.get_runtime_data_dir", return_value=tmp):
                second = make_llm_client({"transport": "manual"}, 10)
            second.POLL_SECONDS = 0.01
            with open(first.pending_path, encoding="utf-8") as source:
                self.assertEqual(first_request, json.load(source))

            two = threading.Thread(target=lambda: answers.update(
                second=second.create(model="m", messages=[{"role": "user", "content": "second"}])
                .choices[0].message.content), daemon=True)
            two.start()
            time.sleep(0.05)
            with open(first.pending_path, encoding="utf-8") as source:
                self.assertEqual(first_request["id"], json.load(source)["id"])
            self.assertTrue(two.is_alive())

            with open(first.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": first_request["id"], "content": "one"}, target)
            one.join(2)
            self.assertFalse(one.is_alive())
            second_request = None
            for _ in range(200):
                try:
                    with open(second.pending_path, encoding="utf-8") as source:
                        pending = json.load(source)
                    if pending["id"] != first_request["id"]:
                        second_request = pending
                        break
                except FileNotFoundError:
                    pass
                time.sleep(0.01)
            self.assertIsNotNone(second_request)
            with open(second.response_path, "w", encoding="utf-8") as target:
                json.dump({"id": second_request["id"], "content": "two"}, target)
            two.join(2)
            self.assertFalse(two.is_alive())
            self.assertEqual({"first": "one", "second": "two"}, answers)

    def test_request_is_written_reply_is_returned_and_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            with _answer_pending(client, '[{"n": 0, "speaker": "ELENA"}]'):
                response = client.chat.completions.create(
                    model="m", messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                    temperature=0, max_tokens=500, response_format={"type": "json_schema"})
            self.assertEqual('[{"n": 0, "speaker": "ELENA"}]', response.choices[0].message.content)
            self.assertEqual("stop", response.choices[0].finish_reason)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))

    def test_the_request_file_carries_the_exact_messages_and_a_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            seen = {}

            with _answer_pending(client, "ok", seen=seen):
                client.create(model="m", messages=[{"role": "user", "content": "hello"}], temperature=0.1)
            self.assertEqual([{"role": "user", "content": "hello"}], seen["messages"])
            self.assertEqual(1, seen["sequence"])
            self.assertEqual(0.1, seen["params"]["temperature"])
            self.assertFalse(seen["params"]["json_schema"])
            with _answer_pending(client, "two"):
                client.create(model="m", messages=[])
            self.assertEqual(2, client.sequence)

    def test_pending_artifact_preserves_completion_contract_and_schema(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            schema = {"type": "json_schema", "json_schema": {
                "name": "annotation", "strict": True, "schema": {
                    "type": "object", "properties": {"speaker": {"type": "string"}},
                    "required": ["speaker"], "additionalProperties": False}}}
            kwargs = {"model": "o3", "messages": [{"role": "user", "content": "hello"}],
                      "max_completion_tokens": 512, "response_format": schema,
                      "top_p": 0.8, "seed": 123,
                      "extra_body": {"reasoning_effort": "high", "top_k": 20},
                      "timeout": object(), "extra_headers": {"Authorization": "secret"}}
            original = copy.deepcopy({k: v for k, v in kwargs.items() if k != "timeout"})
            seen = {}

            def answer(_seconds):
                self.assertFalse(seen, "request did not accept the matching reply")
                with open(client.pending_path, encoding="utf-8") as handle:
                    seen.update(json.load(handle))
                with open(client.response_path, "w", encoding="utf-8") as handle:
                    json.dump({"id": seen["id"], "content": '{"speaker":"NARRATOR"}'}, handle)

            with patch.object(llm_provider.time, "sleep", side_effect=answer):
                result = client.create(**kwargs)
            for key in ("max_completion_tokens", "response_format", "top_p", "seed", "extra_body"):
                self.assertEqual(kwargs[key], seen["params"][key])
            self.assertTrue(seen["params"]["json_schema"])
            self.assertNotIn("timeout", seen["params"])
            self.assertNotIn("extra_headers", seen["params"])
            self.assertNotIn("messages", seen["params"])
            self.assertEqual(kwargs["messages"], seen["messages"])
            self.assertEqual('{"speaker":"NARRATOR"}', result.choices[0].message.content)
            self.assertEqual(original, {k: v for k, v in kwargs.items() if k != "timeout"})
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))

    def test_a_reply_to_another_request_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            with _answer_pending(client, "right", mismatch_first=True):
                response = client.create(model="m", messages=[])
            self.assertEqual("right", response.choices[0].message.content)

    def test_dispatch_returns_the_manual_client_only_for_that_transport(self):
        self.assertIsInstance(make_llm_client({"transport": "manual", "base_url": "http://x/v1",
                                               "api_key": "k", "model_name": "m"}, 10), ManualClient)
        self.assertIsInstance(make_llm_client({"base_url": "http://localhost:1234/v1",
                                               "api_key": "k", "model_name": "m"}, 10),
                              llm_provider.ConfiguredOpenAI)
        manual = make_llm_client({"transport": "manual"}, 10)
        self.assertIs(manual, manual.with_options(timeout=5))

    def test_a_manual_profile_is_never_on_this_gpu(self):
        config = {"llm_mode": "local", "llm": {"base_url": "http://localhost:1234/v1", "api_key": "k",
                                                 "model_name": "m", "transport": "manual"}}
        with patch.object(core, "load_app_config", return_value=config):
            self.assertFalse(core.llm_is_on_this_gpu())
        config["llm"]["transport"] = "http"
        with patch.object(core, "load_app_config", return_value=config):
            self.assertTrue(core.llm_is_on_this_gpu())


class ManualThreePassTests(unittest.TestCase):
    def test_a_three_pass_run_completes_with_a_person_answering_every_request(self):
        """Run the native file-reply fixture in an owned, bounded CPU child."""
        code = (
            "import faulthandler; faulthandler.dump_traceback_later(8, exit=True); "
            "from tests.test_manual_llm import ManualThreePassTests; "
            "ManualThreePassTests()._run_manual_three_pass_fixture()"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(os.path.abspath(path) for path in sys.path)}
        try:
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            self.fail(f"Manual three-pass fixture timed out: {error.stdout!r} {error.stderr!r}")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def _run_manual_three_pass_fixture(self):
        """No HTTP: each request lands in the data dir, a stand-in user replies,
        the pipeline validates and moves on. Same three answers as the fake
        client fixtures elsewhere, delivered through the files."""
        import three_pass_generate as tp
        from generate_script import LLMGenParams
        from utils import atomic_json_write, file_lock, safe_load_json
        source = "Elena entered the cold room. \"Tell me the truth.\""
        answers = [
            json.dumps([{"type": "NARRATOR", "text": "Elena entered the cold room."},
                        {"type": "SPOKEN", "text": "Tell me the truth."}]),
            json.dumps([{"n": 0, "head": "Elena entered the", "speaker": "NARRATOR"},
                        {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]),
            json.dumps([{"n": 0, "head": "Elena entered the", "instruct": "Cold, still narration."},
                        {"n": 1, "head": "Tell me the", "instruct": "Firm, quiet demand."}]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            client.POLL_SECONDS = .01
            sequences = []
            stopped = threading.Event()
            failures = []

            def person():
                try:
                    answered = None
                    for content in answers:
                        for _ in range(500):
                            if stopped.wait(.02):
                                return
                            if not os.path.isdir(client.dir):
                                continue
                            with file_lock(client.pending_path):
                                request = safe_load_json(client.pending_path, None)
                                if isinstance(request, dict) and request.get("id") != answered:
                                    sequences.append(request["sequence"])
                                    answered = request["id"]
                                    atomic_json_write({"id": answered, "content": content}, client.response_path)
                                    break
                        else:
                            raise TimeoutError("manual three-pass fixture did not receive a pending request")
                except Exception as error:
                    failures.append(error)
                    stopped.set()
            reply_thread = threading.Thread(target=person, daemon=True)
            reply_thread.start()
            try:
                with llm_provider.apply_manual_cancellation(stopped.is_set):
                    entries = tp.run_three_pass(client, "m", source,
                                                LLMGenParams(max_tokens=500, temperature=0.1, segmentation="llm"),
                                                chunk_size=6000)
            finally:
                stopped.set()
                reply_thread.join()
                self.assertEqual([], failures)
            self.assertFalse(os.path.exists(client.pending_path))
            self.assertFalse(os.path.exists(client.response_path))
        self.assertEqual(["Elena entered the cold room.", "Tell me the truth."],
                         [entry["text"] for entry in entries])
        self.assertTrue(all(entry.get("instruct") for entry in entries))
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in entries])
        self.assertEqual([1, 2, 3], sequences)


class ManualEndpointTests(unittest.TestCase):
    def test_review_stage_hint_drops_previous_batch_validation_error(self):
        for marker in ('Reviewing batch 3/5 (2 entries)...',
                       'Reviewing batches 3-4/5 (2 concurrent requests)...'):
            with self.subTest(marker=marker):
                self.assertEqual(marker, script._stage_hint([
                    'Reviewing batch 2/5 (2 entries)...',
                    'Warning: BATCH 2/5 failed quality validation (attempt 1): invalid entries',
                    'Retrying... (attempt 2 of 3)', marker]))

    def test_review_stage_hint_preserves_new_validation_error(self):
        marker = 'Reviewing batch 3/5 (2 entries)...'
        warning = 'Warning: BATCH 3/5 failed quality validation (attempt 1): invalid entries'
        retry = 'Retrying... (attempt 2 of 3)'
        self.assertEqual(' · '.join((marker, warning, retry)),
                         script._stage_hint([marker, warning, retry]))

    def test_pending_and_response_round_trip_with_stage_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            state = script.process_state["script"]
            original = dict(state)
            try:
                state.update({"running": True, "logs": [
                    "Step 2 (speakers): window 2 of 4 - asking the model",
                    "Warning: ATTRIBUTE failed quality validation (attempt 1): expected 4 entries, got 2",
                    "Retrying... (attempt 2 of 4)"]})
                with patch.object(script, "DATA_DIR", tmp):
                    self.assertIsNone(asyncio.run(script.manual_llm_pending())["pending"])
                    with self.assertRaises(core.AppError) as ctx:
                        asyncio.run(script.manual_llm_response(script.ManualReply(id="x", content="y")))
                    self.assertEqual(409, ctx.exception.status_code)
                    self.assertEqual("No request is waiting for a reply.", ctx.exception.detail)
                    self.assertEqual("error.manual_llm.no_pending_request", ctx.exception.message_key)
                    self.assertFalse(os.path.exists(client.dir))
                    done = {}
                    t = threading.Thread(target=lambda: done.update(
                        text=client.create(model="m", messages=[{"role": "user", "content": "Q"}])
                        .choices[0].message.content), daemon=True)
                    t.start()
                    for _ in range(200):
                        if os.path.exists(client.pending_path):
                            break
                        time.sleep(0.02)
                    status = asyncio.run(script.get_status("script"))
                    self.assertEqual(1, status["manual_request"]["sequence"])
                    self.assertIn("Retrying... (attempt 2 of 4)", status["manual_request"]["stage_hint"])
                    self.assertIn("Step 2 (speakers): window 2 of 4", status["manual_request"]["stage_hint"])
                    pending = asyncio.run(script.manual_llm_pending())["pending"]
                    self.assertEqual([{"role": "user", "content": "Q"}], pending["messages"])
                    with self.assertRaises(script.HTTPException) as ctx:
                        asyncio.run(script.manual_llm_response(script.ManualReply(id="wrong", content="y")))
                    self.assertEqual(409, ctx.exception.status_code)
                    reply = asyncio.run(script.manual_llm_response(
                        script.ManualReply(id=pending["id"], content="A")))
                    self.assertTrue(reply["accepted"])
                    t.join(5)
                    self.assertEqual("A", done.get("text"))
                    state["running"] = False
                    self.assertIsNone(asyncio.run(script.get_status("script"))["manual_request"])
            finally:
                state.clear()
                state.update(original)


class ManualPendingOwnershipTests(unittest.TestCase):
    def assert_pending_storage_error(self, method, route):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(script.router)
        api.add_exception_handler(core.AppError, core.handle_app_error)
        for failure in (PermissionError('synthetic sharing violation'), ValueError('synthetic corrupt JSON')):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = self.write_pending(directory)
                original = path.read_bytes()
                original_open = open
                def fail_pending_read(filename, *args, **kwargs):
                    if os.fspath(filename) == str(path):
                        raise failure
                    return original_open(filename, *args, **kwargs)
                with patch.object(script, 'DATA_DIR', directory), \
                     patch('builtins.open', side_effect=fail_pending_read):
                    with TestClient(api) as client:
                        response = client.request(method, route, **(
                            {'json': {'id': 'old-casting', 'content': 'Answer'}} if method == 'POST' else {}))
                self.assertEqual(503, response.status_code)
                expected = ({'detail': 'The pending manual LLM request could not be read. Try again.',
                             'message_key': 'error.manual_llm.pending_read_failed'}
                            if isinstance(failure, OSError) else {
                                'detail': 'The manual LLM request file manual_llm/pending.json in the data directory is corrupt. '
                                          'Cancel the manual request (or restart the app), then delete or rename '
                                          'this file before trying again.',
                                'message_key': 'error.manual_llm.pending_corrupt'})
                self.assertEqual(expected, response.json())
                self.assertEqual(original, path.read_bytes())
                self.assertFalse(path.with_name('response.json').exists())

    def test_pending_route_reports_storage_error_instead_of_no_request(self):
        self.assert_pending_storage_error('GET', '/api/manual_llm/pending')

    def test_reply_route_reports_storage_error_instead_of_no_request(self):
        self.assert_pending_storage_error('POST', '/api/manual_llm/response')

    """An old casting prompt must not become a new design task's request."""

    def test_reply_revalidates_identity_after_acquiring_slot_lock(self):
        from utils import atomic_json_write, file_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_pending(tmp)

            @contextlib.contextmanager
            def replace_before_admission(target):
                with file_lock(target):
                    atomic_json_write({"id": "replacement", "sequence": 2}, path)
                    yield

            with patch.object(script, "DATA_DIR", tmp), \
                 patch.object(script, "file_lock", side_effect=replace_before_admission) as admission, \
                 patch.object(script, "is_manual_request_owner_alive", return_value=True):
                with self.assertRaises(script.HTTPException) as raised:
                    asyncio.run(script.manual_llm_response(
                        script.ManualReply(id="old-casting", content="Stale answer")))
            self.assertEqual(409, raised.exception.status_code)
            admission.assert_called_once_with(str(path))
            self.assertFalse(path.with_name("response.json").exists())
            self.assertEqual("replacement", json.loads(path.read_text())["id"])

    def write_pending(self, folder, **owner):
        from pathlib import Path
        path = Path(llm_provider.manual_llm_dir(folder), "pending.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": "old-casting", "sequence": 1,
                                    "messages": [{"role": "user", "content": "Rank synthetic voices."}],
                                    **owner}), encoding="utf-8")
        return path

    def test_orphaned_prompt_is_not_attached_to_single_or_multiple_design_status(self):
        for count in (1, 3):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                path = self.write_pending(tmp)
                original = path.read_bytes()
                states = {"persona": {"running": True, "logs": [f"Designing voice 1/{count}"]}}
                with patch.object(script, "DATA_DIR", tmp), patch.object(script, "process_state", states):
                    status = asyncio.run(script.get_status("persona"))
                self.assertTrue(status["running"])
                self.assertIsNone(status["manual_request"])
                self.assertEqual(original, path.read_bytes())
                self.assertNotIn("manual_request", states["persona"])

    def test_pending_endpoint_ignores_legacy_and_invalid_owners_without_deleting(self):
        for owner in ({}, {"owner_pid": True}, {"owner_pid": -1}, {"owner_pid": "123"}):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as tmp:
                path = self.write_pending(tmp, **owner)
                original = path.read_bytes()
                with patch.object(script, "DATA_DIR", tmp):
                    self.assertIsNone(asyncio.run(script.manual_llm_pending())["pending"])
                self.assertEqual(original, path.read_bytes())

    def test_reply_to_orphaned_prompt_is_refused_without_writing_or_deleting(self):
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = FastAPI()
        api.include_router(script.router)
        api.add_exception_handler(core.AppError, core.handle_app_error)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_pending(tmp)
            original = path.read_bytes()
            with patch.object(script, "DATA_DIR", tmp):
                with TestClient(api) as client:
                    refused = client.post("/api/manual_llm/response",
                                          json={"id": "old-casting", "content": "Stale answer"})
            self.assertEqual(409, refused.status_code)
            self.assertEqual({"detail": "No request is waiting for a reply.",
                              "message_key": "error.manual_llm.no_pending_request"}, refused.json())
            self.assertFalse(Path(path.parent, "response.json").exists())
            self.assertEqual(original, path.read_bytes())

    def test_reused_process_number_cannot_revive_a_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_pending(tmp, owner_pid=98765, owner_process_identity="previous-birth")
            with patch.object(script, "DATA_DIR", tmp), \
                 patch.object(llm_provider, "get_manual_process_identity", return_value="new-birth"):
                self.assertIsNone(script.read_manual_pending())

    def test_abandoned_same_process_request_cannot_borrow_a_live_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_pending(tmp, owner_pid=os.getpid(), owner_thread=threading.get_ident())
            with patch.object(script, "DATA_DIR", tmp):
                self.assertIsNone(script.read_manual_pending())

    def test_live_persona_manual_request_remains_answerable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ManualClient(tmp)
            client.POLL_SECONDS = .01
            done = {}
            worker = threading.Thread(target=lambda: done.update(
                text=client.create(model="fixture", messages=[{"role": "user", "content": "Synthetic persona?"}])
                .choices[0].message.content), daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 3
                while not os.path.exists(client.pending_path) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(os.path.exists(client.pending_path))
                states = {"persona": {"running": True, "logs": ["Step 1 (persona): waiting"]}}
                with patch.object(script, "DATA_DIR", tmp), patch.object(script, "process_state", states):
                    pending = asyncio.run(script.manual_llm_pending())["pending"]
                    self.assertEqual("persona", pending["task"])
                    status = asyncio.run(script.get_status("persona"))
                    self.assertEqual(pending["id"], status["manual_request"]["id"])
                    reply = asyncio.run(script.manual_llm_response(
                        script.ManualReply(id=pending["id"], content="A warm synthetic voice.")))
                    self.assertTrue(reply["accepted"])
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual("A warm synthetic voice.", done.get("text"))
                self.assertFalse(os.path.exists(client.pending_path))
                self.assertFalse(os.path.exists(client.response_path))
            finally:
                if worker.is_alive():
                    with open(client.pending_path, encoding="utf-8") as handle:
                        pending = json.load(handle)
                    from utils import atomic_json_write
                    atomic_json_write({"id": pending["id"], "content": "Cleanup answer"}, client.response_path)
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), "manual fixture owner did not retire")

    def test_owner_probe_runs_off_event_loop_and_errors_stay_visible(self):
        caller = threading.get_ident()
        def fail_probe(pid):
            self.assertEqual(98765, pid)
            self.assertNotEqual(caller, threading.get_ident())
            raise OSError("synthetic owner probe failure")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_pending(tmp, owner_pid=98765, owner_process_identity="birth")
            original = path.read_bytes()
            states = {"persona": {"running": True, "logs": []}}
            with patch.object(script, "DATA_DIR", tmp), patch.object(script, "process_state", states), \
                 patch.object(llm_provider, "get_manual_process_identity", side_effect=fail_probe):
                for call in (lambda: script.get_status("persona"), script.manual_llm_pending,
                             lambda: script.manual_llm_response(script.ManualReply(id="old-casting", content="Answer"))):
                    with self.subTest(call=call), self.assertRaisesRegex(OSError, "owner probe failure"):
                        asyncio.run(call())
            self.assertEqual(original, path.read_bytes())


if __name__ == "__main__":
    unittest.main()

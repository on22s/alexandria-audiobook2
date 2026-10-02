import contextlib
import io
import json
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

import mark_pr_ready as ready


class PullRequestCheckCompletenessTests(unittest.TestCase):
    def test_real_gh_pagination_reads_later_failed_check_and_latest_legacy_statuses(self):
        import os
        import shutil
        self.assertIsNotNone(shutil.which('gh'), 'GitHub CLI is required for this native protocol test')
        requests = []
        successful = {'name':'unit','status':'completed','conclusion':'success'}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                later = 'page=2' in self.path
                if 'check-runs' in self.path:
                    payload = {'check_runs': [{'name':f'pass-{i}',**successful} for i in range(30)]
                               if not later else [{'name':'hidden-failure','status':'completed','conclusion':'failure'}]}
                else:
                    payload = ([{'context':'legacy-recovered','state':'success'}] if not later else
                               [{'context':'legacy-recovered','state':'failure'},
                                {'context':'legacy-hidden','state':'pending'}])
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                if not later:
                    next_url = f'http://127.0.0.1:{self.server.server_port}{self.path}?page=2'
                    self.send_header('Link',f'<{next_url}>; rel="next"')
                self.end_headers(); self.wfile.write(data)
            def log_message(self, *_args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def run(command):
            command = [*command]
            command[2] = f'http://127.0.0.1:{server.server_port}/' + command[2]
            return subprocess.run(command,capture_output=True,text=True,
                                  env={**os.environ,'GH_TOKEN':'local-protocol-fixture'},timeout=20)
        try:
            with patch.object(ready,'run',side_effect=run):
                checks = ready.get_head_checks('fixture/repo','abc')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        self.assertEqual(4,len(requests))
        self.assertEqual(33,len(checks))
        legacy = [item for item in checks if item.get('context') == 'legacy-recovered']
        self.assertEqual([{'context':'legacy-recovered','status':'COMPLETED','conclusion':'SUCCESS'}],legacy)
        pr={'isDraft':True,'headRefOid':'abc','mergeable':'MERGEABLE','mergeStateStatus':'CLEAN',
            'statusCheckRollup':checks}
        errors=ready.get_readiness_errors(pr,'abc')
        self.assertTrue(any('hidden-failure' in error for error in errors))
        self.assertTrue(any('legacy-hidden' in error for error in errors))
        self.assertFalse(any('legacy-recovered' in error for error in errors))

    def test_actual_main_blocks_later_checks_and_legacy_failures_and_accepts_recovery(self):
        pr={'isDraft':True,'headRefOid':'abc','mergeable':'MERGEABLE','mergeStateStatus':'CLEAN',
            'url':'https://github.com/fixture/repo/pull/1'}
        success={'name':'unit','status':'completed','conclusion':'success'}
        for case in ('later_failure','legacy_failure','legacy_pending','legacy_error','legacy_recovered','legacy_only','empty','unreadable'):
            with self.subTest(case=case):
                calls=[]
                def run(command):
                    calls.append(command)
                    output=''
                    if command[:3]==['git','rev-parse','HEAD']: output='abc'
                    elif command[:3]==['git','branch','--show-current']: output='fixture'
                    elif command[:3]==['git','remote','get-url']: output='https://github.com/fixture/repo.git'
                    elif command[:3]==['gh','pr','view']: output=json.dumps(pr)
                    elif command[:2]==['gh','api']:
                        paginated='--paginate' in command
                        if 'check-runs' in command[2]:
                            first=[] if case in ('empty','legacy_only') else [success]*30
                            last=[{**success,'name':'hidden','conclusion':'failure'}] if case=='later_failure' else []
                            pages=[{'check_runs':first},{'check_runs':last}]
                            output=json.dumps(pages if paginated else first)
                            if case=='unreadable': output='{"unexpected":true}'
                        else:
                            state={'legacy_failure':'failure','legacy_pending':'pending','legacy_error':'error'}.get(case,'success')
                            statuses=[] if case in ('empty','later_failure') else [{'context':'legacy','state':state}]
                            if case=='legacy_recovered': statuses.append({'context':'legacy','state':'failure'})
                            output=json.dumps([statuses])
                    return subprocess.CompletedProcess(command,0,output,'')
                with patch.object(ready,'run',side_effect=run), \
                     patch.object(ready.subprocess,'run',return_value=subprocess.CompletedProcess([],0)), \
                     contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    code=ready.main([])
                allowed=case in ('legacy_recovered','legacy_only')
                self.assertEqual(0 if allowed else 1,code,case)
                self.assertEqual(allowed,any(call[:3]==['gh','pr','ready'] for call in calls),case)

    def test_api_errors_and_malformed_pages_are_not_success(self):
        for result in (subprocess.CompletedProcess([],1,'','denied'),
                       subprocess.CompletedProcess([],0,'null',''),
                       subprocess.CompletedProcess([],0,'[{"check_runs":[false]}]','')):
            with self.subTest(stdout=result.stdout),patch.object(ready,'run',return_value=result):
                with self.assertRaises(RuntimeError): ready.get_head_checks('fixture/repo','abc')

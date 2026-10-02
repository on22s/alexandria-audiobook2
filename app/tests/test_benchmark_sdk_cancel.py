"""Native SDK request packets and production reliability loops respect queued cancellation."""
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import benchmark_execution as execution
import generate_script as gs
from generate_script import LLMGenParams


class BenchmarkSdkCancelTests(unittest.TestCase):
    def test_captured_state_guards_nickname_worker_thread_without_context_inheritance(self):
        state={'cancel':True};calls=[]
        def handler(request):
            calls.append(request)
            raise AssertionError('Cancelled child-thread request must not dispatch')
        _,guarded=self.make_client(state,handler)
        def worker():
            self.assertIsNone(execution.BENCHMARK_STATE.get())
            return guarded.chat.completions.create(model='fixture',messages=[])
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(execution.BenchmarkCancelled):pool.submit(worker).result()
        self.assertEqual([],calls)

    def test_uncancelled_sdk_keeps_configured_retry_after_transport_error(self):
        state={'cancel':False};calls=[]
        def handler(request):
            calls.append(request.url.path)
            if len(calls)==1:
                return httpx.Response(429,headers={'retry-after-ms':'1'},json={'error':{'message':'fixture rate limit'}})
            return httpx.Response(200,json={'object':'list','data':[]})
        native,_=self.make_client(state,handler)
        retrying=native.with_options(max_retries=1)
        token=execution.BENCHMARK_STATE.set(state)
        try:guarded=execution.get_cancellable_benchmark_client(retrying)
        finally:execution.BENCHMARK_STATE.reset(token)
        self.assertEqual([],guarded.models.list().data)
        self.assertEqual(['/v1/models','/v1/models'],calls)
        self.assertEqual(1,retrying.max_retries)
        self.assertEqual(17,retrying.timeout)

    def make_client(self,state,handler):
        http=httpx.Client(transport=httpx.MockTransport(handler))
        native=OpenAI(api_key='fixture',base_url='http://fixture/v1',http_client=http,timeout=17,max_retries=0)
        self.addCleanup(native.close)
        token=execution.BENCHMARK_STATE.set(state)
        try:guarded=execution.get_cancellable_benchmark_client(native)
        finally:execution.BENCHMARK_STATE.reset(token)
        return native,guarded

    def test_cancel_before_chat_or_rtt_dispatch_makes_no_http_call(self):
        state={'cancel':True};calls=[]
        def handler(request):
            calls.append(request);raise AssertionError('Cancelled SDK request must not dispatch')
        native,guarded=self.make_client(state,handler)
        for request in (lambda:guarded.chat.completions.create(model='fixture',messages=[]),lambda:guarded.models.list()):
            with self.assertRaises(execution.BenchmarkCancelled):request()
        self.assertEqual([],calls)
        self.assertEqual(17,native.timeout)

    def test_cancel_during_valid_response_bypasses_json_retry_and_fallback(self):
        state={'cancel':False};calls=[]
        def handler(request):
            calls.append(json.loads(request.content));state['cancel']=True
            return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':'fixture',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"description":"Calm voice.","ref_text":"Hello."}'}}]})
        _,guarded=self.make_client(state,handler)
        with self.assertRaises(execution.BenchmarkCancelled):
            gs.call_llm_for_object(guarded,'fixture','Return JSON','Describe.',LLMGenParams(max_tokens=400),
                                   label='CANCEL FIXTURE',max_retries=3)
        self.assertEqual(1,len(calls))
        self.assertEqual('fixture',calls[0]['model'])
        self.assertTrue(issubclass(execution.BenchmarkCancelled,BaseException))
        self.assertFalse(issubclass(execution.BenchmarkCancelled,Exception))

    def test_cancel_during_transport_failure_and_rtt_does_not_enter_reliability_retry(self):
        for rtt in (False,True):
            with self.subTest(rtt=rtt):
                state={'cancel':False};calls=[]
                def handler(request):
                    calls.append(request.url.path);state['cancel']=True
                    raise httpx.ConnectError('fixture transport failure',request=request)
                _,guarded=self.make_client(state,handler)
                with self.assertRaises(execution.BenchmarkCancelled):
                    if rtt:guarded.models.list()
                    else:
                        gs.call_llm_for_object(guarded,'fixture','Return JSON','Describe.',LLMGenParams(max_tokens=400),
                                               label='CANCEL FIXTURE',max_retries=3)
                self.assertEqual(1,len(calls))

    def test_guard_keeps_other_invocations_independent_and_leaves_normal_sdk_payload_intact(self):
        active={'cancel':False};cancelled={'cancel':True};calls=[]
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':'fixture',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'ok'}}]})
        native,first=self.make_client(active,handler)
        token=execution.BENCHMARK_STATE.set(cancelled)
        try:second=execution.get_cancellable_benchmark_client(native)
        finally:execution.BENCHMARK_STATE.reset(token)
        with self.assertRaises(execution.BenchmarkCancelled):second.chat.completions.create(model='fixture',messages=[])
        response=first.chat.completions.create(model='fixture',messages=[{'role':'user','content':'Exact request.'}],temperature=.4)
        self.assertEqual('ok',response.choices[0].message.content)
        self.assertEqual([{'role':'user','content':'Exact request.'}],calls[0]['messages'])
        self.assertEqual(.4,calls[0]['temperature'])
        self.assertEqual(1,len(calls))
        self.assertIsNone(execution.BENCHMARK_STATE.get())

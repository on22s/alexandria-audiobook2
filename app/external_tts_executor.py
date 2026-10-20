"""A reusable external-I/O pool whose cancelled queue and exit stay bounded."""
from collections import OrderedDict
from concurrent.futures import Future
import threading


class ExternalTTSExecutor:
    """Keep one fixed daemon pool; cancelled pending work is removed immediately.

    Running provider I/O needs its own owned-process deadline. Daemon threads
    alone do not contain SDK executors, so production clients must use that
    transport rather than starting an SDK in these threads.
    """

    def __init__(self, max_workers):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("External TTS needs a positive worker count")
        self._condition = threading.Condition()
        self._jobs = OrderedDict()
        self._closed = False
        self._threads = []
        try:
            for index in range(max_workers):
                thread = threading.Thread(target=self._run, daemon=True,
                    name=f"external-tts-{id(self):x}-{index}")
                thread.start()
                self._threads.append(thread)
        except BaseException:
            self.shutdown(wait=True, cancel_futures=True)
            raise

    def submit(self, function, /, *args, **kwargs):
        future = Future()
        with self._condition:
            if self._closed:
                raise RuntimeError("External TTS executor is closed")
            self._jobs[future] = (function, args, kwargs)
            future.add_done_callback(self._remove_cancelled)
            self._condition.notify()
        return future

    def _remove_cancelled(self, future):
        if future.cancelled():
            with self._condition:
                self._jobs.pop(future, None)

    def _run(self):
        while True:
            with self._condition:
                while not self._jobs and not self._closed:
                    self._condition.wait()
                if not self._jobs:
                    return
                future, job = self._jobs.popitem(last=False)
            function, args, kwargs = job
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = function(*args, **kwargs)
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
                        del result
            finally:
                # A bound method can retain its engine; release it before idle
                # so engine finalization can shut down this otherwise idle pool.
                del future, job, function, args, kwargs

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._condition:
            self._closed = True
            if cancel_futures:
                for future in list(self._jobs):
                    future.cancel()
            self._condition.notify_all()
        if wait:
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join()

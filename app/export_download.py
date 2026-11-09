"""Pinned export readers that allow Windows publication without reopening paths."""
import asyncio
import os
import stat
from secrets import token_hex
from fastapi.responses import FileResponse
from starlette.datastructures import MutableHeaders
from upload_operation import await_upload_operation
from export_file_io import open_export_reader


class ExportFileResponse(FileResponse):
    """Keep Starlette's HEAD/range dispatch, but serve one delete-shared descriptor."""

    async def _io(self, operation, *args):
        result, cancellation = await await_upload_operation(asyncio.to_thread(operation, *args))
        if cancellation is not None:
            raise cancellation
        return result

    async def __call__(self, scope, receive, send):
        self.stream, cancellation = await await_upload_operation(asyncio.to_thread(open_export_reader, self.path))
        try:
            if cancellation is not None:
                raise cancellation
            self.stat_result = await self._io(os.fstat, self.stream.fileno())
            if not stat.S_ISREG(self.stat_result.st_mode):
                raise RuntimeError('Export download requires a regular file')
            self.set_stat_headers(self.stat_result)
            return await super().__call__(scope, receive, send)
        finally:
            await self._io(self.stream.close)

    async def _send_range(self, send, start, end):
        await self._io(self.stream.seek, start)
        while start < end:
            block = await self._io(self.stream.read, min(self.chunk_size, end - start))
            if not block:
                raise OSError('Pinned export was truncated during download')
            start += len(block)
            await send({'type':'http.response.body', 'body':block, 'more_body':True})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({'type':'http.response.start', 'status':self.status_code, 'headers':self.raw_headers})
        if not send_header_only:
            await self._send_range(send, 0, self.stat_result.st_size)
        await send({'type':'http.response.body', 'body':b'', 'more_body':False})

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-range'] = f'bytes {start}-{end - 1}/{file_size}'
        headers['content-length'] = str(end - start)
        await send({'type':'http.response.start', 'status':206, 'headers':headers.raw})
        if not send_header_only:
            await self._send_range(send, start, end)
        await send({'type':'http.response.body', 'body':b'', 'more_body':False})

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = token_hex(13)
        def make_header(start, end):
            return (f'--{boundary}\r\nContent-Type: {self.headers["content-type"]}\r\n'
                    f'Content-Range: bytes {start}-{end - 1}/{file_size}\r\n\r\n').encode('latin-1')
        closing = f'--{boundary}--'.encode('latin-1')
        size = sum(len(make_header(start, end)) + end - start + 2 for start, end in ranges) + len(closing)
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-type'] = f'multipart/byteranges; boundary={boundary}'
        headers['content-length'] = str(size)
        await send({'type':'http.response.start', 'status':206, 'headers':headers.raw})
        if send_header_only:
            await send({'type':'http.response.body', 'body':b'', 'more_body':False})
            return
        for start, end in ranges:
            await send({'type':'http.response.body', 'body':make_header(start, end), 'more_body':True})
            await self._send_range(send, start, end)
            await send({'type':'http.response.body', 'body':b'\r\n', 'more_body':True})
        await send({'type':'http.response.body', 'body':closing, 'more_body':False})

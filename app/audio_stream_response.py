"""Stream an already pinned audio descriptor and close it on every ASGI exit."""
import asyncio
import mimetypes
from urllib.parse import quote
from fastapi.responses import StreamingResponse
from upload_operation import await_upload_operation


class AudioStreamResponse(StreamingResponse):
    def __init__(self, stream, size, suffix, *, filename=None):
        self.stream = stream
        def chunks():
            try:
                yield from iter(lambda: stream.read(65536), b'')
            finally:
                stream.close()
        media = ('audio/mp4' if suffix == '.m4b' else
                 mimetypes.guess_type('audio'+suffix)[0] or 'application/octet-stream')
        headers = {'Content-Length':str(size),'Cache-Control':'no-store'}
        if filename is not None:
            headers['Content-Disposition'] = "attachment; filename*=UTF-8''"+quote(filename,safe='')
        super().__init__(chunks(),media_type=media,headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope,receive,send)
        finally:
            _, cancellation = await await_upload_operation(asyncio.to_thread(self.stream.close))
            if cancellation is not None:
                raise cancellation

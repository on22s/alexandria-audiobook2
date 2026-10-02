"""Historical adapter URLs retain native static-file delivery after renaming."""
import asyncio
import os
from urllib.parse import quote

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from voice_manifest import get_resolved_adapter_path, validate_adapter_name


class AdapterStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        parts = path.replace(os.sep, '/').split('/')
        try:
            validate_adapter_name(parts[0])
        except ValueError as error:
            raise HTTPException(status_code=404) from error
        try:
            resolved = await asyncio.to_thread(
                get_resolved_adapter_path, os.path.join(self.directory, parts[0]))
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        current = os.path.basename(resolved)
        if current != parts[0]:
            relative = '/'.join([current, *parts[1:]])
            url = scope.get('root_path', '').rstrip('/') + '/' + quote(relative, safe='/')
            query = scope.get('query_string', b'').decode('latin-1')
            if query:
                url += '?' + query
            return RedirectResponse(url, status_code=307)
        return await super().get_response(path, scope)

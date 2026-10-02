"""Read native LM Studio inventory without assuming CLI or SSH ownership."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoNativeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_native_model_status(document, model_name, version):
    if not isinstance(document, dict):
        return None
    models = document.get('models' if version == 1 else 'data')
    if not isinstance(models, list) or not models:
        return None
    loaded = []
    for model in models:
        if not isinstance(model, dict):
            return None
        if version == 1:
            if (model.get('type') not in ('llm', 'embedding')
                    or not isinstance(model.get('key'), str)
                    or not isinstance(model.get('publisher'), str)
                    or not isinstance(model.get('loaded_instances'), list)):
                return None
            instances = model['loaded_instances']
            for instance in instances:
                if (not isinstance(instance, dict) or not isinstance(instance.get('id'), str)
                        or not isinstance(instance.get('config'), dict)):
                    return None
                config = instance['config']
                context, parallel = config.get('context_length'), config.get('parallel')
                if (type(context) is not int or context <= 0
                        or parallel is not None and (type(parallel) is not int or parallel <= 0)):
                    return None
                if (model['type'] == 'llm' and (instance['id'] == model_name
                        or model['key'] == model_name and len(instances) == 1)):
                    loaded.append((context, parallel))
        else:
            if (model.get('object') != 'model'
                    or model.get('type') not in ('llm', 'vlm', 'embeddings')
                    or not isinstance(model.get('id'), str)
                    or not isinstance(model.get('publisher'), str)
                    or model.get('compatibility_type') not in ('gguf', 'mlx')
                    or model.get('state') not in ('loaded', 'not-loaded')):
                return None
            if model['id'] == model_name and model['state'] == 'loaded':
                # max_context_length is capacity, not the actual loaded context.
                loaded.append((None, None))
    context, parallel = loaded[0] if len(loaded) == 1 else (None, None)
    return {'runtime': 'lmstudio', 'native_api_version': version,
            'available': True, 'loaded': len(loaded) == 1,
            'context_length': context, 'parallel': parallel, 'optimized': False}


def get_lmstudio_endpoint_status(base_url, model_name, api_key=None, timeout=5):
    """Return positively identified native status, or None on unknown/failure."""
    try:
        url = urlsplit(base_url)
        if url.scheme not in ('http', 'https') or not url.hostname:
            return None
        path = url.path.rstrip('/')
        if path.endswith('/v1'):
            path = path[:-3]
        headers = {'Accept': 'application/json'}
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        opener = build_opener(_NoNativeRedirects())
        for version in (1, 0):
            endpoint = urlunsplit((url.scheme, url.netloc,
                                   path + '/api/v%d/models' % version, '', ''))
            try:
                with opener.open(Request(endpoint, headers=headers), timeout=timeout) as response:
                    document = json.loads(response.read())
            except HTTPError as exc:
                if version == 1 and exc.code in (404, 405):
                    continue
                return None
            return get_native_model_status(document, model_name, version)
    except (OSError, URLError, ValueError, TypeError):
        return None
    return None

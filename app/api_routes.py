"""One answer to "which routes does this app serve?", across FastAPI versions."""


def get_app_routes(application):
    """Every route the application serves, in registration order.

    Each item is a dict with the route object itself plus its effective type,
    name, path and sorted methods. FastAPI 0.137+ keeps each include_router()
    router as one nested entry in `app.routes`, so walking `app.routes`
    directly would silently skip every route inside it; FastAPI's own
    iter_route_contexts() expands them with their effective paths."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI < 0.137: app.routes is already flat
        pairs = [(route, route) for route in application.routes]
    else:
        pairs = [(context.original_route, context)
                 for context in iter_route_contexts(application.routes)]
    return [{"route": route, "type": type(route).__name__, "name": view.name,
             "path": view.path, "methods": sorted(getattr(view, "methods", None) or [])}
            for route, view in pairs]

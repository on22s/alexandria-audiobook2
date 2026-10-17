"""get_app_routes must list every served route, whatever FastAPI version nests."""
import unittest

from fastapi import APIRouter, FastAPI

from api_routes import get_app_routes


def make_app():
    inner = APIRouter()

    @inner.get("/leaf", name="leaf")
    def leaf():
        return {}

    outer = APIRouter()

    @outer.post("/item", name="item")
    def item():
        return {}

    outer.include_router(inner, prefix="/inner")
    application = FastAPI()

    @application.get("/top", name="top")
    def top():
        return {}

    application.include_router(outer, prefix="/outer")
    return application


class AppRoutesTests(unittest.TestCase):
    def test_routes_inside_included_routers_are_listed_with_their_full_paths(self):
        routes = {route["name"]: route for route in get_app_routes(make_app())}
        self.assertEqual(routes["top"]["path"], "/top")
        self.assertEqual(routes["item"]["path"], "/outer/item")
        self.assertEqual(routes["leaf"]["path"], "/outer/inner/leaf")
        self.assertEqual(routes["item"]["methods"], ["POST"])
        self.assertEqual(routes["leaf"]["type"], "APIRoute")

    def test_registration_order_is_kept(self):
        names = [route["name"] for route in get_app_routes(make_app())
                 if route["name"] in ("top", "item", "leaf")]
        self.assertEqual(names, ["top", "item", "leaf"])

    def test_the_route_object_itself_is_returned(self):
        route = next(r for r in get_app_routes(make_app()) if r["name"] == "leaf")
        self.assertEqual(route["route"].endpoint.__name__, "leaf")


if __name__ == "__main__":
    unittest.main()

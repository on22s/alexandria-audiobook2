"""Synthetic real-HTTP tests for app/calibre_catalog.py (issue #1734, slice 2).

Browse behaviour runs against the real raw-socket servers from
``tests.test_calibre_opds`` (two independent origins where cross-origin
behaviour is asserted with exact per-server auth logs). Credential-store
behaviour uses synthetic injected keyring fakes (no platform vault is
required or touched). No live catalog, no private data.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import calibre_catalog as cat
import calibre_opds as opds
from tests.test_calibre_opds import ATOM, feed_xml, start_server, stop_server

USER, PW = "synthetic-user", "synthetic-pass"
AUTH = opds.OpdsAuth(USER, PW).header_value()


# --- Synthetic injected keyring backends -------------------------------------

class _SecureBackend:
    pass


class PlaintextBackend:
    pass


class FakeKeyring:
    def __init__(self, backend=None):
        self.store = {}
        self._backend = backend if backend is not None else _SecureBackend()

    def get_keyring(self):
        return self._backend

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


class ExplodingKeyring(FakeKeyring):
    def set_password(self, service, account, value):
        raise RuntimeError(f"vault exploded {value}")


class CatalogMixin(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data_dir = self._td.name
        self.server = start_server()
        self.addCleanup(stop_server, self.server)
        self.origin = self.server.origin

    def make_service(self, **kw):
        kw.setdefault("limit", opds.Limits(timeout_seconds=5))
        return cat.CalibreCatalogService(self.data_dir, **kw)

    def config_file_text(self):
        return (Path(self.data_dir) / cat.CONFIG_NAME).read_text(encoding="utf-8")


class ConfigTests(CatalogMixin):
    def test_anonymous_config_works_with_no_vault_and_makes_no_requests_on_save(self):
        # Keyring unavailable entirely: anonymous browse must still work, and
        # config load/save/startup must issue zero HTTP requests.
        svc = self.make_service(keyring_factory=lambda: None)
        self.assertEqual(self.server.seen_paths, [])
        svc.save_config(f"{self.origin}/opds", username="")
        self.assertEqual(self.server.seen_paths, [])
        cfg = svc.get_config()
        self.assertEqual(cfg, {"root_url": f"{self.origin}/opds", "username": "",
                               "configured": True, "revision": 1})
        self.assertEqual(self.server.seen_paths, [])
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")]))
        page = svc.browse_root_page()
        self.assertEqual(page["entries"][0]["title"], "One")
        self.assertEqual(self.server.seen_auth, [("/opds", None)])

    def test_password_only_in_vault_never_in_file_returns_or_errors(self):
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        self.assertNotIn(PW, self.config_file_text())
        self.assertIn(USER, self.config_file_text())  # username is public, stored
        self.assertEqual(set(__import__("json").loads(self.config_file_text())),
                         {"root_url", "username", "revision"})
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")]))
        page = svc.browse_root_page()
        self.assertNotIn(PW, str(page))
        self.assertEqual(self.server.seen_auth, [("/opds", AUTH)])
        # Auth failure path: sanitized message, no secret.
        self.server.route("/up", b"no", status=401)
        self.server.route("/opds", feed_xml("F", [], '<link href="up" rel="next"/>'))
        page = svc.browse_root_page()
        with self.assertRaises(cat.CatalogError) as ctx:
            svc.browse_next_page(page["next_token"])
        self.assertNotIn(PW, str(ctx.exception))

    def test_insecure_and_failing_backends_refused_without_leaking(self):
        for backend in (PlaintextBackend(), None):
            with self.subTest(backend=type(backend).__name__):
                vault = FakeKeyring(backend=backend) if backend is not None else FakeKeyring()
                if backend is None:
                    class PlaintextKeyring(FakeKeyring):
                        pass
                    vault = FakeKeyring(backend=PlaintextBackend())
                svc = self.make_service(keyring_factory=lambda v=vault: v)
                with self.assertRaises(cat.CatalogError) as ctx:
                    svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
                self.assertNotIn(PW, str(ctx.exception))
                self.assertFalse(svc.get_config()["configured"])  # file untouched
        exploding = ExplodingKeyring()
        svc = self.make_service(keyring_factory=lambda: exploding)
        with self.assertRaises(cat.CatalogError) as ctx:
            svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        self.assertNotIn(PW, str(ctx.exception))  # backend error text sanitized
        self.assertFalse(svc.get_config()["configured"])

    def test_unconfigured_browse_refused(self):
        svc = self.make_service(keyring_factory=lambda: None)
        with self.assertRaises(cat.CatalogError):
            svc.browse_root_page()
        with self.assertRaises(cat.CatalogError):
            svc.browse_next_page("invented-token")


class BrowseTests(CatalogMixin):
    def test_path_prefix_preserved_and_relative_next_resolved(self):
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "a.epub", "application/epub+zip")],
                                            '<link href="p2" rel="next"/>'))
        self.server.route("/p2", feed_xml("F", [("2", "Two", "A", "S", "2", "b.epub", "application/epub+zip")]))
        page = svc.browse_root_page()
        self.assertEqual(page["feed_url"], f"{self.origin}/opds")
        self.assertEqual(page["entries"][0]["source_url"], f"{self.origin}/a.epub")
        page2 = svc.browse_next_page(page["next_token"])
        self.assertEqual(page2["feed_url"], f"{self.origin}/p2")
        self.assertEqual([e["title"] for e in page2["entries"]], ["Two"])
        self.assertEqual(self.server.seen_paths, ["/opds", "/p2"])

    def test_cross_origin_next_requires_approval_and_gets_no_credentials(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        b_origin = other.origin
        other.route("/opds/p2", feed_xml("B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        self.server.route("/opds/p1", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                               f'<link href="{b_origin}/opds/p2" rel="next"/>'))
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        # Point the configured root at p1 by browsing root served at /opds:
        self.server.route("/opds", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                           f'<link href="{b_origin}/opds/p2" rel="next"/>'))
        page = svc.browse_root_page()
        review = svc.browse_next_page(page["next_token"])
        self.assertTrue(review["review_required"])
        self.assertEqual(review["destination_origin"], b_origin)
        self.assertEqual(other.seen_paths, [])  # B never fetched before approval
        with self.assertRaises(cat.CatalogError):
            svc.approve_cross_origin_review("bogus-token")
        approval = svc.approve_cross_origin_review(review["review_token"])
        self.assertTrue(approval["approved"])
        with self.assertRaises(cat.CatalogError):  # single-use
            svc.approve_cross_origin_review(review["review_token"])
        page2 = svc.browse_next_page(approval["nav_token"])
        self.assertEqual([e["title"] for e in page2["entries"]], ["Two"])
        self.assertEqual(other.seen_paths, ["/opds/p2"])
        self.assertEqual(other.seen_auth, [("/opds/p2", None)])  # no credentials to B
        self.assertEqual(self.server.seen_auth, [("/opds", AUTH)])

    def test_stale_config_tokens_and_reviews_refused(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        b_origin = other.origin
        other.route("/opds/p2", feed_xml("B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        self.server.route("/opds", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                           f'<link href="{b_origin}/opds/p2" rel="next"/>'))
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        review = svc.browse_next_page(page["next_token"])
        preview_tok = page["entries"][0]["preview_token"]
        svc.save_config(f"{self.origin}/opds")  # bump revision, invalidate all
        with self.assertRaises(cat.CatalogError):
            svc.browse_next_page(page["next_token"])
        with self.assertRaises(cat.CatalogError):
            svc.approve_cross_origin_review(review["review_token"])
        with self.assertRaises(cat.CatalogError):
            svc.get_metadata_preview(preview_tok)

    def test_replayed_next_token_and_self_loop_refused(self):
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                           '<link href="p2" rel="next"/>'))
        self.server.route("/p2", feed_xml("F", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        svc.browse_next_page(page["next_token"])
        with self.assertRaises(cat.CatalogError):  # replay of consumed token
            svc.browse_next_page(page["next_token"])
        # Self-loop: next points at the page itself -> no token minted.
        self.server.route("/opds/loop", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                                '<link href="loop" rel="next"/>'))
        svc.save_config(f"{self.origin}/opds/loop")
        loop_page = svc.browse_root_page()
        self.assertIsNone(loop_page["next_token"])
        self.assertTrue(loop_page["next_loop_refused"])

    def test_page_cap_and_ttl_enforced(self):
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                           '<link href="p2" rel="next"/>'))
        self.server.route("/p2", feed_xml("F", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        svc = self.make_service(keyring_factory=lambda: None, max_pages_per_session=1)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        with self.assertRaises(cat.CatalogError):
            svc.browse_next_page(page["next_token"])
        self.assertEqual(self.server.seen_paths, ["/opds"])  # cap fires before fetch
        # TTL: short-lived tokens expire.
        svc2 = self.make_service(keyring_factory=lambda: None, session_ttl=0.15)
        svc2.save_config(f"{self.origin}/opds")
        page2 = svc2.browse_root_page()
        time.sleep(0.25)
        with self.assertRaises(cat.CatalogError):
            svc2.browse_next_page(page2["next_token"])

    def test_cancellation_propagates_and_token_survives(self):
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                           '<link href="p2" rel="next"/>'))
        self.server.route("/p2", feed_xml("F", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        event = threading.Event()
        event.set()
        with self.assertRaises(opds.CancelledError):
            svc.browse_next_page(page["next_token"], cancel_event=event)
        self.assertEqual(self.server.seen_paths, ["/opds"])
        event.clear()
        page2 = svc.browse_next_page(page["next_token"], cancel_event=event)
        self.assertEqual([e["title"] for e in page2["entries"]], ["Two"])


class PreviewTests(CatalogMixin):
    def test_preview_immutable_provider_metadata_distinct_from_user_edits(self):
        good_xml = (
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">'
            '<title>F</title>'
            '<entry><id>urn:synthetic:1</id><title>Good Book</title>'
            '<author><name>Auth Or</name></author>'
            '<category scheme="http://opds-spec.org/2010/categories/series" label="Ser - 3"/>'
            '<link href="g.epub" rel="http://opds-spec.org/acquisition" type="application/epub+zip"/></entry>'
            '<entry><id>urn:synthetic:2</id><title>DRM Book</title>'
            '<link href="d.epub" rel="http://opds-spec.org/acquisition" type="application/vnd.adobe.adept+xml"/></entry>'
            '<entry><id>urn:synthetic:3</id><title>PDF Book</title>'
            '<link href="x.pdf" rel="http://opds-spec.org/acquisition" type="application/pdf"/></entry>'
            '</feed>').encode()
        self.server.route("/opds", good_xml)
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        good, drm, pdf = page["entries"]
        self.assertIsNotNone(good["preview_token"])
        self.assertIsNone(drm["preview_token"])  # protected: no acquisition preview
        self.assertIsNone(pdf["preview_token"])  # unsupported: no acquisition preview
        preview = svc.get_metadata_preview(good["preview_token"])
        prov = preview["provider"]
        self.assertEqual(prov, {"title": "Good Book", "authors": ["Auth Or"], "series": "Ser",
                                "series_index": "3", "subtitle": "", "format": "epub"})
        self.assertEqual(preview["stable_id"], "urn:synthetic:1")
        self.assertEqual(preview["source_url"], f"{self.origin}/g.epub")
        self.assertEqual(preview["user_editable"],
                         {"title": "Good Book", "authors": ["Auth Or"], "series": "Ser"})
        # Mutating the returned copies must not affect server-held state.
        preview["provider"]["title"] = "Hacked"
        preview["user_editable"]["title"] = "Edited"
        again = svc.get_metadata_preview(good["preview_token"])
        self.assertEqual(again["provider"]["title"], "Good Book")
        self.assertEqual(again["user_editable"]["title"], "Good Book")
        with self.assertRaises(cat.CatalogError):
            svc.get_metadata_preview("invented-token")


class RepairTests(CatalogMixin):
    def test_config_switch_during_browse_leaks_no_new_credentials_to_old_root(self):
        # R-S2-01: two real servers; barrier at auth resolution, A->B switch.
        other = start_server()
        self.addCleanup(stop_server, other)
        import base64
        b_header = "Basic " + base64.b64encode(b"userB:passB").decode()
        self.server.route("/opds", feed_xml("A", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")],
                                            '<link href="opds2" rel="next"/>'))
        self.server.route("/opds2", feed_xml("A", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        other.route("/opds", feed_xml("B", [("9", "Nine", "B", "S", "9", "9.epub", "application/epub+zip")]))
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username="userA", password="passA")
        page = svc.browse_root_page()
        orig = svc._get_auth
        auth_ready, do_save = threading.Event(), threading.Event()

        def patched(*args, **kwargs):
            auth_ready.set()
            do_save.wait(5)
            try:
                return orig(*args, **kwargs)
            except TypeError:
                return orig()

        svc._get_auth = patched
        outcome = {}

        def worker():
            try:
                svc.browse_next_page(page["next_token"])
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        auth_ready.wait(2)
        was_blocked = auth_ready.is_set()
        svc.save_config(f"{other.origin}/opds", username="userB", password="passB")
        do_save.set()
        t.join(10)
        self.assertFalse(any(a == b_header for _p, a in self.server.seen_auth),
                         f"B credentials leaked to A: {self.server.seen_auth}")
        if was_blocked:
            # Stale before network: /opds2 must never be requested.
            self.assertNotIn("/opds2", self.server.seen_paths)
            self.assertIsInstance(outcome.get("exc"), cat.CatalogError)

    def test_username_without_password_refuses_before_fetch(self):
        # R-S2-03: missing and cleared passwords fail loud, zero dispatch.
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        self.server.route("/opds", feed_xml("F", [("1", "One", "A", "S", "1", "1.epub", "application/epub+zip")]))
        svc.save_config(f"{self.origin}/opds", username=USER)  # never set password
        with self.assertRaises(cat.CatalogError) as ctx:
            svc.browse_root_page()
        self.assertNotIn(PW, str(ctx.exception))
        self.assertEqual(self.server.seen_paths, [])
        # Cleared password with username kept: also refuses.
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        svc.save_config(f"{self.origin}/opds", username=USER, password="")
        with self.assertRaises(cat.CatalogError):
            svc.browse_root_page()
        self.assertEqual(self.server.seen_paths, [])
        # Empty username is explicitly anonymous and needs no vault at all.
        svc2 = self.make_service(keyring_factory=lambda: None)
        svc2.save_config(f"{self.origin}/opds", username="")
        page = svc2.browse_root_page()
        self.assertEqual(page["entries"][0]["title"], "One")
        self.assertEqual(self.server.seen_auth, [("/opds", None)])

    def test_concurrent_save_set_clear_leaves_coherent_state(self):
        # R-S2-04: blocking vault set; clear must serialize behind it.
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        root = f"{self.origin}/opds"
        svc.save_config(root, username="initial", password="initialpw")
        real_set = vault.set_password
        set_done, release = threading.Event(), threading.Event()

        def blocking_set(service, account, value):
            real_set(service, account, value)
            set_done.set()
            release.wait(5)

        vault.set_password = blocking_set
        t1_out = {}

        def t1():
            try:
                svc.save_config(root, username="userA", password="newpw")
            except Exception as exc:  # noqa: BLE001
                t1_out["exc"] = exc

        th = threading.Thread(target=t1)
        th.start()
        self.assertTrue(set_done.wait(5))
        t2_done = threading.Event()

        def t2():
            try:
                svc.save_config(root, username="", password="")
            except Exception as exc:  # noqa: BLE001
                t2_done.set()
                raise
            t2_done.set()

        th2 = threading.Thread(target=t2)
        th2.start()
        self.assertFalse(t2_done.wait(0.3), "clear finished while set held the config lock")
        release.set()
        th.join(10)
        th2.join(10)
        self.assertNotIn("exc", t1_out)
        cfg = svc.get_config()
        stored = vault.store.get((cat.CREDENTIAL_SERVICE, root))
        # Coherent: username configured iff a password is stored; never
        # username-without-password (which would silently downgrade).
        self.assertEqual(bool(cfg["username"]), stored is not None)
        if cfg["username"]:
            self.assertIsNotNone(svc._get_auth())
        else:
            self.assertIsNone(svc._get_auth())

    def test_save_persistence_failure_retains_prior_config_and_vault(self):
        # R-S2-04 (persistence): config write fails after vault write.
        import utils
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        old_root = f"{self.origin}/opds"
        svc.save_config(old_root, username="userA", password="passA")
        before_cfg, before_file = svc.get_config(), self.config_file_text()
        real_write = utils.atomic_json_write

        def boom(*a, **k):
            raise OSError("disk full")

        utils.atomic_json_write = boom
        try:
            with self.assertRaises(cat.CatalogError):
                svc.save_config(f"{self.origin}/other", username="userB", password="passB")
        finally:
            utils.atomic_json_write = real_write
        self.assertEqual(svc.get_config(), before_cfg)
        self.assertEqual(self.config_file_text(), before_file)
        self.assertEqual(vault.store.get((cat.CREDENTIAL_SERVICE, old_root)), "passA")
        self.assertIsNone(vault.store.get((cat.CREDENTIAL_SERVICE, f"{self.origin}/other")))

    def test_redirect_review_shows_exact_redirect_target(self):
        # R-S2-02: real A->B redirect; review must display B's exact URL.
        other = start_server()
        self.addCleanup(stop_server, other)
        b_origin = other.origin
        other.route("/opds/p2", feed_xml("B", [("9", "Nine", "B", "S", "9", "9.epub", "application/epub+zip")]))
        self.server.route("/opds", b"", status=302, headers={"Location": f"{b_origin}/opds/p2"})
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        review = svc.browse_root_page()
        self.assertTrue(review["review_required"])
        self.assertEqual(review["destination_origin"], b_origin)
        self.assertEqual(review["destination_url"], f"{b_origin}/opds/p2")
        self.assertIn("nav_token", review)
        self.assertIn("session_id", review)
        self.assertEqual(review["config_revision"], svc.get_config()["revision"])
        self.assertEqual(other.seen_paths, [])
        approval = svc.approve_cross_origin_review(review["review_token"])
        self.assertEqual(approval["destination_url"], f"{b_origin}/opds/p2")
        page = svc.browse_next_page(approval["nav_token"])
        self.assertEqual([e["title"] for e in page["entries"]], ["Nine"])
        self.assertEqual(other.seen_paths, ["/opds/p2"])
        self.assertEqual(other.seen_auth, [("/opds/p2", None)])
        self.assertIn(AUTH, [a for _p, a in self.server.seen_auth])

    def test_approved_exact_url_not_origin_wide_changed_redirect_reviews_again(self):
        # R-S2-02: B/path1 approved; B redirecting to B/path2 needs new review.
        other = start_server()
        self.addCleanup(stop_server, other)
        b_origin = other.origin
        other.route("/opds/path2", feed_xml("B", [("9", "Nine", "B", "S", "9", "9.epub", "application/epub+zip")]))
        other.route("/opds/path1", b"", status=302, headers={"Location": f"{b_origin}/opds/path2"})
        self.server.route("/opds", b"", status=302, headers={"Location": f"{b_origin}/opds/path1"})
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        review = svc.browse_root_page()
        self.assertEqual(review["destination_url"], f"{b_origin}/opds/path1")
        approval = svc.approve_cross_origin_review(review["review_token"])
        review2 = svc.browse_next_page(approval["nav_token"])
        self.assertTrue(review2["review_required"])
        self.assertEqual(review2["destination_url"], f"{b_origin}/opds/path2")
        self.assertEqual(other.seen_paths, ["/opds/path1"])  # path2 never fetched
        approval2 = svc.approve_cross_origin_review(review2["review_token"])
        page = svc.browse_next_page(approval2["nav_token"])
        self.assertEqual([e["title"] for e in page["entries"]], ["Nine"])
        self.assertEqual(other.seen_paths, ["/opds/path1", "/opds/path1", "/opds/path2"])
        self.assertTrue(all(a is None for _p, a in other.seen_auth))

    def test_multi_hop_b_to_c_approvals_progress_without_origin_trust(self):
        # R-S2-02: A->B->C, each exact hop separately reviewed.
        b_srv, c_srv = start_server(), start_server()
        self.addCleanup(stop_server, b_srv)
        self.addCleanup(stop_server, c_srv)
        c_srv.route("/final", feed_xml("C", [("9", "Nine", "C", "S", "9", "9.epub", "application/epub+zip")]))
        b_srv.route("/hop", b"", status=302, headers={"Location": f"{c_srv.origin}/final"})
        self.server.route("/opds", b"", status=302, headers={"Location": f"{b_srv.origin}/hop"})
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        r1 = svc.browse_root_page()
        self.assertEqual(r1["destination_url"], f"{b_srv.origin}/hop")
        a1 = svc.approve_cross_origin_review(r1["review_token"])
        with self.assertRaises(cat.CatalogError):  # review token single-use
            svc.approve_cross_origin_review(r1["review_token"])
        r2 = svc.browse_next_page(a1["nav_token"])
        self.assertTrue(r2["review_required"])
        self.assertEqual(r2["destination_url"], f"{c_srv.origin}/final")
        self.assertEqual(c_srv.seen_paths, [])
        a2 = svc.approve_cross_origin_review(r2["review_token"])
        page = svc.browse_next_page(a2["nav_token"])
        self.assertEqual([e["title"] for e in page["entries"]], ["Nine"])
        self.assertEqual(c_srv.seen_paths, ["/final"])

    def test_load_config_rejects_malformed_persisted_fields(self):
        # R-S2-05: restart validation; file preserved, no secret echo.
        import json as _json
        cases = [
            {"root_url": "http://127.0.0.1:9/opds?token=secret#frag", "username": "u", "revision": 7},
            {"root_url": f"{self.origin}/opds", "username": "u", "revision": True},
            {"root_url": f"{self.origin}/opds", "username": "u", "revision": "7"},
            {"root_url": f"{self.origin}/opds", "username": "bad user", "revision": 1},
            {"root_url": 123, "username": "u", "revision": 1},
        ]
        for case in cases:
            with self.subTest(case=str(case)[:40]):
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / cat.CONFIG_NAME
                    text = _json.dumps(case)
                    path.write_text(text, encoding="utf-8")
                    svc = cat.CalibreCatalogService(td, keyring_factory=lambda: None,
                                                    limit=opds.Limits(timeout_seconds=5))
                    cfg = svc.get_config()
                    self.assertFalse(cfg["configured"])
                    self.assertEqual(cfg["root_url"], "")
                    self.assertNotIn("secret", str(cfg))
                    self.assertEqual(path.read_text(encoding="utf-8"), text)
                    with self.assertRaises(cat.CatalogError):
                        svc.browse_root_page()
        # Control: a valid persisted config still loads.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / cat.CONFIG_NAME).write_text(
                _json.dumps({"root_url": f"{self.origin}/opds", "username": "u", "revision": 3}),
                encoding="utf-8")
            svc = cat.CalibreCatalogService(td, keyring_factory=lambda: None,
                                            limit=opds.Limits(timeout_seconds=5))
            self.assertEqual(svc.get_config(),
                             {"root_url": f"{self.origin}/opds", "username": "u",
                              "configured": True, "revision": 3})



class RecoveryTests(CatalogMixin):
    def test_failed_rollback_blocks_dispatch_and_survives_restart(self):
        for had_password in (False, True):
            with self.subTest(had_password=had_password), tempfile.TemporaryDirectory() as td:
                class Vault(FakeKeyring):
                    armed = False
                    def set_password(self, service, account, value):
                        if self.armed and value == 'old_pass':
                            raise RuntimeError('vault locked')
                        super().set_password(service, account, value)
                    def delete_password(self, service, account):
                        if self.armed:
                            raise RuntimeError('vault locked')
                        super().delete_password(service, account)
                vault = Vault()
                root = f'{self.origin}/opds'
                svc = cat.CalibreCatalogService(td, keyring_factory=lambda: vault)
                svc.save_config(root, 'old_user' if had_password else '',
                                'old_pass' if had_password else None)
                vault.armed = True
                with mock.patch('utils.atomic_json_write', side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(cat.CatalogError, 'recover'):
                        svc.save_config(root, 'new_user', 'new_pass')
                self.assertTrue(svc.get_config().get('recovery_required'))
                reopened = cat.CalibreCatalogService(td, keyring_factory=lambda: vault)
                self.assertTrue(reopened.get_config().get('recovery_required'))
                for service in (svc, reopened):
                    with self.assertRaises(cat.CatalogError):
                        service.browse_root_page()
                self.assertEqual(self.server.seen_paths, [])
                vault.armed = False
                reopened.save_config(root, 'new_user', 'new_pass')
                self.assertFalse(reopened.get_config().get('recovery_required', False))
                self.server.route('/opds', feed_xml('F', []))
                reopened.browse_root_page()
                self.assertEqual(self.server.seen_auth[-1],
                                 ('/opds', opds.OpdsAuth('new_user', 'new_pass').header_value()))
                self.server.seen_paths.clear()
                self.server.seen_auth.clear()


class NavigationTests(CatalogMixin):
    def _feed(self, title, entries_xml):
        return f"<?xml version='1.0'?><feed {ATOM}><title>{title}</title>{entries_xml}</feed>".encode()

    def test_mixed_entry_keeps_navigation_but_mints_one_book_preview(self):
        acquisition = '<link href="book.epub" rel="http://opds-spec.org/acquisition" type="application/epub+zip"/>'
        navigation = '<link href="section" rel="subsection" type="application/atom+xml"/>'
        for mixed_first in (True, False):
            with self.subTest(mixed_first=mixed_first):
                def entry(mixed):
                    return ('<entry><id>urn:synthetic:mixed</id><title>Book</title>'
                            + acquisition + (navigation if mixed else '') + '</entry>')
                self.server.route('/opds', self._feed('Root', entry(mixed_first)
                    + '<link href="page2" rel="next" type="application/atom+xml"/>'))
                self.server.route('/page2', self._feed('Detail', entry(not mixed_first)))
                svc = self.make_service(keyring_factory=lambda: None)
                svc.save_config(f'{self.origin}/opds')
                first = svc.browse_root_page()
                second = svc.browse_next_page(first['next_token'])
                entries = first['entries'] + second['entries']
                self.assertEqual(sum(bool(e['preview_token']) for e in entries), 1)
                self.assertEqual(sum(len(e['navigation']) for e in entries), 1)
                self.assertEqual(self.server.seen_paths[-2:], ['/opds', '/page2'])

    def test_returned_navigation_tokens_survive_all_page_mints(self):
        entries = ''.join(
            f'<entry><id>urn:synthetic:section{i}</id><title>S{i}</title>'
            f'<link href="section{i}" rel="subsection" type="application/atom+xml"/></entry>'
            for i in range(4))
        self.server.route('/opds', self._feed('Root', entries
            + '<link href="page2" rel="next" type="application/atom+xml"/>'
            + '<link href="/opds" rel="start" type="application/atom+xml"/>'))
        for i in range(4):
            self.server.route(f'/section{i}', self._feed(f'S{i}', ''))
        svc = self.make_service(keyring_factory=lambda: None, max_nav_tokens=3)
        svc.save_config(f'{self.origin}/opds')
        page = svc.browse_root_page()
        usable = [nav for e in page['entries'] for nav in e['navigation'] if nav['nav_token']]
        self.assertEqual(len(usable), 1)
        self.assertTrue(page['next_token'])
        self.assertTrue(page['start_token'])
        section = svc.browse_next_page(usable[0]['nav_token'])
        self.assertEqual(section['feed_url'], usable[0]['url'])

    def test_root_relative_category_to_book_preview(self):
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        root_xml = self._feed("Root",
            '<entry><id>urn:synthetic:cat-shared</id><title>Fiction</title>'
            '<link href="cat/fiction" rel="subsection" type="application/atom+xml" title="Fiction"/></entry>')
        cat_xml = self._feed("Cat",
            '<entry><id>urn:synthetic:book1</id><title>Cat Book</title><author><name>A</name></author>'
            '<link href="catbook.epub" rel="http://opds-spec.org/acquisition" type="application/epub+zip"/></entry>'
            '<entry><id>urn:synthetic:cat-shared</id><title>Subsection</title>'
            '<link href="sub" rel="subsection" type="application/atom+xml" title="Subsection"/></entry>')
        self.server.route("/opds", root_xml)
        self.server.route("/cat/fiction", cat_xml)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        page = svc.browse_root_page()
        self.assertEqual(len(page["entries"]), 1)
        entry = page["entries"][0]
        self.assertIsNone(entry["preview_token"])
        self.assertFalse(entry["has_acquisition"])
        nav = entry["navigation"]
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0]["title"], "Fiction")
        self.assertEqual(nav[0]["url"], f"{self.origin}/cat/fiction")
        self.assertIsNotNone(nav[0]["nav_token"])
        self.assertFalse(nav[0]["loop_refused"])
        page2 = svc.browse_next_page(nav[0]["nav_token"])
        titles = [e["title"] for e in page2["entries"]]
        self.assertIn("Cat Book", titles)
        self.assertIn("Subsection", titles)
        book = next(e for e in page2["entries"] if e["title"] == "Cat Book")
        self.assertIsNotNone(book["preview_token"])
        preview = svc.get_metadata_preview(book["preview_token"])
        self.assertEqual(preview["provider"]["title"], "Cat Book")
        self.assertEqual(self.server.seen_paths, ["/opds", "/cat/fiction"])
        self.assertTrue(all(".epub" not in p for p in self.server.seen_paths))
        self.assertEqual(self.server.seen_auth,
                         [("/opds", AUTH), ("/cat/fiction", AUTH)])

    def test_relative_nav_resolved_against_final_feed_url(self):
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        base_xml = self._feed("Base",
            '<entry><id>urn:synthetic:cat1</id><title>Fiction</title>'
            '<link href="fiction" rel="subsection" type="application/atom+xml" title="Fiction"/></entry>')
        self.server.route("/opds", b"", status=302,
                          headers={"Location": "/final/base"})
        self.server.route("/final/base", base_xml)
        self.server.route("/final/fiction",
                          feed_xml("F", [("9", "Nine", "A", "S", "9", "9.epub",
                                          "application/epub+zip")]))
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        page = svc.browse_root_page()
        self.assertEqual(page["feed_url"], f"{self.origin}/final/base")
        nav = page["entries"][0]["navigation"][0]
        self.assertEqual(nav["url"], f"{self.origin}/final/fiction")
        page2 = svc.browse_next_page(nav["nav_token"])
        self.assertEqual([e["title"] for e in page2["entries"]], ["Nine"])
        self.assertEqual(self.server.seen_paths,
                         ["/opds", "/final/base", "/final/fiction"])
        self.assertEqual(self.server.seen_auth,
                         [("/opds", AUTH), ("/final/base", AUTH),
                          ("/final/fiction", AUTH)])

    def test_cross_origin_category_target_and_redirect_review_without_credentials(self):
        other = start_server()
        self.addCleanup(stop_server, other)
        b_origin = other.origin
        other.route("/cat", feed_xml(
            "B", [("2", "Two", "A", "S", "2", "2.epub", "application/epub+zip")]))
        root_xml = self._feed("Root",
            '<entry><id>urn:synthetic:catX</id><title>Cross</title>'
            f'<link href="{b_origin}/cat" rel="subsection" type="application/atom+xml" title="Cross"/></entry>')
        self.server.route("/opds", root_xml)
        vault = FakeKeyring()
        svc = self.make_service(keyring_factory=lambda: vault)
        svc.save_config(f"{self.origin}/opds", username=USER, password=PW)
        page = svc.browse_root_page()
        nav = page["entries"][0]["navigation"][0]
        self.assertEqual(nav["url"], f"{b_origin}/cat")
        review = svc.browse_next_page(nav["nav_token"])
        self.assertTrue(review["review_required"])
        self.assertEqual(review["destination_url"], f"{b_origin}/cat")
        self.assertEqual(other.seen_paths, [])
        approval = svc.approve_cross_origin_review(review["review_token"])
        page2 = svc.browse_next_page(approval["nav_token"])
        self.assertEqual([e["title"] for e in page2["entries"]], ["Two"])
        self.assertEqual(other.seen_paths, ["/cat"])
        self.assertEqual(other.seen_auth, [("/cat", None)])
        self.assertEqual(self.server.seen_auth, [("/opds", AUTH)])
        # Redirect variant: same-origin /hop -> B /final2 (exact redirect target).
        other2 = start_server()
        self.addCleanup(stop_server, other2)
        other2.route("/final2", feed_xml(
            "B", [("3", "Three", "A", "S", "3", "3.epub", "application/epub+zip")]))
        self.server.route("/hop", b"", status=302,
                          headers={"Location": f"{other2.origin}/final2"})
        hop_xml = self._feed("Root",
            '<entry><id>urn:synthetic:hopcat</id><title>Hop</title>'
            '<link href="hop" rel="subsection" type="application/atom+xml" title="Hop"/></entry>')
        self.server.route("/opds", hop_xml)
        svc2 = self.make_service(keyring_factory=lambda: vault)
        svc2.save_config(f"{self.origin}/opds", username=USER, password=PW)
        page3 = svc2.browse_root_page()
        nav2 = page3["entries"][0]["navigation"][0]
        self.assertEqual(nav2["url"], f"{self.origin}/hop")
        review2 = svc2.browse_next_page(nav2["nav_token"])
        self.assertTrue(review2["review_required"])
        self.assertEqual(review2["destination_url"], f"{other2.origin}/final2")
        self.assertEqual(other2.seen_paths, [])
        approval2 = svc2.approve_cross_origin_review(review2["review_token"])
        page4 = svc2.browse_next_page(approval2["nav_token"])
        self.assertEqual([e["title"] for e in page4["entries"]], ["Three"])
        self.assertEqual(other2.seen_paths, ["/final2"])
        self.assertEqual(other2.seen_auth, [("/final2", None)])

    def test_unsupported_relation_or_type_mints_no_navigation(self):
        xml = self._feed("Mixed",
            '<entry><id>urn:synthetic:u1</id><title>HTML</title>'
            '<link href="page.html" rel="alternate" type="text/html" title="Page"/></entry>'
            '<entry><id>urn:synthetic:u2</id><title>Img</title>'
            '<link href="img.png" rel="related" type="image/png" title="Cover"/></entry>'
            '<entry><id>urn:synthetic:u3</id><title>PDFSec</title>'
            '<link href="doc.pdf" rel="subsection" type="application/pdf" title="Doc"/></entry>'
            '<entry><id>urn:synthetic:u4</id><title>RelAtom</title>'
            '<link href="rel" rel="related" type="application/atom+xml" title="Rel"/></entry>'
            '<entry><id>urn:synthetic:u5</id><title>EpubBook</title><author><name>A</name></author>'
            '<link href="b.epub" rel="http://opds-spec.org/acquisition" type="application/epub+zip"/></entry>'
            '<entry><id>urn:synthetic:u6</id><title>ValidParams</title>'
            '<link href="cat6" rel="subsection" type="application/atom+xml; charset=utf-8" title="Cat6"/></entry>'
            '<entry><id>urn:synthetic:u7</id><title>AltAtom</title>'
            '<link href="cat7" rel="alternate" type="application/atom+xml" title="Cat7"/></entry>'
            '<entry><id>urn:synthetic:u8</id><title>DefaultAtom</title>'
            '<link href="cat8" type="application/atom+xml" title="Cat8"/></entry>')
        self.server.route("/opds", xml)
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        by_title = {e["title"]: e for e in page["entries"]}
        for title in ("HTML", "Img", "PDFSec", "RelAtom"):
            self.assertEqual(by_title[title]["navigation"], [])
            self.assertIsNone(by_title[title]["preview_token"])
        self.assertEqual(by_title["EpubBook"]["navigation"], [])
        self.assertIsNotNone(by_title["EpubBook"]["preview_token"])
        for title, href in (("ValidParams", "cat6"), ("AltAtom", "cat7"),
                            ("DefaultAtom", "cat8")):
            nav = by_title[title]["navigation"]
            self.assertEqual(len(nav), 1)
            self.assertIsNotNone(nav[0]["nav_token"])
            self.assertEqual(nav[0]["url"], f"{self.origin}/{href}")
            self.assertIsNone(by_title[title]["preview_token"])
        self.assertEqual(self.server.seen_paths, ["/opds"])

    def test_nav_cycle_page_cap_and_token_guards_apply_before_fetch(self):
        root_xml = self._feed("Root",
            '<entry><id>urn:synthetic:cyclecat</id><title>Cat</title>'
            '<link href="cat" rel="subsection" type="application/atom+xml" title="Cat"/></entry>')
        cat_xml = self._feed("Cat",
            '<entry><id>urn:synthetic:back</id><title>Back</title>'
            '<link href="/opds" rel="subsection" type="application/atom+xml" title="Back"/></entry>')
        self.server.route("/opds", root_xml)
        self.server.route("/cat", cat_xml)
        svc = self.make_service(keyring_factory=lambda: None)
        svc.save_config(f"{self.origin}/opds")
        page = svc.browse_root_page()
        token = page["entries"][0]["navigation"][0]["nav_token"]
        cat_page = svc.browse_next_page(token)
        back = cat_page["entries"][0]["navigation"][0]
        self.assertIsNone(back["nav_token"])
        self.assertTrue(back["loop_refused"])
        self.assertEqual(self.server.seen_paths, ["/opds", "/cat"])
        # Page cap fires before fetch.
        svc_cap = self.make_service(
            keyring_factory=lambda: None, max_pages_per_session=1)
        svc_cap.save_config(f"{self.origin}/opds")
        cap_page = svc_cap.browse_root_page()
        cap_token = cap_page["entries"][0]["navigation"][0]["nav_token"]
        before = list(self.server.seen_paths)
        with self.assertRaises(cat.CatalogError):
            svc_cap.browse_next_page(cap_token)
        self.assertEqual(self.server.seen_paths, before)
        # Stale revision fires before fetch.
        svc_stale = self.make_service(keyring_factory=lambda: None)
        svc_stale.save_config(f"{self.origin}/opds")
        stale_page = svc_stale.browse_root_page()
        stale_token = stale_page["entries"][0]["navigation"][0]["nav_token"]
        svc_stale.save_config(f"{self.origin}/opds")
        before = list(self.server.seen_paths)
        with self.assertRaises(cat.CatalogError):
            svc_stale.browse_next_page(stale_token)
        self.assertEqual(self.server.seen_paths, before)
        # Consumed token fires before fetch.
        svc_cons = self.make_service(keyring_factory=lambda: None)
        svc_cons.save_config(f"{self.origin}/opds")
        cons_page = svc_cons.browse_root_page()
        cons_token = cons_page["entries"][0]["navigation"][0]["nav_token"]
        svc_cons.browse_next_page(cons_token)
        before = list(self.server.seen_paths)
        with self.assertRaises(cat.CatalogError):
            svc_cons.browse_next_page(cons_token)
        self.assertEqual(self.server.seen_paths, before)
        # Expiry fires before fetch.
        svc_exp = self.make_service(
            keyring_factory=lambda: None, session_ttl=0.15)
        svc_exp.save_config(f"{self.origin}/opds")
        exp_page = svc_exp.browse_root_page()
        exp_token = exp_page["entries"][0]["navigation"][0]["nav_token"]
        time.sleep(0.25)
        before = list(self.server.seen_paths)
        with self.assertRaises(cat.CatalogError):
            svc_exp.browse_next_page(exp_token)
        self.assertEqual(self.server.seen_paths, before)


if __name__ == "__main__":
    unittest.main()

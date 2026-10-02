"""Scheme casing is independent of strict credential verification."""
import base64
import unittest

from utils import check_basic_auth


class BasicAuthSchemeTests(unittest.TestCase):
    def test_all_basic_scheme_cases_accept_same_exact_unicode_credentials(self):
        token=base64.b64encode('名前:秘密:colon'.encode()).decode()
        for scheme in ('Basic','basic','BASIC','bAsIc'):
            with self.subTest(scheme=scheme):
                self.assertTrue(check_basic_auth(scheme+' '+token,'名前','秘密:colon'))
                self.assertFalse(check_basic_auth(scheme+' '+token,'名前','wrong'))
                self.assertFalse(check_basic_auth(scheme+' '+token,'wrong','秘密:colon'))

    def test_wrong_scheme_and_malformed_payload_still_reject(self):
        for header in ('Bearer dTpw','Basics dTpw','basicx dTpw','Basic','Basic !!!',
                       'basic dTpw!!!','BASIC bm9jb2xvbg==','basic /w==',''):
            with self.subTest(header=header):
                self.assertFalse(check_basic_auth(header,'u','p'))

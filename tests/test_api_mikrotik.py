import unittest
from types import SimpleNamespace

from app.api_mikrotik import (
    _has_ambiguous_pppoe_matches,
    _one_pppoe_match_per_user,
    _routers_for_olt,
)


class AmbiguousPppoeMatchesTests(unittest.TestCase):
    def test_multiple_pppoe_users_for_one_onu_are_ambiguous(self):
        matches = [
            {"mac_compact": "80F1A87AE278", "username": "first-user"},
            {"mac_compact": "AABBCCDDEEFF", "username": "second-user"},
        ]

        self.assertTrue(_has_ambiguous_pppoe_matches(matches))

    def test_one_pppoe_user_across_sessions_is_not_ambiguous(self):
        matches = [
            {"mac_compact": "80F1A87AE278", "username": "one-user"},
            {"mac_compact": "AABBCCDDEEFF", "username": "one-user"},
        ]

        self.assertFalse(_has_ambiguous_pppoe_matches(matches))

    def test_multiple_sessions_for_the_configured_user_are_not_ambiguous(self):
        matches = [
            {"mac_compact": "80F1A87AE278", "username": "one-user"},
            {"mac_compact": "80F1A87AE278", "username": "ONE-USER"},
        ]

        self.assertFalse(_has_ambiguous_pppoe_matches(matches))

    def test_no_matches_are_not_ambiguous(self):
        self.assertFalse(_has_ambiguous_pppoe_matches([]))

    def test_multiple_sessions_for_one_user_are_collapsed_to_one(self):
        matches = [
            {"username": "one-user", "router": "ROUTER", "mac_compact": "AABBCCDDEEFF", "ip": "10.0.0.2"},
            {"username": "ONE-USER", "router": "ROUTER", "mac_compact": "001122334455", "ip": "10.0.0.3"},
        ]

        self.assertEqual(len(_one_pppoe_match_per_user(matches)), 1)

    def test_explicit_olt_router_link_overrides_name_matching(self):
        purwosari = SimpleNamespace(id=1, name="ROUTER PURWOSARI")
        other = SimpleNamespace(id=2, name="ROUTER OTHER")
        olt = SimpleNamespace(vendor="cdata", name="EPON PURWOSARI", mikrotik_router_id=2)

        self.assertEqual(_routers_for_olt([purwosari, other], olt), [other])

    def test_unlinked_cdata_olt_keeps_name_matching(self):
        purwosari = SimpleNamespace(id=1, name="ROUTER PURWOSARI")
        other = SimpleNamespace(id=2, name="ROUTER OTHER")
        olt = SimpleNamespace(vendor="cdata", name="EPON PURWOSARI", mikrotik_router_id=None)

        self.assertEqual(_routers_for_olt([purwosari, other], olt), [purwosari])


if __name__ == "__main__":
    unittest.main()

from django.test import SimpleTestCase, override_settings

from eth_account import Account

from ..whitelist import get_whitelisted_safes, is_whitelist_enabled, is_whitelisted


class TestWhitelist(SimpleTestCase):
    def setUp(self):
        self.address = Account.create().address
        self.other_address = Account.create().address

    def test_disabled(self):
        self.assertEqual(get_whitelisted_safes(), frozenset())
        self.assertFalse(is_whitelist_enabled())
        self.assertTrue(is_whitelisted(self.address))
        self.assertTrue(is_whitelisted(self.other_address))

    def test_enabled(self):
        with override_settings(WHITELISTED_SAFES=frozenset({self.address})):
            self.assertEqual(get_whitelisted_safes(), frozenset({self.address}))
            self.assertTrue(is_whitelist_enabled())
            self.assertTrue(is_whitelisted(self.address))
            self.assertFalse(is_whitelisted(self.other_address))

    def test_settings_not_cached(self):
        self.assertTrue(is_whitelisted(self.other_address))
        with override_settings(WHITELISTED_SAFES=frozenset({self.address})):
            self.assertFalse(is_whitelisted(self.other_address))
            with override_settings(WHITELISTED_SAFES=frozenset({self.other_address})):
                self.assertTrue(is_whitelisted(self.other_address))
                self.assertFalse(is_whitelisted(self.address))
        self.assertFalse(is_whitelist_enabled())
        self.assertTrue(is_whitelisted(self.address))

    def test_checksummed_address_required(self):
        with override_settings(WHITELISTED_SAFES=frozenset({self.address})):
            self.assertFalse(is_whitelisted(self.address.lower()))

import os
from unittest import TestCase

from django.conf import settings

from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.utils import fast_to_checksum_address

from ..whitelist import parse_whitelisted_safes, validate_whitelist_config

CHECKSUM_ADDRESS = "0x5aFE3855358E112B5647B952709E6165e1c1eEEe"


class TestParseWhitelistedSafes(TestCase):
    def test_empty(self):
        self.assertEqual(parse_whitelisted_safes([]), frozenset())
        self.assertEqual(parse_whitelisted_safes(["", "  "]), frozenset())

    def test_whitespace_and_trailing_comma(self):
        # `env.list("X")` with `X="0x...,"` returns `["0x...", ""]`
        self.assertEqual(
            parse_whitelisted_safes([f"  {CHECKSUM_ADDRESS} ", ""]),
            frozenset({CHECKSUM_ADDRESS}),
        )

    def test_lowercase_and_uppercase_accepted(self):
        self.assertEqual(
            parse_whitelisted_safes([CHECKSUM_ADDRESS.lower()]),
            frozenset({CHECKSUM_ADDRESS}),
        )
        self.assertEqual(
            parse_whitelisted_safes(["0x" + CHECKSUM_ADDRESS[2:].upper()]),
            frozenset({CHECKSUM_ADDRESS}),
        )

    def test_duplicates_in_different_case(self):
        self.assertEqual(
            parse_whitelisted_safes(
                [
                    CHECKSUM_ADDRESS,
                    CHECKSUM_ADDRESS.lower(),
                    "0x" + CHECKSUM_ADDRESS[2:].upper(),
                ]
            ),
            frozenset({CHECKSUM_ADDRESS}),
        )

    def test_bad_checksum_rejected(self):
        # Swap the case of one letter
        bad_checksum = CHECKSUM_ADDRESS.replace("aFE", "afE")
        with self.assertRaisesRegex(ValueError, f"{bad_checksum} has an invalid"):
            parse_whitelisted_safes([CHECKSUM_ADDRESS, bad_checksum])

    def test_bad_hex_rejected(self):
        for value in (
            "0xBAD",
            CHECKSUM_ADDRESS[2:],  # No 0x
            CHECKSUM_ADDRESS + "0",  # Too long
            "0x" + "g" * 40,  # Not hex
            "safe.eth",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "is not a valid address"),
            ):
                parse_whitelisted_safes([value])

    def test_zero_address_rejected(self):
        with self.assertRaisesRegex(ValueError, "zero address"):
            parse_whitelisted_safes([NULL_ADDRESS])

    def test_many_addresses(self):
        addresses = [fast_to_checksum_address(os.urandom(20)) for _ in range(1_500)]
        self.assertEqual(parse_whitelisted_safes(addresses), frozenset(addresses))


class TestValidateWhitelistConfig(TestCase):
    def test_l1_rejected(self):
        with self.assertRaisesRegex(ValueError, "ETH_L2_NETWORK"):
            validate_whitelist_config(frozenset({CHECKSUM_ADDRESS}), l2_network=False)

    def test_l2_accepted(self):
        validate_whitelist_config(frozenset({CHECKSUM_ADDRESS}), l2_network=True)

    def test_empty_whitelist_always_valid(self):
        for l2_network in (True, False):
            with self.subTest(l2_network=l2_network):
                validate_whitelist_config(frozenset(), l2_network=l2_network)


class TestWhitelistSetting(TestCase):
    def test_default_empty(self):
        self.assertEqual(settings.WHITELISTED_SAFES, frozenset())

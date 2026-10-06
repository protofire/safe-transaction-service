"""
Helpers for `WHITELISTED_SAFES`. An empty whitelist means stock behaviour: every
Safe is indexed.

Settings are read on every call (no caching), so `override_settings` works.
"""

from django.conf import settings

from eth_typing import ChecksumAddress


def get_whitelisted_safes() -> frozenset[ChecksumAddress]:
    return settings.WHITELISTED_SAFES


def is_whitelist_enabled() -> bool:
    return bool(get_whitelisted_safes())


def is_whitelisted(address: ChecksumAddress) -> bool:
    """
    :param address: Checksummed address. No case normalization is done
    :return: `True` if the whitelist is disabled or `address` is whitelisted
    """
    whitelisted_safes = get_whitelisted_safes()
    return not whitelisted_safes or address in whitelisted_safes

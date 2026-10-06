"""
Parsing and validation of the `WHITELISTED_SAFES` setting.

Imported from the settings module, so it must not import Django or models.
"""

import re
from collections.abc import Iterable

from eth_typing import ChecksumAddress
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.utils import fast_is_checksum_address, fast_to_checksum_address

ADDRESS_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")


def parse_whitelisted_safes(values: Iterable[str]) -> frozenset[ChecksumAddress]:
    """
    :param values: Raw addresses, e.g. from `env.list`. Empty items are skipped
    :return: Checksummed, deduplicated addresses
    :raises ValueError: for an invalid address, the zero address or a mixed-case
        address with an invalid checksum
    """
    addresses = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        if not ADDRESS_REGEX.match(value):
            raise ValueError(f"WHITELISTED_SAFES: {value} is not a valid address")
        hex_part = value[2:]
        # Mixed case means EIP-55 checksum was intended, catch typos
        if (
            hex_part != hex_part.lower()
            and hex_part != hex_part.upper()
            and not fast_is_checksum_address(value)
        ):
            raise ValueError(f"WHITELISTED_SAFES: {value} has an invalid checksum")
        address = fast_to_checksum_address(value)
        if address == NULL_ADDRESS:
            raise ValueError("WHITELISTED_SAFES: zero address is not allowed")
        addresses.add(address)
    return frozenset(addresses)


def validate_whitelist_config(
    whitelist: frozenset[ChecksumAddress], l2_network: bool
) -> None:
    """
    :raises ValueError: if the whitelist is enabled on a non L2 (tracing) network
    """
    if whitelist and not l2_network:
        raise ValueError(
            "WHITELISTED_SAFES is only supported with ETH_L2_NETWORK=True (L2 event indexing)"
        )

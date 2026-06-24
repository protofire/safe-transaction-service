from eth_typing import ChecksumAddress

CRYPTO_KITTIES_CONTRACT_ADDRESSES = {
    "0x06012c8cf97BEaD5deAe237070F9587f8E7A266d",  # Mainnet
}

ENS_CONTRACTS_WITH_TLD = {
    "0x57f1887a8BF19b14fC0dF6Fd9B2acc9Af147eA85": "eth",  # ENS .eth registrar (Every network)
}

# Number of `encryptKeyHash == 0` `Transfer` logs a SINGLE SRC20 transfer emits when both
# parties are keyless. The SRC20 standard base emits one placeholder for the sender and one
# for the recipient => 2; a single transfer between keyless parties therefore produces 2
# byte-identical logs, which the indexer must collapse back to one logical transfer.
#
# IMPORTANT: this count is STRUCTURAL and PROVIDER-INDEPENDENT. Registered intelligence
# providers emit their own `encryptKeyHash != 0` logs, which the indexer counts separately
# (they never inflate the keyless divisor). Do NOT "correct" these values to
# `n_providers + 2` when providers are registered — the keyless placeholder count stays 2.
#
# Addresses verified on-chain (Seismic testnet, chainId 5124). Tokens not listed here fall
# back to `SRC20_DEFAULT_KEYLESS_PLACEHOLDER_LOGS` (1 => keep every log, never under-count).
SRC20_KEYLESS_PLACEHOLDER_LOGS: dict[ChecksumAddress, int] = {
    "0x790701048922E265105fd6a4467a2901c2201C43": 2,  # standard base (sender + recipient)
    "0x91eDd1341dCb5515EaF5eF34338BB2460241F3Bf": 2,  # standard base
    "0xd21813092Ed81dF1e2D05DadA358386885208b4D": 2,  # standard base (ref tx 0x2e563ed0)
    "0xDDe870c4fc7a4712994812D82aB87e9b8855eDFb": 1,  # recipient-only, keyed (kh = keyHash(to))
}

# Tokens absent from the map keep every log: a wrong divisor could drop real transfers,
# while keeping all logs only risks an occasional over-count, which is the safer failure.
SRC20_DEFAULT_KEYLESS_PLACEHOLDER_LOGS = 1


def get_src20_keyless_placeholder_logs(address: ChecksumAddress) -> int:
    """
    :param address: SRC20 token contract address (checksummed)
    :return: Number of `encryptKeyHash == 0` logs a single transfer emits when both parties
        are keyless, used as the divisor for the keyless counting path. Defaults to
        ``SRC20_DEFAULT_KEYLESS_PLACEHOLDER_LOGS`` (keep-all) for unknown tokens.
    """
    return SRC20_KEYLESS_PLACEHOLDER_LOGS.get(
        address, SRC20_DEFAULT_KEYLESS_PLACEHOLDER_LOGS
    )

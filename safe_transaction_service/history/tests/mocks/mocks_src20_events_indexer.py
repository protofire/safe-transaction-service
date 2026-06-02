from hexbytes import HexBytes

# Raw SRC20 `Transfer` log:
# Transfer(address indexed from, address indexed to, bytes32 indexed encryptKeyHash, bytes encryptedAmount)
# topics[0] is the SRC20 transfer topic (distinct from the ERC20/721 one).
# `data` is the ABI-encoded dynamic `bytes encryptedAmount` (here 0xdeadbeefcafe).
log_receipt_mock = [
    {
        "address": "0xD84dbd5138D2297959Ae56602Bd5B2A035bb3F59",
        "blockHash": HexBytes(
            "0xe630ebf8c8ff2397896f23de27fd6e9f280d4ede613acbf788d545cc0c5194e8"
        ),
        "blockNumber": 6,
        "data": HexBytes(
            "0x0000000000000000000000000000000000000000000000000000000000000020"
            "0000000000000000000000000000000000000000000000000000000000000006"
            "deadbeefcafe0000000000000000000000000000000000000000000000000000"
        ),
        "logIndex": 0,
        "removed": False,
        "topics": [
            HexBytes(
                "0x80ffa007a69623ef13594f5e8178eee6c4ef2d0cba74c08329e879f695b7d3f6"
            ),
            HexBytes(
                "0x00000000000000000000000022d491bde2303f2f43325b2108d26f1eaba1e32b"
            ),
            HexBytes(
                "0x0000000000000000000000006e5b7093ac36ea61da02fd1cceecf56fd6626d48"
            ),
            HexBytes(
                "0x1111111111111111111111111111111111111111111111111111111111111111"
            ),
        ],
        "transactionHash": HexBytes(
            "0x53a869a24855dcae97e6cea9069eb7a2e57c45a3538081947a1af7a7da38d627"
        ),
        "transactionIndex": 0,
    }
]

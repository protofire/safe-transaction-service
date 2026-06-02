from django.test import TestCase

from eth_account import Account

from ..serializers import TransferType, TransferWithTokenInfoResponseSerializer


class TestTransferWithTokenInfoResponseSerializer(TestCase):
    def setUp(self) -> None:
        self.serializer = TransferWithTokenInfoResponseSerializer()
        self.token_address = Account.create().address

    def _transfer(self, token: dict | None):
        # SRC20 transfers are surfaced like an ERC20 transfer with `value=0`
        return {
            "token_address": self.token_address,
            "_value": 0,
            "_token_id": None,
            "token": token,
        }

    def test_src20_token_classified_as_src20_transfer(self):
        obj = self._transfer({"src20": True, "decimals": 0})
        self.assertEqual(self.serializer.get_type(obj), TransferType.SRC20_TRANSFER.name)

    def test_erc20_token_not_classified_as_src20(self):
        obj = self._transfer({"src20": False, "decimals": 18})
        self.assertEqual(
            self.serializer.get_type(obj), TransferType.ERC20_TRANSFER.name
        )

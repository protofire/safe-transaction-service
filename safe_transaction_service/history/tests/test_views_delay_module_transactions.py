from django.urls import reverse

from eth_account import Account
from rest_framework import status
from rest_framework.test import APITestCase

from .factories import DelayModuleTransactionFactory


class TestDelayModuleTransactionListView(APITestCase):
    def get_url(self, delay_module_address: str, query: str = "") -> str:
        return (
            reverse(
                "v1:history:delay-module-transactions", args=(delay_module_address,)
            )
            + query
        )

    def test_invalid_address(self):
        response = self.client.get(self.get_url("0x1234"), format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_empty(self):
        response = self.client.get(
            self.get_url(Account.create().address), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_fields(self):
        delay_module_transaction = DelayModuleTransactionFactory(
            queue_nonce=2, value=7, data=b"\x12\x34", operation=1
        )

        response = self.client.get(
            self.get_url(delay_module_transaction.module), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ethereum_tx = delay_module_transaction.ethereum_tx
        self.assertEqual(
            response.json()["results"],
            [
                {
                    "queueNonce": "2",
                    "txHash": delay_module_transaction.module_tx_hash,
                    "to": delay_module_transaction.to,
                    "value": "7",
                    "data": "0x1234",
                    "operation": 1,
                    "transactionHash": ethereum_tx.tx_hash,
                    "blockNumber": ethereum_tx.block_id,
                    "executionDate": response.json()["results"][0]["executionDate"],
                    "proposer": ethereum_tx._from,
                }
            ],
        )

    def test_filter_by_module_and_queue_nonce(self):
        delay_module_address = Account.create().address
        for queue_nonce in (3, 1, 2):
            DelayModuleTransactionFactory(
                module=delay_module_address, queue_nonce=queue_nonce
            )
        DelayModuleTransactionFactory()  # Another Delay Modifier

        response = self.client.get(self.get_url(delay_module_address), format="json")
        self.assertEqual(
            [result["queueNonce"] for result in response.json()["results"]],
            ["1", "2", "3"],
        )

        response = self.client.get(
            self.get_url(delay_module_address, "?queue_nonce__gte=2&queue_nonce__lt=3"),
            format="json",
        )
        self.assertEqual(
            [result["queueNonce"] for result in response.json()["results"]], ["2"]
        )

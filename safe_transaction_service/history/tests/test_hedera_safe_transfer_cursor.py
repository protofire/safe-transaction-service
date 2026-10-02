from django.test import TestCase

from ..models import HederaSafeTransferCursor
from .factories import SafeContractFactory


class TestHederaSafeTransferCursor(TestCase):
    def test_create_and_get_or_create_is_one_per_safe(self):
        safe_contract = SafeContractFactory()
        cursor, created = HederaSafeTransferCursor.objects.get_or_create(
            safe_contract=safe_contract
        )
        self.assertTrue(created)
        self.assertIsNone(cursor.hedera_account_id)
        self.assertIsNone(cursor.last_consensus_timestamp)

        cursor.hedera_account_id = "0.0.10127045"
        cursor.last_consensus_timestamp = "1787164650.039440869"
        cursor.save()

        cursor_again, created_again = HederaSafeTransferCursor.objects.get_or_create(
            safe_contract=safe_contract
        )
        self.assertFalse(created_again)
        self.assertEqual(cursor_again.hedera_account_id, "0.0.10127045")

    def test_related_name_from_safe_contract(self):
        safe_contract = SafeContractFactory()
        HederaSafeTransferCursor.objects.create(safe_contract=safe_contract)
        self.assertIsNotNone(safe_contract.hedera_transfer_cursor)

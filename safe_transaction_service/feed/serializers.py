from rest_framework import serializers

from safe_transaction_service.history.serializers import (
    AllTransactionsSchemaSerializerV2,
)


class GlobalAllTransactionsResponseSerializer(serializers.Serializer):
    next_cursor = serializers.CharField(required=False, allow_null=True)
    data = AllTransactionsSchemaSerializerV2(many=True) 
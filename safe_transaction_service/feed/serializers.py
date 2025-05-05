from rest_framework import serializers

from safe_transaction_service.history.serializers import (
    AllTransactionsSchemaSerializerV2,
)


class GlobalAllTransactionsResponseSerializer(serializers.Serializer):
    """
    Schema for the response of the AllTransactionsGlobalView.
    This serializer defines the structure of the API response.
    """
    metrics = serializers.DictField(
        help_text="Performance metrics for the request",
        child=serializers.DictField(),
        required=False
    )
    
    next_cursor = serializers.CharField(
        help_text="Cursor for the next page of results. Null if there are no more results.",
        allow_null=True
    )
    
    data = serializers.ListField(
        help_text="List of transactions data",
        child=serializers.DictField(allow_empty=True)
    )
    
    class Meta:
        fields = ('metrics', 'next_cursor', 'data') 
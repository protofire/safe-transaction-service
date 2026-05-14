from rest_framework import serializers


class SummarySerializer(serializers.Serializer):
    total_safes = serializers.IntegerField()
    total_multisig_txs = serializers.IntegerField()
    total_module_txs = serializers.IntegerField()
    total_erc20_transfers = serializers.IntegerField()
    total_erc721_transfers = serializers.IntegerField()
    first_safe_created = serializers.DateTimeField(allow_null=True)
    last_safe_created = serializers.DateTimeField(allow_null=True)
    chain_id = serializers.IntegerField()
    service_version = serializers.CharField()


class ActiveSafesSerializer(serializers.Serializer):
    window = serializers.CharField()
    active_safes = serializers.IntegerField()
    computed_at = serializers.DateTimeField()


class SafeCreationSerializer(serializers.Serializer):
    period = serializers.DateField()
    count = serializers.IntegerField()


class ActiveOwnersSerializer(serializers.Serializer):
    window = serializers.CharField()
    active_owners = serializers.IntegerField()
    computed_at = serializers.DateTimeField()


class TxVolumeSerializer(serializers.Serializer):
    window = serializers.CharField()
    total_multisig_txs = serializers.IntegerField()
    executed_multisig_txs = serializers.IntegerField()
    module_txs = serializers.IntegerField()
    total_value_wei = serializers.CharField()
    avg_confirmations = serializers.FloatField()
    computed_at = serializers.DateTimeField()


class SafeSegmentsSerializer(serializers.Serializer):
    personal = serializers.IntegerField()
    team = serializers.IntegerField()
    enterprise = serializers.IntegerField()
    with_modules = serializers.IntegerField()
    avg_threshold = serializers.FloatField()
    avg_owners = serializers.FloatField()
    computed_at = serializers.DateTimeField()


class TopTokenSerializer(serializers.Serializer):
    address = serializers.CharField()
    total_balance = serializers.CharField()
    safe_count = serializers.IntegerField()


class TVLSerializer(serializers.Serializer):
    total_safes_with_balance = serializers.IntegerField()
    native_balance_wei = serializers.CharField()
    erc20_token_count = serializers.IntegerField()
    top_tokens = TopTokenSerializer(many=True)
    computed_at = serializers.DateTimeField()


class TopTokenVolumeSerializer(serializers.Serializer):
    address = serializers.CharField()
    transfer_count = serializers.IntegerField()
    total_value = serializers.CharField()


class TokenVolumeSerializer(serializers.Serializer):
    window = serializers.CharField()
    total_erc20_transfers = serializers.IntegerField()
    unique_tokens = serializers.IntegerField()
    top_tokens = TopTokenVolumeSerializer(many=True)
    computed_at = serializers.DateTimeField()

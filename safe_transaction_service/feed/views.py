from django.shortcuts import render

# Create your views here.

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from django.db.models import Q, QuerySet
from django.utils.dateparse import parse_datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import ParseError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response

from safe_transaction_service.history.models import (
    ERC20Transfer,
    ERC721Transfer,
    EthereumTx,
    InternalTx,
    ModuleTransaction,
    MultisigTransaction,
    SafeRelevantTransaction,
)
# Import Token model from the correct app
from safe_transaction_service.tokens.models import Token

from safe_transaction_service.history.serializers import (
    AllTransactionsSchemaSerializerV2, TransferDict,
    EthereumTxWithTransfersResponseSerializer,
    SafeModuleTransactionWithTransfersResponseSerializer,
    SafeMultisigTransactionWithTransfersResponseSerializerV2,
)
from safe_transaction_service.history.services import TransactionServiceProvider

from .pagination import KeysetPagination
from .permissions import IsInternalApi
# Import the new response serializer
from .serializers import GlobalAllTransactionsResponseSerializer
# Import our new service
from .services import GlobalTransactionService

logger = logging.getLogger(__name__)


def get_all_txs_from_identifiers_global(
    identifiers: List[str],
) -> List[Dict[str, Any]]:
    """
    Fetch transactions and related data based on ethereum_tx_ids globally.
    This is adapted from TransactionService.get_all_txs_from_identifiers
    but removes the `safe_address` filter where appropriate.

    TODO: Caching is omitted for simplicity in this initial implementation.
          It should be added back for performance.
    TODO: Error handling and logging can be improved.
    """

    if not identifiers:
        return []

    logger.debug(
        "[%s] Getting %d txs from identifiers globally",
        __name__,
        len(identifiers),
    )

    # Fetch Multisig Transactions
    # Note: Multisig Txs are inherently linked to a safe, but we fetch based on eth tx hash
    multisig_txs_queryset = (
        MultisigTransaction.objects.filter(ethereum_tx_id__in=identifiers)
        .with_confirmations_required()
        .prefetch_related("confirmations")
        .select_related("ethereum_tx__block")
        .order_by("-nonce", "-created") # Ordering might not be strictly needed here
    )
    ids_with_multisig_txs: Dict[str, List[MultisigTransaction]] = {}
    for tx in multisig_txs_queryset:
        ids_with_multisig_txs.setdefault(tx.ethereum_tx_id, []).append(tx)
    logger.debug(
        "[%s] Got %d Multisig txs from identifiers globally",
        __name__,
        multisig_txs_queryset.count(),
    )

    # Fetch Module Transactions
    module_txs_queryset = ModuleTransaction.objects.filter(
        internal_tx__ethereum_tx__in=identifiers
    ).select_related("internal_tx__ethereum_tx", "internal_tx__ethereum_tx__block")
    ids_with_module_txs: Dict[str, List[ModuleTransaction]] = {}
    for tx in module_txs_queryset:
        ids_with_module_txs.setdefault(tx.internal_tx.ethereum_tx_id, []).append(tx)
    logger.debug(
        "[%s] Got %d Module txs from identifiers globally",
        __name__,
        module_txs_queryset.count(),
    )

    # Fetch plain Ethereum Transactions (for incoming/outgoing native/token transfers not covered by Multisig/Module)
    plain_ethereum_txs = {
        tx.tx_hash: [tx]
        for tx in EthereumTx.objects.filter(tx_hash__in=identifiers).select_related("block")
    }
    logger.debug(
        "[%s] Got %d Plain Ethereum txs from identifiers globally",
        __name__,
        len(plain_ethereum_txs),
    )

    # Fetch all potentially relevant transfers (ERC20, ERC721, Ether)
    # Select related `ethereum_tx__block` to get block info efficiently
    erc20_queryset = ERC20Transfer.objects.filter(
        ethereum_tx__in=identifiers
    ).select_related("ethereum_tx__block")
    erc721_queryset = ERC721Transfer.objects.filter(
        ethereum_tx__in=identifiers
    ).select_related("ethereum_tx__block")
    ether_queryset = InternalTx.objects.filter(
        ethereum_tx__in=identifiers, value__gt=0
    ).select_related("ethereum_tx__block")

    transfer_dict = {}
    collected_token_addresses = set()
    # Reconstruct TransferDict structure manually, including block info
    for transfer in erc20_queryset:
        collected_token_addresses.add(transfer.address)
        transfer_data = {
            "block": transfer.ethereum_tx.block.number, # Key is 'block', value is integer block number
            "transaction_hash": transfer.ethereum_tx_id,
            "to": transfer.to,
            "_from": transfer._from,
            "_value": transfer.value,
            "execution_date": transfer.ethereum_tx.block.timestamp, # Correct key for this field
            "_token_id": None,
            "token_address": transfer.address,
            "token": None, # Placeholder, will be filled later
            "_log_index": transfer.log_index,
            "_trace_address": None,
            "transfer_type": "erc20",
        }
        transfer_dict.setdefault(transfer.ethereum_tx_id, []).append(transfer_data)

    for transfer in erc721_queryset:
        collected_token_addresses.add(transfer.address)
        transfer_data = {
            "block": transfer.ethereum_tx.block.number, # Key is 'block', value is integer block number
            "transaction_hash": transfer.ethereum_tx_id,
            "to": transfer.to,
            "_from": transfer._from,
            "_value": None,
            "execution_date": transfer.ethereum_tx.block.timestamp, # Correct key for this field
            "_token_id": transfer.token_id,
            "token_address": transfer.address,
            "token": None, # Placeholder, will be filled later
            "_log_index": transfer.log_index,
            "_trace_address": None,
            "transfer_type": "erc721",
        }
        transfer_dict.setdefault(transfer.ethereum_tx_id, []).append(transfer_data)

    for transfer in ether_queryset:
        transfer_data = {
            "block": transfer.ethereum_tx.block.number, # Key is 'block', value is integer block number
            "transaction_hash": transfer.ethereum_tx_id,
            "to": transfer.to,
            "_from": transfer._from,
            "_value": transfer.value,
            "execution_date": transfer.ethereum_tx.block.timestamp, # Correct key for this field
            "_token_id": None,
            "token_address": None,
            "token": None,
            "_log_index": -1,
            "_trace_address": transfer.trace_address,
            "transfer_type": "ether",
        }
        transfer_dict.setdefault(transfer.ethereum_tx_id, []).append(transfer_data)

    # Fetch Token objects for all collected addresses
    valid_token_addresses = {addr for addr in collected_token_addresses if addr}
    tokens_by_address = {
        token.address: token
        for token in Token.objects.filter(address__in=valid_token_addresses)
    }

    # Assign Token objects to the transfer data
    for tx_hash in transfer_dict:
        for transfer_data in transfer_dict[tx_hash]:
            if transfer_data.get("token_address"):
                transfer_data["token"] = tokens_by_address.get(transfer_data["token_address"])

    logger.debug(
        "[%s] Got %d transfers from identifiers globally",
        __name__,
        sum(len(v) for v in transfer_dict.values()),
    )

    # Combine results based on the original identifiers list order (if important)
    # This part needs careful merging, respecting that one eth_tx might contain multiple relevant items
    # (e.g., a MultisigTx and its transfers)
    final_results = []
    processed_multisig = set()
    processed_module = set()

    # Iterate through the original identifiers to somewhat maintain order/grouping by eth_tx
    for identifier in identifiers:
        multisig_list = ids_with_multisig_txs.get(identifier, [])
        module_list = ids_with_module_txs.get(identifier, [])
        plain_eth_tx_list = plain_ethereum_txs.get(identifier, [])
        transfers_list = transfer_dict.get(identifier, [])

        # Add Multisig transactions (and attach their transfers)
        for multisig_tx in multisig_list:
            if multisig_tx.safe_tx_hash not in processed_multisig:
                multisig_tx.transfers = transfers_list # Attach all transfers for this eth_tx
                final_results.append(multisig_tx)
                processed_multisig.add(multisig_tx.safe_tx_hash)

        # Add Module transactions (and attach their transfers)
        for module_tx in module_list:
            # Use internal_tx_id as unique identifier for module txs
            if module_tx.internal_tx_id not in processed_module:
                module_tx.transfers = transfers_list # Attach all transfers for this eth_tx
                final_results.append(module_tx)
                processed_module.add(module_tx.internal_tx_id)

        # Add Plain Ethereum Tx if it wasn't part of a Multisig or Module execution
        # and has relevant transfers. This handles incoming transfers primarily.
        if not multisig_list and not module_list and plain_eth_tx_list and transfers_list:
            eth_tx = plain_eth_tx_list[0]
            eth_tx.transfers = transfers_list # Attach transfers
            final_results.append(eth_tx)

    # TODO: Re-sort final_results if strict chronological order across all types is needed
    # The current approach groups by ethereum tx hash primarily.
    # Sorting here might be computationally expensive.
    # final_results.sort(key=lambda x: x.execution_date or x.timestamp)

    # Manually serialize based on the type of transaction object
    serialized_data = []
    for tx in final_results:
        if isinstance(tx, ModuleTransaction):
            serializer = SafeModuleTransactionWithTransfersResponseSerializer(tx)
        elif isinstance(tx, MultisigTransaction):
            serializer = SafeMultisigTransactionWithTransfersResponseSerializerV2(tx)
        elif isinstance(tx, EthereumTx):
            # Need to attach transfers manually if not already done
            # (The previous logic attached it, but ensure it's consistent)
            if not hasattr(tx, 'transfers'):
                tx.transfers = transfer_dict.get(tx.tx_hash, [])
            serializer = EthereumTxWithTransfersResponseSerializer(tx)
        else:
            logger.warning(
                "[%s] Unrecognized transaction type in final_results: %s",
                __name__,
                type(tx),
            )
            continue # Skip unknown types
        serialized_data.append(serializer.data)

    # Previous attempt (Incorrectly used the documentation-only serializer)
    # serializer = AllTransactionsSchemaSerializerV2(final_results, many=True)
    # serialized_data = serializer.data
    # tx_service_provider = TransactionServiceProvider()
    # serialized_data = tx_service_provider.serialize_all_txs_v2(final_results)

    return serialized_data


@extend_schema(
    tags=["stats"],
    parameters=[
        OpenApiParameter(
            "updated_after",
            location="query",
            description="Return items created or modified after this timestamp (ISO 8601 format).",
            required=False,
            type=OpenApiTypes.DATETIME,
        ),
        OpenApiParameter(
            "limit",
            location="query",
            description="Number of items to retrieve (default: 1000, max: 10000).",
            required=False,
            type=OpenApiTypes.INT,
        ),
        OpenApiParameter(
            "cursor",
            location="query",
            description="Base64 encoded cursor for keyset pagination (timestamp|id).",
            required=False,
            type=OpenApiTypes.STR,
        ),
    ],
    responses={
        200: OpenApiResponse(
            description="List of all executed transactions across all Safes, paginated.",
            response=GlobalAllTransactionsResponseSerializer,
        ),
        400: OpenApiResponse(description="Invalid query parameter format (cursor, updated_after, limit)."),
        403: OpenApiResponse(description="Forbidden. Stats API not enabled or invalid/missing API key."),
    },
)
class AllTransactionsGlobalView(ListAPIView):
    """
    Returns all *executed* transactions across all Safes on the chain, ordered by execution time (oldest first).
    This endpoint uses keyset pagination based on `(timestamp, id)`.

    Requires `ENABLE_STATS_API=True` in settings and a valid `X-Internal-API-Key` header.
    """
    permission_classes = [IsInternalApi]
    pagination_class = KeysetPagination

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transaction_service = GlobalTransactionService()

    def get_queryset(self) -> QuerySet:
        """
        Returns the base queryset used for pagination. This efficiently gets just the
        identifiers and timestamps needed for pagination without loading all transaction data.
        """
        # We only need to fetch minimal data for pagination
        return (
            SafeRelevantTransaction.objects
            .select_related("ethereum_tx")
            .only("id", "timestamp", "ethereum_tx_id")
        )

    def list(self, request, *args, **kwargs):
        """
        Process the request and return paginated transaction data.
        This implementation is optimized to minimize database queries and memory usage.
        """
        # Get the base queryset for pagination
        queryset = self.filter_queryset(self.get_queryset())

        # Get paginated identifiers using cursor pagination
        page_items = self.paginate_queryset(queryset)
        
        if not page_items:
            # Return empty response with pagination structure
            return Response(OrderedDict([("next_cursor", None), ("data", [])]))

        # Extract ethereum_tx_ids from the paginated items
        ethereum_tx_ids = [item.ethereum_tx_id for item in page_items]

        # Fetch full transaction data using our optimized service
        transactions = self.transaction_service.get_transactions_with_transfers_from_tx_ids(
            ethereum_tx_ids
        )

        # Build response with pagination info
        next_cursor = None
        if self.paginator.has_next and page_items:
            # Create cursor from the last item in current page
            next_cursor = self.paginator.encode_cursor(page_items[-1])

        # Return the final response
        return Response(
            OrderedDict([
                ("next_cursor", next_cursor),
                ("data", transactions),
            ])
        )

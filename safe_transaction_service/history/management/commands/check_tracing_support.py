from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from safe_eth.eth import EthereumClient

from safe_transaction_service.utils.ethereum import get_chain_id

from ...whitelist import get_whitelisted_safes


class Command(BaseCommand):
    help = """
    Check the tracing node (ETHEREUM_TRACING_NODE_URL) supports the queries used to index
    WHITELISTED_SAFES on L1 (ETH_L2_NETWORK=False) and is on the same chain as
    ETHEREUM_NODE_URL. Nothing is checked on L2 or with an empty whitelist (stock).
    """

    def handle(self, *args, **options):
        whitelisted_safes = get_whitelisted_safes()
        if settings.ETH_L2_NETWORK or not whitelisted_safes:
            self.stdout.write("Tracing support check not needed")
            return

        tracing = EthereumClient(settings.ETHEREUM_TRACING_NODE_URL)
        try:
            tracing_chain_id = tracing.get_chain_id()
            chain_id = get_chain_id()
            if tracing_chain_id != chain_id:
                raise CommandError(
                    f"Tracing node chainId {tracing_chain_id} does not match "
                    f"EthereumRPC chainId {chain_id}"
                )

            block_number = tracing.current_block_number - 1
            if not settings.ETH_INTERNAL_NO_FILTER:
                # Same query as `InternalTxIndexer` with a whitelist
                tracing.tracing.trace_filter(
                    from_block=block_number,
                    to_block=block_number,
                    from_address=[min(whitelisted_safes)],
                )
            if (
                settings.ETH_INTERNAL_NO_FILTER
                or settings.ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS
            ):
                tracing.tracing.trace_blocks([block_number])
        except CommandError:
            raise
        except Exception as e:
            raise CommandError(f"Tracing node check failed: {e}") from e

        self.stdout.write(
            self.style.SUCCESS(f"Tracing node chainId {chain_id} supports tracing")
        )

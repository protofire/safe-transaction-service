from io import StringIO
from unittest import mock
from unittest.mock import MagicMock, PropertyMock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from safe_eth.eth.ethereum_client import EthereumClient, TracingManager
from web3.exceptions import Web3RPCError

SAFE_ADDRESS = "0x5aFE3855358E112B5647B952709E6165e1c1eEEe"
COMMAND_MODULE = (
    "safe_transaction_service.history.management.commands.check_tracing_support"
)


@override_settings(
    ETH_L2_NETWORK=False,
    WHITELISTED_SAFES=frozenset({SAFE_ADDRESS}),
    ETH_INTERNAL_NO_FILTER=False,
    ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS=0,
)
@mock.patch(f"{COMMAND_MODULE}.get_chain_id", return_value=1)
@mock.patch.object(EthereumClient, "get_chain_id", autospec=True, return_value=1)
@mock.patch.object(
    EthereumClient, "current_block_number", new_callable=PropertyMock, return_value=101
)
@mock.patch.object(TracingManager, "trace_blocks", autospec=True, return_value=[[]])
@mock.patch.object(TracingManager, "trace_filter", autospec=True, return_value=[])
class TestCheckTracingSupport(TestCase):
    def call_command(self) -> str:
        buf = StringIO()
        call_command("check_tracing_support", stdout=buf)
        return buf.getvalue()

    def test_not_needed(
        self,
        trace_filter_mock: MagicMock,
        trace_blocks_mock: MagicMock,
        *args,
    ):
        for settings in (
            {"ETH_L2_NETWORK": True},
            # Stock L1
            {"WHITELISTED_SAFES": frozenset()},
        ):
            with self.subTest(**settings), override_settings(**settings):
                self.assertIn("not needed", self.call_command())
        trace_filter_mock.assert_not_called()
        trace_blocks_mock.assert_not_called()

    def test_trace_filter(
        self,
        trace_filter_mock: MagicMock,
        trace_blocks_mock: MagicMock,
        *args,
    ):
        self.assertIn("supports tracing", self.call_command())
        trace_filter_mock.assert_called_once_with(
            mock.ANY, from_block=100, to_block=100, from_address=[SAFE_ADDRESS]
        )
        trace_blocks_mock.assert_not_called()

    def test_trace_block(
        self,
        trace_filter_mock: MagicMock,
        trace_blocks_mock: MagicMock,
        *args,
    ):
        with override_settings(ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS=10):
            self.assertIn("supports tracing", self.call_command())
        trace_filter_mock.assert_called_once()
        trace_blocks_mock.assert_called_once_with(mock.ANY, [100])

        # Nodes without `trace_filter`
        trace_filter_mock.reset_mock()
        trace_blocks_mock.reset_mock()
        with override_settings(ETH_INTERNAL_NO_FILTER=True):
            self.assertIn("supports tracing", self.call_command())
        trace_filter_mock.assert_not_called()
        trace_blocks_mock.assert_called_once_with(mock.ANY, [100])

    def test_errors(
        self,
        trace_filter_mock: MagicMock,
        trace_blocks_mock: MagicMock,
        current_block_number_mock: MagicMock,
        tracing_get_chain_id_mock: MagicMock,
        get_chain_id_mock: MagicMock,
    ):
        trace_filter_mock.side_effect = Web3RPCError(
            "the method trace_filter does not exist/is not available"
        )
        with self.assertRaisesMessage(CommandError, "trace_filter does not exist"):
            self.call_command()
        trace_filter_mock.side_effect = None

        trace_blocks_mock.side_effect = ValueError("Batch request error")
        with (
            override_settings(ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS=10),
            self.assertRaisesMessage(CommandError, "Batch request error"),
        ):
            self.call_command()

        get_chain_id_mock.return_value = 11155111
        with self.assertRaisesMessage(
            CommandError,
            "Tracing node chainId 1 does not match EthereumRPC chainId 11155111",
        ):
            self.call_command()

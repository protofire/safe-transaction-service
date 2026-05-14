import logging

from django.core.files import File
from django.core.management import BaseCommand, CommandError

from safe_eth.eth import EthereumClient, get_auto_ethereum_client
from safe_eth.safe.safe_deployments import default_safe_deployments, safe_deployments

from config.settings.base import STATICFILES_DIRS
from safe_transaction_service.contracts.models import Contract

logger = logging.getLogger(__name__)

TRUSTED_FOR_DELEGATE_CALL = [
    "MultiSendCallOnly",
    "SignMessageLib",
    "SafeMigration",
]

# Push Donut (chainId 42101) non-canonical v1.3.0 deployment.
# See setup_service.py for the matching master copy / proxy factory override.
# Remove this when the official Safe Singleton Factory lands on Push and
# Safe contracts are redeployed at canonical addresses.
PUSH_DONUT_CHAIN_ID = 42101
PUSH_DONUT_V1_3_0_DEPLOYMENTS: list[tuple[str, str, str]] = [
    # (version, contract_name, address)
    ("1.3.0", "GnosisSafe", "0x4cf6773D4C56d82129BdC1187A62972AD0805aD7"),
    ("1.3.0", "GnosisSafeL2", "0xd6C8A29565168288cAEA51fEE946A1014f4d8809"),
    ("1.3.0", "ProxyFactory", "0x40D84e121f9C9F0d834a12577764FcAa4D428278"),
    ("1.3.0", "CompatibilityFallbackHandler", "0x2d8083FB02BD152b04411B6400F3dCb15eC57fD5"),
    ("1.3.0", "DefaultCallbackHandler", "0x13a8Ac34441925f02637b22eD951E026F2AA9f00"),
    ("1.3.0", "MultiSend", "0xcEB6813f410Ab1c13a5e7B988219c8EB1334D69d"),
    ("1.3.0", "MultiSendCallOnly", "0x960A2D8e6E30668B35314a183B158b652b275c38"),
    ("1.3.0", "SignMessageLib", "0x55F1DaDFD75Ae576E37A59e7e523bec62CF1274F"),
    ("1.3.0", "CreateCall", "0xC6F80f7E4593AB6180691DbE7699A850b39eE893"),
    ("1.3.0", "SimulateTxAccessor", "0x06F839F3D47Ba3FAFcbC033994D5cd4244116D5A"),
]


def generate_safe_contract_display_name(contract_name: str, version: str) -> str:
    """
    Generates the display name for Safe contract.
    Append Safe at the beginning if the contract name doesn't contain Safe word and append the contract version at the end.

    :param contract_name:
    :param version:
    :return: display_name
    """
    # Remove gnosis word
    contract_name = contract_name.replace("Gnosis", "")
    if "safe" not in contract_name.lower():
        return f"Safe: {contract_name} {version}"
    else:
        return f"{contract_name} {version}"


class Command(BaseCommand):
    help = "Create or update the Safe contracts with default data. A different logo can be provided"

    def add_arguments(self, parser):
        parser.add_argument(
            "--safe-version", type=str, help="Contract version", required=False
        )
        parser.add_argument(
            "--force-update-contracts",
            help="Update all the information related to the Safe contracts",
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "--logo-path",
            type=str,
            help="Path of new logo",
            required=False,
            default=f"{STATICFILES_DIRS[0]}/safe/safe_contract_logo.png",
        )

    def handle(self, *args, **options):
        """
        Command to create or update Safe contracts with default data. A different contract logo can be provided.

        :param args:
        :param options: Safe version and logo path
        :return:
        """
        safe_version = options["safe_version"]
        force_update_contracts = options["force_update_contracts"]
        logo_path = options["logo_path"]
        ethereum_client = get_auto_ethereum_client()
        chain_id = ethereum_client.get_chain_id()
        logo_file = File(open(logo_path, "rb"))
        if not safe_version:
            versions = list(safe_deployments.keys())
        elif safe_version in safe_deployments:
            versions = [safe_version]
        else:
            raise CommandError(
                f"Wrong Safe version {safe_version}, supported versions {safe_deployments.keys()}"
            )

        if force_update_contracts:
            # update all safe contract names
            queryset = Contract.objects.update_or_create
        else:
            # only update the contracts with empty values
            queryset = Contract.objects.get_or_create

        logger.info("Creating default Safe contracts from chain")

        if chain_id == PUSH_DONUT_CHAIN_ID:
            logger.info(
                "Push Donut (chainId 42101) detected — using non-canonical v1.3.0 addresses"
            )
            chain_deployments = [
                (v, n, a)
                for (v, n, a) in PUSH_DONUT_V1_3_0_DEPLOYMENTS
                if v in versions
            ]
            self._create_or_update_contracts_from_deployments(
                chain_deployments, queryset, force_update_contracts, logo_file
            )
            return

        chain_deployments = self._get_default_deployments_by_version_on_chain(
            versions, ethereum_client
        )

        if chain_deployments:
            self._create_or_update_contracts_from_deployments(
                chain_deployments, queryset, force_update_contracts, logo_file
            )
        else:
            logger.warning(f"No deployment was found for the network {chain_id}")

    @staticmethod
    def _get_deployments_by_chain_and_version(
        versions: list[str], chain_id: str
    ) -> list[tuple[str, str, str]]:
        """
        Get the list of contracts for the given versions and chain.

        :param versions: list of versions
        :param chain_id: chain id
        :return: list of (version, contract_name, contract_address)
        """
        chain_deployments: list[tuple[str, str, str]] = []
        for version in versions:
            for contract_name, addresses in safe_deployments[version].items():
                for contract_address in addresses.get(chain_id, []):
                    chain_deployments.append((version, contract_name, contract_address))

        return chain_deployments

    @staticmethod
    def _get_default_deployments_by_version_on_chain(
        versions: list[str], ethereum_client: EthereumClient
    ) -> list[tuple[str, str, str]]:
        """
        Get the default deployments by version actually deployed on chain.

        :param versions: list of versions
        :param ethereum_client: Ethereum client
        :return: list of (version, contract_name, contract_address)
        """
        chain_deployments: list[tuple[str, str, str]] = []
        for version in versions:
            for contract_name, addresses in default_safe_deployments[version].items():
                for contract_address in addresses:
                    if ethereum_client.is_contract(contract_address):
                        chain_deployments.append(
                            (version, contract_name, contract_address)
                        )

        return chain_deployments

    @staticmethod
    def _create_or_update_contracts_from_deployments(
        deployments: list[tuple[str, str, str]],
        queryset,
        force_update_contracts: bool,
        logo_file: File,
    ) -> None:
        """
        Create or update contracts from given deployments list.
        """
        for version, contract_name, contract_address in deployments:
            display_name = generate_safe_contract_display_name(contract_name, version)
            contract, created = queryset(
                address=contract_address,
                defaults={
                    "name": contract_name,
                    "display_name": display_name,
                    "trusted_for_delegate_call": contract_name
                    in TRUSTED_FOR_DELEGATE_CALL,
                },
            )

            if not created:
                # Remove previous logo file
                contract.logo.delete(save=True)
                # update name only for contracts with empty names
                if not force_update_contracts and contract.name == "":
                    contract.display_name = display_name
                    contract.name = contract_name

            try:
                contract.logo.save(f"{contract.address}.png", logo_file)
                contract.save()
            except OSError:
                logger.warning("Logo cannot be stored.")

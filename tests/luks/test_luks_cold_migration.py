from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationPlanExecError
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms, verify_luks_encryption
from utilities.resources import create_and_store_resource
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


class _LuksColdMigrationBase:
    """Shared setup for LUKS cold migration tests.

    Provides common storagemap, networkmap, and plan creation methods.
    Subclasses define scenario-specific migration execution and validation.

    VM requirements:
        The source VM must have a LUKS-encrypted partition (e.g. sda3 with crypto_LUKS)
        and a key file (e.g. /etc/luks-key) configured in /etc/crypttab for unattended
        boot. Without a key file, the VM prompts for the passphrase interactively on
        boot, which blocks SSH access after migration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create StorageMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (BaseProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Raises:
            AssertionError: If StorageMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (BaseProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            multus_network_name (dict[str, str]): Multus network name for network mapping.

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        source_provider_data: dict[str, Any],
    ) -> None:
        """Create MTV Plan with LUKS decryption secret.

        Creates a Kubernetes Secret containing the LUKS passphrase and references
        it in the VM spec so forklift mounts it for virt-v2v during conversion.
        The passphrase is resolved from per-VM config override first, then from
        the provider configuration (providers.json).

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            source_provider_data (dict[str, Any]): Provider configuration from providers.json.

        Raises:
            ValueError: If LUKS passphrase is empty.
            AssertionError: If Plan creation fails.
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        vms = [dict(vm) for vm in prepared_plan["virtual_machines"]]
        for vm in vms:
            vm.pop("luks", None)
            luks_passphrase = vm.pop("luks_passphrase", None)
            if luks_passphrase is None:
                luks_passphrase = source_provider_data["luks_passphrase"]
            if not luks_passphrase:
                raise ValueError(f"LUKS passphrase is empty for VM '{vm['name']}'")

            luks_secret = create_and_store_resource(
                client=ocp_admin_client,
                fixture_store=fixture_store,
                resource=Secret,
                namespace=target_namespace,
                string_data={"key": luks_passphrase},
            )
            vm["luks"] = {"name": luks_secret.name}

        self.__class__.plan_resource = create_plan_resource(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            virtual_machines_list=vms,
            warm_migration=prepared_plan["warm_migration"],
        )
        assert self.plan_resource, "Plan creation failed"


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_luks_cold_migration"])],
    indirect=True,
    ids=["luks-cold-correct-key"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestLuksColdMigration(_LuksColdMigrationBase):
    """Cold migration with correct LUKS disk decryption passphrase.

    Validates that LUKS-encrypted VMs migrate successfully when the correct
    passphrase is provided, and that disk encryption remains active post-migration.
    """

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute cold migration with LUKS decryption.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.

        Raises:
            MigrationTimeoutError: If migration fails to complete within timeout.
        """
        execute_migration(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager | None",
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            source_provider_data (dict[str, Any]): Source provider configuration.
            source_vms_namespace (str): Source VMs namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connections for connectivity testing.

        Raises:
            AssertionError: If any VM validation checks fail.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_data=source_provider_data,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )

    def test_verify_luks_encryption(
        self,
        prepared_plan: dict[str, Any],
        vm_ssh_connections: "SSHConnectionManager",
        source_provider_data: dict[str, Any],
    ) -> None:
        """Verify LUKS encryption is active on migrated VM.

        SSHs into the migrated VM and checks lsblk JSON output for crypto_LUKS
        filesystem type, confirming encryption survived the migration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            vm_ssh_connections (SSHConnectionManager): SSH connection manager.
            source_provider_data (dict[str, Any]): Provider configuration with guest credentials.

        Raises:
            AssertionError: If no LUKS-encrypted devices are found.
        """
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            source_vm_info = prepared_plan["source_vms_data"][vm_name]
            verify_luks_encryption(
                vm_name=vm_name,
                vm_ssh_connections=vm_ssh_connections,
                source_provider_data=source_provider_data,
                source_vm_info=source_vm_info,
            )


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_luks_cold_migration_wrong_key"])],
    indirect=True,
    ids=["luks-cold-wrong-key"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestLuksColdMigrationWrongKey(_LuksColdMigrationBase):
    """Cold migration with incorrect LUKS passphrase — expects failure at ImageConversion."""

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration — expects failure due to wrong LUKS passphrase.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.

        Raises:
            AssertionError: If migration does not raise MigrationPlanExecError.
        """
        with pytest.raises(MigrationPlanExecError, match="ImageConversion"):
            execute_migration(
                fixture_store=fixture_store,
                ocp_admin_client=ocp_admin_client,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )

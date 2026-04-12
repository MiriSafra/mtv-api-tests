"""Shared disk migration tests for RHEL VMs (MTV-676).

Tests VM-level migrateSharedDisks overrides in a single migration plan.
The owner VM (migrateSharedDisks=true) migrates the shared disk PVC, while
the consumer VM (migrateSharedDisks=false) skips it.
"""

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.shared_disk import verify_shared_disk_data
from utilities.utils import populate_vm_ids


@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_shared_disk_rhel_migration"],
        )
    ],
    indirect=True,
    ids=["shared-disk-rhel"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSharedDiskRhelMigration:
    """MTV-676: Cold migrate RHEL VMs with VM-level migrateSharedDisks overrides."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for both VMs."""
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for both VMs."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR with VM-level migrateSharedDisks overrides."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            migrate_shared_disks=prepared_plan["migrate_shared_disks"],
            target_power_state=prepared_plan["target_power_state"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Execute migration for both VMs in a single plan."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_shared_disk_data(
        self,
        prepared_plan,
        vm_ssh_connections,
        source_provider_data,
    ):
        """Verify shared disk read/write access from both VMs."""
        vm1_name = prepared_plan["virtual_machines"][0]["name"]
        vm2_name = prepared_plan["virtual_machines"][1]["name"]
        vm1_info = prepared_plan["source_vms_data"][vm1_name]
        vm2_info = prepared_plan["source_vms_data"][vm2_name]

        verify_shared_disk_data(
            vm1_name=vm1_name,
            vm2_name=vm2_name,
            vm_ssh_connections=vm_ssh_connections,
            source_provider_data=source_provider_data,
            vm1_info=vm1_info,
            vm2_info=vm2_info,
            shared_disk_device=prepared_plan["shared_disk_device"],
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate both migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )

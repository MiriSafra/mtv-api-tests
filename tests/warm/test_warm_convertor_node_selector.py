from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import py_config

from utilities.deep_inspection import DI_RESULTS_KEY, create_di_capture_callback
from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_migration_resource,
    create_plan_resource,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import populate_vm_ids
from utilities.warm_migration_scheduling import (
    create_warm_migration_scheduling_callback,
    run_default_scheduling_migration,
    run_selector_recovery_migration,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from ocp_resources.node import Node


@pytest.mark.vsphere
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_convertor_node_selector_pending"],
            id="pending-recovery-and-conversion-placement",
        ),
        pytest.param(
            py_config["tests_params"]["test_warm_convertor_node_selector_default"],
            id="default-scheduling-without-selector",
        ),
    ],
    indirect=True,
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestWarmConvertorNodeSelector:
    """Verify selector propagation to CDI importer pods and default warm scheduling.

    Purpose/Regression:
        Verify that Plan.spec.convertorNodeSelector constrains CDI importer and virt-v2v pods during
        vSphere warm VDDK migration. The regression scenario first leaves the selector unsatisfied,
        then labels a worker and verifies recovery. A baseline scenario confirms that omitting the
        field preserves CDI's default scheduling and does not add the VDDK ConfigMap selector key.

    Prerequisites:
        Register a vSphere 7 or 8 source provider with VDDK enabled and an OpenShift destination with
        CNV/CDI VDDK importer support. Provide accessible destination storage and a usable migration
        network. Supply a disposable, powered-on RHEL 8 VM with one SCSI boot disk and working guest
        tools; an equivalent VM may be used. Enable warm migration prerequisites such as CBT, use a
        separate target namespace for each scenario, and ensure a Ready, schedulable worker can satisfy
        the CDI importer's existing nodeSelector constraints. The operator must be able to inspect Pods
        and ConfigMaps and temporarily label a worker node. Use a dedicated destination Multus network
        attachment when the test VM needs one.

    Test plan:
        1. Confirm the disposable source VM is visible in provider inventory and meets warm migration
           prerequisites. Create StorageMap and NetworkMap resources, then create a warm Plan with a
           selector label absent from every node. Confirm the Plan reaches Ready.
        2. Start a warm Migration with a future cutover. Inspect the VDDK ConfigMap and confirm its
           vddk-node-selector data decodes to the Plan selector. Confirm every current CDI importer Pod
           is Pending with PodScheduled=Unschedulable and has the same nodeSelector.
        3. Add the selector label to a Ready, schedulable worker that satisfies the importer's other
           nodeSelector entries. Confirm importer Pods are assigned to that worker. Allow cutover to
           finish and confirm the virt-v2v Pod has the Plan selector and status.nodeName set to that
           worker. Confirm migration success and VDDK ConfigMap removal.
        4. Repeat in a separate namespace with a warm Plan that omits ConvertorNodeSelector. Confirm the
           Plan reaches Ready, importer Pods receive nodes through default scheduling, and any Plan
           VDDK ConfigMap lacks vddk-node-selector. Confirm migration success and ConfigMap removal.
        5. Verify the destination VM and guest checks. Delete both Plans, Migrations, StorageMaps,
           NetworkMaps, destination VMs, disks/PVCs, and the two target namespaces. Restore the worker's
           original labels and remove any destination NetworkAttachmentDefinitions created for this test.

    Expected result:
        With the selector set, every observed importer and virt-v2v pod contains the Plan selector and
        runs on the labeled worker after the initial Unschedulable state; the Plan succeeds and its VDDK
        ConfigMap is deleted. Without the field, importers schedule normally and no VDDK ConfigMap
        contains vddk-node-selector. For failures, inspect Plan and Migration conditions, importer and
        conversion Pod nodeSelector/nodeName and scheduling events, VDDK ConfigMap data, and relevant
        Forklift/CDI controller logs. Importer scheduling waits up to one minute; migration completion
        uses the configured plan_wait_timeout.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    convertor_node_selector: dict[str, str] | None
    conversion_pod_placements: list[dict[str, Any]]
    importer_pod_placements: list[dict[str, Any]]

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create the storage mapping for the warm migration.

        Args:
            prepared_plan (dict[str, Any]): Prepared source VM configuration.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift client for resource creation.
            source_provider (BaseProvider): Registered vSphere source provider.
            destination_provider (OCPProvider): OpenShift destination provider.
            source_provider_inventory (ForkliftInventory): Source inventory for VM storage lookup.
            target_namespace (str): Namespace for MTV mapping resources.
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
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create the network mapping for the warm migration.

        Args:
            prepared_plan (dict[str, Any]): Prepared source VM configuration.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift client for resource creation.
            source_provider (BaseProvider): Registered vSphere source provider.
            destination_provider (OCPProvider): OpenShift destination provider.
            source_provider_inventory (ForkliftInventory): Source inventory for VM network lookup.
            target_namespace (str): Namespace for MTV mapping resources.
            multus_network_name (dict[str, str]): Destination network attachment names by network ID.
        """
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
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        convertor_node_selector: dict[str, str] | None,
    ) -> None:
        """Create the warm Plan and verify its selector field and Ready condition.

        Args:
            prepared_plan (dict[str, Any]): Prepared Plan configuration and source VM data.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift client for Plan creation.
            source_provider (BaseProvider): Registered vSphere source provider.
            destination_provider (OCPProvider): OpenShift destination provider.
            source_provider_inventory (ForkliftInventory): Source inventory used to populate VM IDs.
            target_namespace (str): Namespace for MTV Plan and mapping resources.
            convertor_node_selector (dict[str, str] | None): Unique scheduling selector for the scenario.
        """
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)
        self.__class__.convertor_node_selector = convertor_node_selector
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
            vm_target_namespace=prepared_plan.get("_vm_target_namespace"),
            convertor_node_selector=convertor_node_selector,
        )

        plan_spec = self.plan_resource.instance.get("spec", {})
        if convertor_node_selector is None:
            assert "convertorNodeSelector" not in plan_spec, "Default Plan unexpectedly contains convertorNodeSelector"
        else:
            assert plan_spec.get("convertorNodeSelector") == convertor_node_selector

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        convertor_worker_nodes: list[Node] | None,
    ) -> None:
        """Run the migration and verify the configured scheduling behavior.

        Args:
            fixture_store (dict[str, Any]): Fixture store for Migration tracking and teardown.
            ocp_admin_client (DynamicClient): OpenShift client for Migration and Pod operations.
            target_namespace (str): Namespace containing the Plan and Migration.
            convertor_worker_nodes (list[Node] | None): Ready, schedulable workers for selector recovery.
        """
        selector = self.convertor_node_selector
        vm_target_namespace = self.plan_resource.instance.get("spec", {}).get("targetNamespace")
        self.__class__.conversion_pod_placements = []
        self.__class__.importer_pod_placements = []
        if selector is not None:
            assert convertor_worker_nodes, "Selector recovery requires an eligible worker node"

        create_migration_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )
        observer = create_warm_migration_scheduling_callback(
            plan=self.plan_resource,
            client=ocp_admin_client,
            importer_namespace=vm_target_namespace,
            importer_placements=self.importer_pod_placements,
            conversion_placements=self.conversion_pod_placements,
            di_callback=create_di_capture_callback(plan=self.plan_resource, fixture_store=fixture_store),
        )

        if selector is None:
            run_default_scheduling_migration(
                plan=self.plan_resource,
                importer_placements=self.importer_pod_placements,
                on_status_poll=observer,
            )
            return

        assert convertor_worker_nodes is not None
        run_selector_recovery_migration(
            plan=self.plan_resource,
            client=ocp_admin_client,
            importer_namespace=vm_target_namespace,
            selector=selector,
            candidate_worker_nodes=convertor_worker_nodes,
            importer_placements=self.importer_pod_placements,
            conversion_placements=self.conversion_pod_placements,
            on_status_poll=observer,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: dict[str, Any],
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify the migrated destination VM.

        Args:
            prepared_plan (dict[str, Any]): Prepared migration plan and VM configuration.
            source_provider (BaseProvider): Registered vSphere source provider.
            destination_provider (OCPProvider): OpenShift destination provider.
            source_provider_data (dict[str, Any]): Source provider connection/configuration data.
            source_vms_namespace (str): Source provider namespace for inventory data.
            source_provider_inventory (ForkliftInventory): Source inventory used for comparison.
            vm_ssh_connections (dict[str, Any]): SSH manager for guest-level destination checks.
            fixture_store (dict[str, Any]): Fixture store containing deep-inspection results.
        """
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
            plan_resource=self.plan_resource,
            di_results=fixture_store.get(DI_RESULTS_KEY),
        )

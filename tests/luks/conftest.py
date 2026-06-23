from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger

from utilities.resources import create_and_store_resource
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.forklift_inventory import ForkliftInventory

LOGGER = get_logger(name=__name__)


@pytest.fixture(scope="class")
def luks_vm_specs(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    source_provider_data: dict[str, Any],
    source_provider_inventory: "ForkliftInventory",
) -> list[dict[str, Any]]:
    """VM specs with LUKS secrets created and injected.

    Populates VM IDs from inventory, then resolves passphrase per-VM (from VM config
    override or provider-level fallback), creates a Kubernetes Secret for each VM,
    and returns the VM list with secret references ready for plan creation.

    Args:
        prepared_plan (dict[str, Any]): Test plan configuration with VM details.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Target namespace for migration resources.
        source_provider_data (dict[str, Any]): Provider configuration from providers.json.
        source_provider_inventory (ForkliftInventory): Source provider inventory for VM ID lookup.

    Returns:
        list[dict[str, Any]]: VM dicts with luks secret references and IDs injected.

    Raises:
        ValueError: If LUKS passphrase is empty for any VM.
    """
    populate_vm_ids(prepared_plan, source_provider_inventory)
    vms = [copy.deepcopy(vm) for vm in prepared_plan["virtual_machines"]]
    for vm in vms:
        vm.pop("luks", None)
        source = "per-VM config"
        luks_passphrase = vm.pop("luks_passphrase", None)
        if luks_passphrase is None:
            luks_passphrase = source_provider_data.get("luks_passphrase")
            source = "source_provider_data"
            if luks_passphrase:
                LOGGER.debug(f"Using provider-level LUKS passphrase for VM '{vm['name']}'")
        if not luks_passphrase:
            raise ValueError(f"LUKS passphrase empty/missing for VM '{vm['name']}' (resolved from {source})")

        luks_secret = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Secret,
            namespace=target_namespace,
            string_data={"key": luks_passphrase},
        )
        vm["luks"] = {"name": luks_secret.name}

    return vms

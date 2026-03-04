"""Shared disk migration utilities.

This module provides utilities for testing and validating shared disk migrations (MTV-4548).
Includes functions for verifying shared disk accessibility between VMs and PVC count validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from pyhelper_utils.shell import run_ssh_commands
from simple_logger.logger import get_logger

from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.ssh_utils import SSHConnectionManager

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(name=__name__)


def verify_shared_disk_data(
    vm1_name: str,
    vm2_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    vm1_info: dict[str, Any],
    vm2_info: dict[str, Any],
    shared_disk_device: str = "/dev/vdc",
) -> None:
    """Verify shared disk is accessible from both VMs by writing/reading data.

    NOTE: This test assumes the shared disk is already formatted with a filesystem
    and is unmounted (due to MTV-2200 limitation - virt-v2v cannot update fstab for
    shared disks, causing boot failure if mounted).

    Args:
        vm1_name (str): Name of the first VM (owner).
        vm2_name (str): Name of the second VM (consumer).
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        vm1_info (dict[str, Any]): VM1 information including OS type.
        vm2_info (dict[str, Any]): VM2 information including OS type.
        shared_disk_device (str): Shared disk device path. Defaults to "/dev/vdc".

    Raises:
        AssertionError: If shared disk data verification fails.
    """
    LOGGER.info(f"Verifying shared disk between {vm1_name} and {vm2_name}")

    vm1_user, vm1_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm1_info)
    vm2_user, vm2_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm2_info)

    ssh_vm1 = vm_ssh_connections.create(vm_name=vm1_name, username=vm1_user, password=vm1_pass)
    ssh_vm2 = vm_ssh_connections.create(vm_name=vm2_name, username=vm2_user, password=vm2_pass)

    mount_point = "/mnt/shared_disk"
    partition = f"{shared_disk_device}1"
    test_file_vm1 = f"{mount_point}/test-vm1.txt"
    test_file_vm2 = f"{mount_point}/test-vm2.txt"

    # VM1: Mount shared disk and write test data
    LOGGER.info(f"VM1: Mounting shared disk {partition}")
    with ssh_vm1:
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "mkdir", "-p", mount_point])
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "mount", partition, mount_point])

        # Write test data from VM1
        # Note: Using 'tee' instead of shell redirect (>) to avoid quoting issues over SSH.
        # Shell redirect can fail silently due to permission and escaping problems.
        LOGGER.info("VM1: Writing test data")
        run_ssh_commands(
            ssh_vm1.rrmngmnt_host, ["sh", "-c", f"echo 'Data from VM1' | sudo tee {test_file_vm1} > /dev/null"]
        )
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "sync"])

    # VM2: Mount shared disk and verify VM1's data
    LOGGER.info(f"VM2: Mounting shared disk {partition}")
    with ssh_vm2:
        run_ssh_commands(ssh_vm2.rrmngmnt_host, ["sudo", "mkdir", "-p", mount_point])
        run_ssh_commands(ssh_vm2.rrmngmnt_host, ["sudo", "mount", partition, mount_point])

        # Verify VM2 can read VM1's data
        LOGGER.info("VM2: Reading VM1's test data")
        results = run_ssh_commands(ssh_vm2.rrmngmnt_host, ["sudo", "cat", test_file_vm1])
        vm2_read_data = results[0].strip()
        assert "Data from VM1" in vm2_read_data, f"VM2 cannot read VM1's data: {vm2_read_data}"
        LOGGER.info("VM2: Successfully read VM1's data")

        # Write test data from VM2
        LOGGER.info("VM2: Writing test data")
        run_ssh_commands(
            ssh_vm2.rrmngmnt_host, ["sh", "-c", f"echo 'Data from VM2' | sudo tee {test_file_vm2} > /dev/null"]
        )
        run_ssh_commands(ssh_vm2.rrmngmnt_host, ["sudo", "sync"])

        # Unmount to prevent concurrent access when VM1 remounts
        run_ssh_commands(ssh_vm2.rrmngmnt_host, ["sudo", "umount", mount_point])

    # VM1: Verify can read VM2's data (bidirectional access)
    LOGGER.info("VM1: Verifying bidirectional access")
    with ssh_vm1:
        # Unmount stale mount from first session
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "umount", mount_point])

        # Flush block device buffers to clear stale kernel cache
        # XFS (non-cluster filesystem) retains metadata in kernel buffer cache.
        # Without this, VM1 won't see VM2's newly written files even after remount.
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "blockdev", "--flushbufs", shared_disk_device])

        # Remount fresh to see VM2's changes
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "mount", partition, mount_point])

        results = run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "cat", test_file_vm2])
        vm1_read_data = results[0].strip()
        assert "Data from VM2" in vm1_read_data, f"VM1 cannot read VM2's data: {vm1_read_data}"
        LOGGER.info("VM1: Successfully read VM2's data")

        # Cleanup: Unmount from VM1
        run_ssh_commands(ssh_vm1.rrmngmnt_host, ["sudo", "umount", mount_point])

    LOGGER.info("Shared disk verification successful - bidirectional access confirmed")


def verify_pvc_count_for_vm(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    plan_name: str,
    expected_pvc_count: int,
) -> None:
    """Verify the number of PVCs created for a migration plan.

    Used for shared disk testing: the consumer VM plan should create fewer PVCs
    because it reuses the shared disk PVC from the owner VM plan.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where PVCs were created.
        plan_name (str): Plan name to filter PVCs by (matched as substring of PVC name).
        expected_pvc_count (int): Expected number of PVCs.

    Raises:
        AssertionError: If actual PVC count doesn't match expected count.
    """
    LOGGER.info(f"Verifying PVC count for plan '{plan_name}' in namespace '{target_namespace}'.")

    all_pvcs = list(PersistentVolumeClaim.get(client=ocp_admin_client, namespace=target_namespace))
    plan_pvcs = [pvc for pvc in all_pvcs if plan_name in pvc.name]

    actual_count = len(plan_pvcs)
    LOGGER.info(f"Found {actual_count} PVCs for plan '{plan_name}'. Expected: {expected_pvc_count}")

    if actual_count != expected_pvc_count:
        pvc_names = [pvc.name for pvc in plan_pvcs]
        LOGGER.error(f"PVC count mismatch. Found PVCs: {pvc_names}")

    assert actual_count == expected_pvc_count, (
        f"Expected {expected_pvc_count} PVCs for plan '{plan_name}', but found {actual_count}"
    )

    LOGGER.info(f"Successfully verified {expected_pvc_count} PVCs for plan '{plan_name}'.")

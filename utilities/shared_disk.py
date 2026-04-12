"""Shared disk migration verification utilities.

Provides functions for verifying shared disk accessibility between VMs
and PVC count validation after migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from simple_logger.logger import get_logger

from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.ssh_utils import SSHConnectionManager

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(name=__name__)


def _run_cmd_on_vm(
    ssh_conn: Any,
    cmd: list[str],
    description: str,
) -> str:
    """Execute a command on a VM via SSH using the explicit executor pattern.

    Uses the same approach as check_static_ip_preservation() in post_migration.py:
    creates an executor with the correct user and port-forward port.

    Args:
        ssh_conn: VMSSHConnection object (must be connected via context manager).
        cmd (list[str]): Command to execute.
        description (str): Human-readable description for logging.

    Returns:
        str: Command stdout.

    Raises:
        RuntimeError: If the command fails (non-zero return code).
    """
    executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
    executor.port = ssh_conn.local_port
    rc, stdout, stderr = executor.run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(f"{description} failed (rc={rc}): {stderr}")
    return stdout


def verify_shared_disk_data(
    vm1_name: str,
    vm2_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    vm1_info: dict[str, Any],
    vm2_info: dict[str, Any],
    shared_disk_device: str = "/dev/vdc",
) -> None:
    """Verify shared disk is accessible from both VMs by writing and reading data.

    The shared disk must already be formatted with a filesystem and unmounted.
    (MTV-2200 limitation: virt-v2v cannot update fstab for shared disks.)

    Flow:
    1. VM1: mount shared disk, write test data, sync, unmount
    2. VM2: mount shared disk, read VM1's data, write own data, sync, unmount
    3. VM1: flush block device cache, remount, read VM2's data, unmount

    Args:
        vm1_name (str): Name of the first VM (owner).
        vm2_name (str): Name of the second VM (consumer).
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        vm1_info (dict[str, Any]): VM1 source data including OS type.
        vm2_info (dict[str, Any]): VM2 source data including OS type.
        shared_disk_device (str): Shared disk device path. Defaults to "/dev/vdc".

    Raises:
        AssertionError: If shared disk data verification fails.
        RuntimeError: If SSH commands fail.
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
    LOGGER.info(f"VM1 ({vm1_name}): Mounting shared disk {partition}")
    with ssh_vm1:
        _run_cmd_on_vm(ssh_vm1, ["sudo", "mkdir", "-p", mount_point], "VM1 mkdir")
        _run_cmd_on_vm(ssh_vm1, ["sudo", "mount", partition, mount_point], "VM1 mount")
        _run_cmd_on_vm(
            ssh_vm1,
            ["sh", "-c", f"echo 'Data from VM1' | sudo tee {test_file_vm1} > /dev/null"],
            "VM1 write test data",
        )
        _run_cmd_on_vm(ssh_vm1, ["sudo", "sync"], "VM1 sync")
        _run_cmd_on_vm(ssh_vm1, ["sudo", "umount", mount_point], "VM1 umount")

    # VM2: Mount shared disk, verify VM1's data, write own data
    LOGGER.info(f"VM2 ({vm2_name}): Mounting shared disk {partition}")
    with ssh_vm2:
        _run_cmd_on_vm(ssh_vm2, ["sudo", "mkdir", "-p", mount_point], "VM2 mkdir")
        _run_cmd_on_vm(ssh_vm2, ["sudo", "mount", partition, mount_point], "VM2 mount")

        # Read VM1's data
        vm2_read_data = _run_cmd_on_vm(ssh_vm2, ["sudo", "cat", test_file_vm1], "VM2 read VM1 data")
        assert "Data from VM1" in vm2_read_data.strip(), f"VM2 cannot read VM1's data: {vm2_read_data}"
        LOGGER.info(f"VM2 ({vm2_name}): Successfully read VM1's data")

        # Write VM2's data
        _run_cmd_on_vm(
            ssh_vm2,
            ["sh", "-c", f"echo 'Data from VM2' | sudo tee {test_file_vm2} > /dev/null"],
            "VM2 write test data",
        )
        _run_cmd_on_vm(ssh_vm2, ["sudo", "sync"], "VM2 sync")
        _run_cmd_on_vm(ssh_vm2, ["sudo", "umount", mount_point], "VM2 umount")

    # VM1: Verify bidirectional access (remount with cache flush)
    LOGGER.info(f"VM1 ({vm1_name}): Verifying bidirectional access")
    with ssh_vm1:
        # Flush block device buffers to clear stale kernel cache.
        # XFS (non-cluster filesystem) retains metadata in kernel buffer cache.
        # Without this, VM1 won't see VM2's newly written files even after remount.
        _run_cmd_on_vm(ssh_vm1, ["sudo", "blockdev", "--flushbufs", shared_disk_device], "VM1 flush buffers")
        _run_cmd_on_vm(ssh_vm1, ["sudo", "mount", partition, mount_point], "VM1 remount")

        vm1_read_data = _run_cmd_on_vm(ssh_vm1, ["sudo", "cat", test_file_vm2], "VM1 read VM2 data")
        assert "Data from VM2" in vm1_read_data.strip(), f"VM1 cannot read VM2's data: {vm1_read_data}"
        LOGGER.info(f"VM1 ({vm1_name}): Successfully read VM2's data")

        _run_cmd_on_vm(ssh_vm1, ["sudo", "umount", mount_point], "VM1 final umount")

    LOGGER.info("Shared disk verification successful - bidirectional access confirmed")

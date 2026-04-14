"""Shared disk migration verification utilities.

Provides functions for verifying shared disk accessibility between VMs
after migration.
"""

from __future__ import annotations

import shlex
from typing import Any

from simple_logger.logger import get_logger

from exceptions.exceptions import GuestCommandError
from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.ssh_utils import SSHConnectionManager, VMSSHConnection

LOGGER = get_logger(name=__name__)


def _run_cmd_on_vm(
    ssh_conn: VMSSHConnection,
    cmd: list[str],
    description: str,
) -> str:
    """Execute a command on a VM via SSH using the explicit executor pattern.

    Uses the same approach as check_static_ip_preservation() in post_migration.py:
    creates an executor with the correct user and port-forward port.

    Args:
        ssh_conn (VMSSHConnection): SSH connection object (must be connected via context manager).
        cmd (list[str]): Command to execute.
        description (str): Human-readable description for logging.

    Returns:
        str: Command stdout.

    Raises:
        GuestCommandError: If the command fails (non-zero return code).
    """
    executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)  # type: ignore[union-attr]
    executor.port = ssh_conn.local_port
    rc, stdout, stderr = executor.run_cmd(cmd)
    if rc != 0:
        raise GuestCommandError(f"{description} failed (rc={rc}): {stderr}")
    return stdout


def _mount_shared_partition(ssh_conn: VMSSHConnection, partition: str, mount_point: str, vm_label: str) -> None:
    """Mount a shared disk partition on a VM.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        partition (str): Device partition path (e.g., "/dev/vdc1").
        mount_point (str): Mount target directory.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If mkdir or mount command fails.
    """
    _run_cmd_on_vm(ssh_conn, ["sudo", "mkdir", "-p", mount_point], f"{vm_label} mkdir")
    _run_cmd_on_vm(ssh_conn, ["sudo", "mount", partition, mount_point], f"{vm_label} mount")


def _umount_shared_partition(ssh_conn: VMSSHConnection, mount_point: str, vm_label: str) -> None:
    """Unmount a shared disk partition on a VM.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        mount_point (str): Mount point to unmount.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If umount command fails.
    """
    _run_cmd_on_vm(ssh_conn, ["sudo", "umount", mount_point], f"{vm_label} umount")


def _write_marker(ssh_conn: VMSSHConnection, file_path: str, content: str, vm_label: str) -> None:
    """Write a marker file and sync to disk.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        file_path (str): Absolute path for the marker file.
        content (str): Text content to write.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If write or sync command fails.
    """
    _run_cmd_on_vm(
        ssh_conn,
        ["sh", "-c", f"echo {shlex.quote(content)} | sudo tee {shlex.quote(file_path)} > /dev/null"],
        f"{vm_label} write test data",
    )
    _run_cmd_on_vm(ssh_conn, ["sudo", "sync"], f"{vm_label} sync")


def verify_shared_disk_data(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
) -> None:
    """Verify shared disk is accessible from both VMs by writing and reading data.

    The shared disk must already be formatted with a filesystem and unmounted.
    (MTV-2200 limitation: virt-v2v cannot update fstab for shared disks.)

    Flow:
    1. VM1: mount shared disk, write test data, sync, unmount
    2. VM2: mount shared disk, read VM1's data, write own data, sync, unmount
    3. VM1: flush block device cache, remount, read VM2's data, unmount

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data,
            and shared_disk_device.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.

    Raises:
        AssertionError: If shared disk data verification fails.
        GuestCommandError: If SSH commands fail.
    """
    vm1_name = prepared_plan["virtual_machines"][0]["name"]
    vm2_name = prepared_plan["virtual_machines"][1]["name"]
    shared_disk_device: str = prepared_plan["shared_disk_device"]

    LOGGER.info(f"Verifying shared disk between {vm1_name} and {vm2_name}")

    vm1_info = prepared_plan["source_vms_data"][vm1_name]
    vm2_info = prepared_plan["source_vms_data"][vm2_name]

    vm1_user, vm1_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm1_info)
    vm2_user, vm2_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm2_info)

    ssh_vm1 = vm_ssh_connections.create(vm_name=vm1_name, username=vm1_user, password=vm1_pass)
    ssh_vm2 = vm_ssh_connections.create(vm_name=vm2_name, username=vm2_user, password=vm2_pass)

    mount_point = "/mnt/shared_disk"
    partition = f"{shared_disk_device}1"
    test_file_vm1 = f"{mount_point}/test-vm1.txt"
    test_file_vm2 = f"{mount_point}/test-vm2.txt"

    # VM1: Mount shared disk, write test data, unmount (keep connection open for verify phase)
    LOGGER.info(f"VM1 ({vm1_name}): Mounting shared disk {partition}")
    with ssh_vm1:
        _mount_shared_partition(ssh_vm1, partition, mount_point, "VM1")
        _write_marker(ssh_vm1, test_file_vm1, "Data from VM1", "VM1")
        _umount_shared_partition(ssh_vm1, mount_point, "VM1")

        # VM2: Mount shared disk, verify VM1's data, write own data, unmount
        LOGGER.info(f"VM2 ({vm2_name}): Mounting shared disk {partition}")
        with ssh_vm2:
            _mount_shared_partition(ssh_vm2, partition, mount_point, "VM2")

            vm2_read_data = _run_cmd_on_vm(ssh_vm2, ["sudo", "cat", test_file_vm1], "VM2 read VM1 data")
            assert "Data from VM1" in vm2_read_data.strip(), f"VM2 cannot read VM1's data: {vm2_read_data}"
            LOGGER.info(f"VM2 ({vm2_name}): Successfully read VM1's data")

            _write_marker(ssh_vm2, test_file_vm2, "Data from VM2", "VM2")
            _umount_shared_partition(ssh_vm2, mount_point, "VM2")

        # Verify bidirectional access (remount with cache flush)
        LOGGER.info(f"VM1 ({vm1_name}): Verifying bidirectional access")
        # Flush block device buffers to clear stale kernel cache.
        # XFS (non-cluster filesystem) retains metadata in kernel buffer cache.
        # Without this, VM1 won't see VM2's newly written files even after remount.
        _run_cmd_on_vm(ssh_vm1, ["sudo", "blockdev", "--flushbufs", shared_disk_device], "VM1 flush buffers")
        _run_cmd_on_vm(ssh_vm1, ["sudo", "mount", partition, mount_point], "VM1 remount")

        vm1_read_data = _run_cmd_on_vm(ssh_vm1, ["sudo", "cat", test_file_vm2], "VM1 read VM2 data")
        assert "Data from VM2" in vm1_read_data.strip(), f"VM1 cannot read VM2's data: {vm1_read_data}"
        LOGGER.info(f"VM1 ({vm1_name}): Successfully read VM2's data")

        _umount_shared_partition(ssh_vm1, mount_point, "VM1 final")

    LOGGER.info("Shared disk verification successful - bidirectional access confirmed")

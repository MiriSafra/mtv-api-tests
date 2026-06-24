"""Shared disk migration verification utilities.

Provides functions for verifying shared disk accessibility between VMs
after migration.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ocp_resources.virtual_machine import VirtualMachine
from paramiko.ssh_exception import AuthenticationException, ChannelException, NoValidConnectionsError, SSHException
from simple_logger.logger import get_logger

from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import GuestCommandError
from utilities.naming import resolve_destination_vm_name
from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.ssh_utils import SSHConnectionManager, VMSSHConnection, run_cmd_in_vm

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(name=__name__)


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
    run_cmd_in_vm(ssh_conn, ["sudo", "mkdir", "-p", mount_point], f"{vm_label} mkdir")
    run_cmd_in_vm(ssh_conn, ["sudo", "mount", partition, mount_point], f"{vm_label} mount")


def _umount_shared_partition(ssh_conn: VMSSHConnection, mount_point: str, vm_label: str) -> None:
    """Unmount a shared disk partition on a VM.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        mount_point (str): Mount point to unmount.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If umount command fails.
    """
    run_cmd_in_vm(ssh_conn, ["sudo", "umount", mount_point], f"{vm_label} umount")


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
    run_cmd_in_vm(
        ssh_conn,
        ["sh", "-c", f"echo {shlex.quote(content)} | sudo tee {shlex.quote(file_path)} > /dev/null"],
        f"{vm_label} write test data",
    )
    run_cmd_in_vm(ssh_conn, ["sudo", "sync"], f"{vm_label} sync")


_VMI_VOLUME_STATUS_TIMEOUT = 300
_VMI_VOLUME_STATUS_POLL_INTERVAL = 5


def _get_pvc_device_targets(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    vm_name: str,
) -> dict[str, str]:
    """Map PVC claim names to their runtime device targets from VMI status.

    Uses ``volumeStatus[].target`` which contains the actual device name
    assigned by KubeVirt at runtime (e.g. ``vda``), eliminating any
    dependency on volume ordering or index arithmetic.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where the migrated VM lives.
        vm_name (str): Destination VM name (already sanitized for Kubernetes).

    Returns:
        dict[str, str]: Mapping of PVC claim name to device path,
            e.g. ``{"pvc-boot": "/dev/vda", "pvc-shared": "/dev/vdc"}``.

    Raises:
        ValueError: If PVC device targets are not populated within the
            timeout (e.g. VMI not running or volumeStatus not yet available).
    """
    cnv_vm = VirtualMachine(
        client=ocp_admin_client,
        name=vm_name,
        namespace=target_namespace,
        ensure_exists=True,
    )
    pvc_devices: dict[str, str] = {}
    sample = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=_VMI_VOLUME_STATUS_TIMEOUT,
            sleep=_VMI_VOLUME_STATUS_POLL_INTERVAL,
            func=lambda: cnv_vm.vmi.instance if cnv_vm.vmi else None,
        ):
            if not sample:
                continue
            volume_status = getattr(sample.status, "volumeStatus", None)
            if not volume_status:
                continue
            pvc_devices.clear()
            for vol_status in volume_status:
                pvc_info = getattr(vol_status, "persistentVolumeClaimInfo", None)
                if not pvc_info:
                    continue
                if not vol_status.target:
                    pvc_devices.clear()
                    break
                pvc_devices[pvc_info.claimName] = f"/dev/{vol_status.target}"
            if pvc_devices:
                break
    except TimeoutExpiredError as exc:
        phase = getattr(sample.status, "phase", "unknown") if sample else "no-sample"
        raise ValueError(
            f"VM '{vm_name}' in '{target_namespace}' VMI has no PVC device targets "
            f"after {_VMI_VOLUME_STATUS_TIMEOUT}s (phase: {phase})"
        ) from exc
    LOGGER.debug(f"PVC device targets for VM '{vm_name}': {pvc_devices}")
    return pvc_devices


def _get_shared_disk_devices(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    vm1_dest_name: str,
    vm2_dest_name: str,
) -> dict[str, str]:
    """Determine per-VM shared disk device paths from destination PVC references.

    Finds the PVC referenced by both destination VMs (the shared disk) and
    returns each VM's device path using the runtime device target from
    ``VMI status.volumeStatus``. Handles VMs with different disk layouts
    correctly.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where migrated VMs live.
        vm1_dest_name (str): First destination VM name.
        vm2_dest_name (str): Second destination VM name.

    Returns:
        dict[str, str]: Mapping of destination VM name to device path,
            e.g. ``{"vm1-name": "/dev/vdc", "vm2-name": "/dev/vdb"}``.

    Raises:
        ValueError: If no shared PVC found between the two VMs.
    """
    vm1_pvc_devices = _get_pvc_device_targets(ocp_admin_client, target_namespace, vm1_dest_name)
    vm2_pvc_devices = _get_pvc_device_targets(ocp_admin_client, target_namespace, vm2_dest_name)

    shared_pvcs = set(vm1_pvc_devices) & set(vm2_pvc_devices)
    if not shared_pvcs:
        raise ValueError(
            f"No shared PVC between '{vm1_dest_name}' and '{vm2_dest_name}'. "
            f"VM1 PVCs: {list(vm1_pvc_devices)}, VM2 PVCs: {list(vm2_pvc_devices)}"
        )
    if len(shared_pvcs) > 1:
        raise ValueError(
            f"Multiple shared PVCs between '{vm1_dest_name}' and '{vm2_dest_name}': {shared_pvcs}. "
            "Only single shared disk is supported."
        )

    shared_pvc = shared_pvcs.pop()
    vm1_device = vm1_pvc_devices[shared_pvc]
    vm2_device = vm2_pvc_devices[shared_pvc]
    LOGGER.info(f"Shared PVC '{shared_pvc}': '{vm1_dest_name}' -> {vm1_device}, '{vm2_dest_name}' -> {vm2_device}")
    return {vm1_dest_name: vm1_device, vm2_dest_name: vm2_device}


@dataclass
class _SharedDiskContext:
    """Common context extracted by _prepare_shared_disk_verification."""

    vm1_name: str
    vm2_name: str
    vm1_dest_name: str
    vm2_dest_name: str
    ssh_vm1: VMSSHConnection
    ssh_vm2: VMSSHConnection
    shared_devices: dict[str, str]


def _prepare_shared_disk_verification(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: "DynamicClient",
) -> _SharedDiskContext:
    """Extract common setup for shared disk verification (Linux and Windows).

    Both verify functions share the same preamble: extract VM configs,
    resolve destination names, validate shared PVC exists, get SSH
    credentials, and create SSH connections.

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data, and _vm_target_namespace.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Returns:
        _SharedDiskContext: Common context for shared disk verification.
    """
    vm1_config = prepared_plan["virtual_machines"][0]
    vm2_config = prepared_plan["virtual_machines"][1]
    vm1_name = vm1_config["name"]
    vm2_name = vm2_config["name"]
    vm_namespace = prepared_plan["_vm_target_namespace"]
    vm1_dest_name = resolve_destination_vm_name(vm1_config)
    vm2_dest_name = resolve_destination_vm_name(vm2_config)
    shared_devices = _get_shared_disk_devices(ocp_admin_client, vm_namespace, vm1_dest_name, vm2_dest_name)

    vm1_info = prepared_plan["source_vms_data"][vm1_name]
    vm2_info = prepared_plan["source_vms_data"][vm2_name]
    vm1_user, vm1_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm1_info)
    vm2_user, vm2_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm2_info)
    ssh_vm1 = vm_ssh_connections.create(vm_name=vm1_name, username=vm1_user, password=vm1_pass)
    ssh_vm2 = vm_ssh_connections.create(vm_name=vm2_name, username=vm2_user, password=vm2_pass)

    return _SharedDiskContext(
        vm1_name=vm1_name,
        vm2_name=vm2_name,
        vm1_dest_name=vm1_dest_name,
        vm2_dest_name=vm2_dest_name,
        ssh_vm1=ssh_vm1,
        ssh_vm2=ssh_vm2,
        shared_devices=shared_devices,
    )


def verify_shared_disk_data(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: "DynamicClient",
) -> None:
    """Verify shared disk is accessible from both VMs by writing and reading data.

    The shared disk must already be formatted with a filesystem and unmounted.
    (MTV-2200 limitation: virt-v2v cannot update fstab for shared disks.)

    Flow:
    1. VM1: mount shared disk, write test data, sync, unmount
    2. VM2: mount shared disk, read VM1's data, write own data, sync, unmount
    3. VM1: flush block device cache, remount, read VM2's data, unmount

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data, and _vm_target_namespace.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Raises:
        AssertionError: If shared disk data verification fails.
        GuestCommandError: If SSH commands fail.
    """
    ctx = _prepare_shared_disk_verification(prepared_plan, vm_ssh_connections, source_provider_data, ocp_admin_client)
    vm1_device = ctx.shared_devices[ctx.vm1_dest_name]
    vm2_device = ctx.shared_devices[ctx.vm2_dest_name]

    LOGGER.info(f"Verifying shared disk between {ctx.vm1_name} and {ctx.vm2_name}")

    mount_point = "/mnt/shared_disk"
    vm1_partition = f"{vm1_device}1"
    vm2_partition = f"{vm2_device}1"
    test_file_vm1 = f"{mount_point}/test-vm1.txt"
    test_file_vm2 = f"{mount_point}/test-vm2.txt"

    LOGGER.info(f"VM1 ({ctx.vm1_name}): Mounting shared disk {vm1_partition}")
    with ctx.ssh_vm1:
        _mount_shared_partition(ctx.ssh_vm1, vm1_partition, mount_point, "VM1")
        _write_marker(ctx.ssh_vm1, test_file_vm1, "Data from VM1", "VM1")
        _umount_shared_partition(ctx.ssh_vm1, mount_point, "VM1")

        LOGGER.info(f"VM2 ({ctx.vm2_name}): Mounting shared disk {vm2_partition}")
        with ctx.ssh_vm2:
            _mount_shared_partition(ctx.ssh_vm2, vm2_partition, mount_point, "VM2")

            vm2_read_data = run_cmd_in_vm(ctx.ssh_vm2, ["sudo", "cat", test_file_vm1], "VM2 read VM1 data")
            assert "Data from VM1" in vm2_read_data.strip(), f"VM2 cannot read VM1's data: {vm2_read_data}"
            LOGGER.info(f"VM2 ({ctx.vm2_name}): Successfully read VM1's data")

            _write_marker(ctx.ssh_vm2, test_file_vm2, "Data from VM2", "VM2")
            _umount_shared_partition(ctx.ssh_vm2, mount_point, "VM2")

        LOGGER.info(f"VM1 ({ctx.vm1_name}): Verifying bidirectional access")
        # Flush block device buffers to clear stale kernel cache.
        # XFS (non-cluster filesystem) retains metadata in kernel buffer cache.
        # Without this, VM1 won't see VM2's newly written files even after remount.
        run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "blockdev", "--flushbufs", vm1_device], "VM1 flush buffers")
        run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "mount", vm1_partition, mount_point], "VM1 remount")

        vm1_read_data = run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "cat", test_file_vm2], "VM1 read VM2 data")
        assert "Data from VM2" in vm1_read_data.strip(), f"VM1 cannot read VM2's data: {vm1_read_data}"
        LOGGER.info(f"VM1 ({ctx.vm1_name}): Successfully read VM2's data")

        _umount_shared_partition(ctx.ssh_vm1, mount_point, "VM1 final")

    LOGGER.info("Shared disk verification successful - bidirectional access confirmed")


_SHARED_VOLUME_LABEL = "SHARED"


def _win_run_powershell(
    ssh_conn: VMSSHConnection,
    script: str,
    description: str,
) -> str:
    """Execute a PowerShell command on a Windows VM via SSH.

    SSH on Windows lands in CMD by default. This wraps the script
    in ``powershell -Command "..."`` so it is interpreted by PowerShell.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        script (str): PowerShell script to execute (single command or semicolon-separated).
        description (str): Human-readable description for logging.

    Returns:
        str: Command stdout.

    Raises:
        GuestCommandError: If the command fails (non-zero return code).
    """
    return _run_cmd_on_vm(ssh_conn, ["powershell", "-Command", script], description)


def _win_ensure_shared_volume_online(ssh_conn: VMSSHConnection, vm_label: str) -> str:
    """Bring offline disks online and return the SHARED volume drive letter.

    After migration, Windows SAN policy may leave non-boot disks offline.
    This brings all offline disks online, clears read-only flags, then
    locates the SHARED volume by its filesystem label.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages (e.g., "VM1").

    Returns:
        str: Single-character drive letter (e.g., "E").

    Raises:
        GuestCommandError: If SHARED volume not found after bringing disks online.
    """
    # Best-effort: piped Set-Disk fails on some vSphere versions due to a Windows SSH
    # console buffer bug. The targeted Set-Disk -Number <N> in _win_refresh_shared_disk
    # is the reliable path; this is just an initial attempt to bring disks online.
    try:
        _win_run_powershell(
            ssh_conn,
            "Get-Disk | Where-Object {$_.OperationalStatus -eq 'Offline'} | Set-Disk -IsOffline $false | Out-Null",
            f"{vm_label} bring offline disks online",
        )
    except GuestCommandError as e:
        LOGGER.warning(f"{vm_label}: Set-Disk -IsOffline failed (best-effort): {e}")
    try:
        _win_run_powershell(
            ssh_conn,
            "Get-Disk | Where-Object {$_.IsReadOnly -eq $true -and $_.Number -ne 0} "
            "| Set-Disk -IsReadOnly $false | Out-Null",
            f"{vm_label} clear read-only flags",
        )
    except GuestCommandError as e:
        LOGGER.warning(f"{vm_label}: Set-Disk -IsReadOnly failed (best-effort): {e}")
    return _win_get_shared_drive_letter(ssh_conn, vm_label)


_WIN_VOLUME_DISCOVERY_TIMEOUT = 60
_WIN_VOLUME_DISCOVERY_POLL_INTERVAL = 5
_WIN_VERIFICATION_RETRY_TIMEOUT = 300
_WIN_VERIFICATION_RETRY_INTERVAL = 15


def _win_get_shared_drive_letter(ssh_conn: VMSSHConnection, vm_label: str) -> str:
    """Find the drive letter of the SHARED volume by its filesystem label.

    Polls with a timeout because Windows may take a moment to mount the
    filesystem after the disk is brought online.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages.

    Returns:
        str: Single-character drive letter (e.g., "E").

    Raises:
        GuestCommandError: If the SHARED volume is not found within the timeout.
    """

    def _try_get_drive_letter() -> str | None:
        try:
            return _win_run_powershell(
                ssh_conn,
                f"(Get-Volume -FileSystemLabel '{_SHARED_VOLUME_LABEL}').DriveLetter",
                f"{vm_label} get SHARED drive letter",
            ).strip()
        except GuestCommandError:
            return None

    drive_letter: str | None = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=_WIN_VOLUME_DISCOVERY_TIMEOUT,
            sleep=_WIN_VOLUME_DISCOVERY_POLL_INTERVAL,
            func=_try_get_drive_letter,
        ):
            if sample and len(sample) == 1:
                drive_letter = sample
                break
    except TimeoutExpiredError as exc:
        raise GuestCommandError(
            f"{vm_label}: Volume with label '{_SHARED_VOLUME_LABEL}' not found after "
            f"{_WIN_VOLUME_DISCOVERY_TIMEOUT}s. Ensure the shared disk on the source VM "
            f"has an NTFS volume labeled '{_SHARED_VOLUME_LABEL}'."
        ) from exc

    if not drive_letter:
        raise GuestCommandError(f"{vm_label}: SHARED volume not found")

    LOGGER.info(f"{vm_label}: SHARED volume is drive {drive_letter}:")
    return drive_letter


def _win_refresh_shared_disk(ssh_conn: VMSSHConnection, vm_label: str) -> None:
    """Flush NTFS metadata cache via disk offline/online cycle.

    NTFS does not see files written by another VM until the disk is taken
    offline and brought back online. This is the Windows equivalent of
    ``blockdev --flushbufs`` used in the Linux verification.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages.

    Raises:
        GuestCommandError: If disk offline/online commands fail.
    """
    disk_num = _win_run_powershell(
        ssh_conn,
        f"(Get-Volume -FileSystemLabel '{_SHARED_VOLUME_LABEL}' | Get-Partition).DiskNumber",
        f"{vm_label} get SHARED disk number",
    ).strip()
    if not disk_num.isdigit():
        raise GuestCommandError(f"{vm_label}: Cannot determine disk number for SHARED volume (got: '{disk_num}')")
    _win_run_powershell(
        ssh_conn, f"Set-Disk -Number {disk_num} -IsOffline $true | Out-Null", f"{vm_label} disk offline"
    )
    _win_run_powershell(
        ssh_conn, f"Set-Disk -Number {disk_num} -IsOffline $false | Out-Null", f"{vm_label} disk online"
    )
    LOGGER.info(f"{vm_label}: Refreshed SHARED disk (disk {disk_num})")


def _win_write_marker(ssh_conn: VMSSHConnection, drive_letter: str, filename: str, content: str, vm_label: str) -> None:
    """Write a marker file on the SHARED volume.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        drive_letter (str): Drive letter (e.g., "E").
        filename (str): File name to write (e.g., "test-vm1.txt").
        content (str): Text content to write.
        vm_label (str): Label for log messages.

    Raises:
        GuestCommandError: If the write command fails.
    """
    ps_path = f"{drive_letter}:\\{filename}".replace("'", "''")
    ps_content = content.replace("'", "''")
    _win_run_powershell(
        ssh_conn,
        f"Set-Content -Path '{ps_path}' -Value '{ps_content}'",
        f"{vm_label} write {filename}",
    )


def _win_read_marker(ssh_conn: VMSSHConnection, drive_letter: str, filename: str, expected: str, vm_label: str) -> None:
    """Read a marker file from the SHARED volume and verify its content.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        drive_letter (str): Drive letter (e.g., "E").
        filename (str): File name to read (e.g., "test-vm1.txt").
        expected (str): Expected content substring.
        vm_label (str): Label for log messages.

    Raises:
        AssertionError: If the file content does not contain the expected string.
        GuestCommandError: If the read command fails.
    """
    content = _win_run_powershell(
        ssh_conn,
        f"Get-Content -Path '{drive_letter}:\\{filename}'",
        f"{vm_label} read {filename}",
    )
    assert expected in content.strip(), f"{vm_label} cannot read expected data from {filename}: {content}"
    LOGGER.info(f"{vm_label}: Successfully read {filename}")


def verify_shared_disk_data_windows(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: "DynamicClient",
) -> None:
    """Verify shared disk is accessible from both Windows VMs after migration.

    Uses NTFS volume label to locate the shared disk (no Linux device paths).
    The shared disk must be formatted with NTFS and labeled ``SHARED``.

    Flow:
    1. Confirm shared PVC exists via KubeVirt volumeStatus
    2. VM1: bring shared disk online, write test data
    3. VM2: bring shared disk online, refresh (clear stale NTFS cache), read VM1's data, write, refresh (flush to disk)
    4. VM1: refresh disk (invalidate cache), read VM2's data

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data, and _vm_target_namespace.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Raises:
        AssertionError: If shared disk data verification fails.
        GuestCommandError: If SSH or PowerShell commands fail.
    """
    # Shared PVC validated by helper (device paths unused — Windows uses volume labels)
    ctx = _prepare_shared_disk_verification(prepared_plan, vm_ssh_connections, source_provider_data, ocp_admin_client)

    LOGGER.info(f"Verifying Windows shared disk between {ctx.vm1_name} and {ctx.vm2_name}")

    test_file_vm1 = "test-vm1.txt"
    test_file_vm2 = "test-vm2.txt"

    def _do_verification() -> bool | None:
        try:
            with ctx.ssh_vm1:
                drive1 = _win_ensure_shared_volume_online(ctx.ssh_vm1, "VM1")
                _win_write_marker(ctx.ssh_vm1, drive1, test_file_vm1, "Data from VM1", "VM1")

                with ctx.ssh_vm2:
                    drive2 = _win_ensure_shared_volume_online(ctx.ssh_vm2, "VM2")
                    _win_refresh_shared_disk(ctx.ssh_vm2, "VM2")
                    drive2 = _win_get_shared_drive_letter(ctx.ssh_vm2, "VM2")
                    _win_read_marker(ctx.ssh_vm2, drive2, test_file_vm1, "Data from VM1", "VM2")

                    _win_write_marker(ctx.ssh_vm2, drive2, test_file_vm2, "Data from VM2", "VM2")
                    _win_refresh_shared_disk(ctx.ssh_vm2, "VM2")

                _win_refresh_shared_disk(ctx.ssh_vm1, "VM1")
                drive1 = _win_get_shared_drive_letter(ctx.ssh_vm1, "VM1")
                _win_read_marker(ctx.ssh_vm1, drive1, test_file_vm2, "Data from VM2", "VM1")

                return True
        except (
            SSHException,
            AuthenticationException,
            NoValidConnectionsError,
            ChannelException,
            GuestCommandError,
            AssertionError,
        ) as e:
            LOGGER.warning(f"Shared disk verification failed: {type(e).__name__}: {e} - retrying...")
            return None

    try:
        for sample in TimeoutSampler(
            wait_timeout=_WIN_VERIFICATION_RETRY_TIMEOUT,
            sleep=_WIN_VERIFICATION_RETRY_INTERVAL,
            func=_do_verification,
        ):
            if sample:
                break
    except TimeoutExpiredError as e:
        raise TimeoutExpiredError(
            f"Windows shared disk verification failed after {_WIN_VERIFICATION_RETRY_TIMEOUT}s"
        ) from e

    LOGGER.info("Windows shared disk verification successful - bidirectional access confirmed")

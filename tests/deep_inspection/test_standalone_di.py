from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.conversion import Conversion
from ocp_resources.pod import Pod
from ocp_resources.secret import Secret
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import ConversionError
from utilities.deep_inspection import (
    DI_POD_CLEANUP_TIMEOUT,
    DI_SNAPSHOT_CLEANUP_TIMEOUT,
    DI_SNAPSHOT_NAME,
    VDDK_IMAGE_NOT_SET_CONDITION,
    cancel_conversion,
    create_conversion_resource,
    verify_di_results,
    wait_for_conversion_complete,
    wait_for_conversion_phase,
    wait_for_di_snapshot,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.providers.vmware import VMWareProvider


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDeepInspection:
    """Standalone Deep Inspection without a migration plan.

    Creates a DeepInspection Conversion CR directly (no Plan, StorageMap,
    or NetworkMap). The controller creates a snapshot, runs the inspection
    pod, fetches results, and cleans up the snapshot automatically.

    No cleanup_migrated_vms: DI does not create migrated VMs.
    """

    conversion: Conversion

    def test_create_conversion(self, di_conversion_resource: Conversion) -> None:
        """Create a standalone DeepInspection Conversion CR."""
        self.__class__.conversion = di_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("di_vm_name")
    def test_wait_for_completion(self) -> None:
        """Wait for the DeepInspection pipeline to complete."""
        wait_for_conversion_complete(
            conversion=self.conversion,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_verify_di_results(self, di_vm_name: str) -> None:
        """Verify inspection results contain OS info and filesystem data."""
        verify_di_results(conversion=self.conversion, vm_name=di_vm_name)


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-cancel-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDICancel:
    """Cancel a running standalone Deep Inspection and re-run.

    Verifies the cancel mechanism: patch status.phase to Canceled,
    controller deletes the pod and cleans up snapshots. Then creates
    a new DI for the same VM and verifies it succeeds.
    """

    conversion: Conversion
    rerun_conversion: Conversion

    def test_create_and_cancel_conversion(
        self,
        di_conversion_resource: Conversion,
        di_vm_name: str,
        source_provider: "VMWareProvider",
    ) -> None:
        """Create DI Conversion, wait for Running and snapshot, then cancel."""
        self.__class__.conversion = di_conversion_resource
        wait_for_conversion_phase(
            conversion=self.conversion,
            phase=Conversion.Status.RUNNING,
            timeout=py_config["plan_wait_timeout"],
        )
        wait_for_di_snapshot(source_provider=source_provider, vm_name=di_vm_name)
        cancel_conversion(conversion=self.conversion)

    @pytest.mark.usefixtures("di_vm_name")
    def test_verify_cancel_cleanup(self) -> None:
        """Verify canceled conversion reaches Canceled phase and pod is cleaned up."""
        with pytest.raises(ConversionError, match="Canceled"):
            wait_for_conversion_complete(
                conversion=self.conversion,
                timeout=py_config["plan_wait_timeout"],
            )

        try:
            for pods in TimeoutSampler(
                wait_timeout=DI_POD_CLEANUP_TIMEOUT,
                sleep=5,
                func=lambda: list(
                    Pod.get(
                        client=self.conversion.client,
                        namespace=self.conversion.namespace,
                        label_selector=f"conversion={self.conversion.name}",
                    )
                ),
            ):
                if not pods:
                    return
        except TimeoutExpiredError as err:
            remaining = list(
                Pod.get(
                    client=self.conversion.client,
                    namespace=self.conversion.namespace,
                    label_selector=f"conversion={self.conversion.name}",
                )
            )
            raise AssertionError(
                f"Conversion pods still present {DI_POD_CLEANUP_TIMEOUT}s after cancel: {[p.name for p in remaining]}"
            ) from err

    def test_verify_snapshot_cleanup(self, di_vm_name: str, source_provider: "VMWareProvider") -> None:
        """Verify forklift DI snapshot is removed from the source VM after cancel."""
        vm = source_provider.get_vm_by_name(query=di_vm_name)
        assert vm, f"VM '{di_vm_name}' not found in source provider"
        try:
            for snapshots in TimeoutSampler(
                wait_timeout=DI_SNAPSHOT_CLEANUP_TIMEOUT,
                sleep=10,
                func=lambda: [s for s in source_provider.list_snapshots(vm) if s.name == DI_SNAPSHOT_NAME],
            ):
                if not snapshots:
                    return
        except TimeoutExpiredError as err:
            remaining = [s.name for s in source_provider.list_snapshots(vm)]
            raise AssertionError(
                f"DI snapshot '{DI_SNAPSHOT_NAME}' still present on VM '{di_vm_name}' "
                f"{DI_SNAPSHOT_CLEANUP_TIMEOUT}s after cancel. All snapshots: {remaining}"
            ) from err

    def test_rerun_di_after_cancel(
        self,
        di_resolved_vm: dict[str, str],
        di_connection_secret: Secret,
        di_vddk_image: str,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Create a new DI Conversion for the same VM after cancel."""
        self.__class__.rerun_conversion = create_conversion_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            connection_secret=di_connection_secret,
            vm_id=di_resolved_vm["id"],
            vm_name=di_resolved_vm["name"],
            vddk_image=di_vddk_image,
            target_namespace=target_namespace,
        )
        assert self.rerun_conversion

    @pytest.mark.usefixtures("di_vm_name")
    def test_rerun_wait_for_completion(self) -> None:
        """Wait for the re-run DI to complete successfully."""
        wait_for_conversion_complete(
            conversion=self.rerun_conversion,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_rerun_verify_results(self, di_vm_name: str) -> None:
        """Verify the re-run DI produced valid inspection results."""
        verify_di_results(conversion=self.rerun_conversion, vm_name=di_vm_name)


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-validation-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDIValidation:
    """Validation errors for standalone Deep Inspection.

    Creates a Conversion CR with missing vddkImage (required for
    DeepInspection type). The controller sets a Critical condition
    (VDDKImageNotSet) and blocks the pipeline.
    """

    conversion: Conversion

    def test_create_invalid_conversion(self, di_invalid_conversion_resource: Conversion) -> None:
        """Create a DeepInspection Conversion CR without vddkImage."""
        self.__class__.conversion = di_invalid_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("di_vm_name")
    def test_verify_validation_error(self) -> None:
        """Verify controller sets Critical condition for missing vddkImage.

        Polls status until conditions appear (controller may not have
        reconciled immediately after CR creation).
        """
        timeout = py_config["plan_wait_timeout"]
        critical_conditions: list[Any] = []
        phase = ""

        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=3,
                func=lambda: self.conversion.instance.status,
            ):
                if not sample:
                    continue
                conditions = sample.get("conditions", [])
                critical_conditions = [
                    c for c in conditions if c.get("category") == "Critical" and c.get("status") == "True"
                ]
                if critical_conditions:
                    phase = sample.get("phase", "")
                    break
        except TimeoutExpiredError as err:
            raise AssertionError(
                f"Conversion '{self.conversion.name}' expected Critical conditions within {timeout}s"
            ) from err

        condition_types = [c.get("type") for c in critical_conditions]
        assert VDDK_IMAGE_NOT_SET_CONDITION in condition_types, (
            f"Conversion '{self.conversion.name}' expected {VDDK_IMAGE_NOT_SET_CONDITION} condition, "
            f"got: {condition_types}"
        )

        assert phase not in {Conversion.Status.RUNNING, Conversion.Status.SUCCEEDED}, (
            f"Conversion '{self.conversion.name}' should not reach Running or Succeeded with validation errors, "
            f"got phase '{phase}'"
        )

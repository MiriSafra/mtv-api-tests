from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Any

from ocp_resources.migration import Migration
from ocp_resources.node import Node
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.resource import ResourceEditor
from ocp_resources.utils.constants import TIMEOUT_10MINUTES, TIMEOUT_2MINUTES, TIMEOUT_5SEC
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.mtv_migration import wait_for_migration_complate
from utilities.worker_node_selection import (
    get_node_labels,
    get_ready_schedulable_worker_nodes,
    select_node_by_available_memory,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

_IMPORTER_POD_LABEL_SELECTOR = "app=containerized-data-importer,cdi.kubevirt.io=importer"
_CONVERSION_POD_LABEL_SELECTOR = "forklift.app=virt-v2v,plan-name={plan_name},plan-namespace={plan_namespace}"
_DISK_TRANSFER_STEP_NAME = "DiskTransfer"
_POD_SCHEDULED_CONDITION_TYPE = "PodScheduled"
_UNSCHEDULABLE_REASON = "Unschedulable"
_FALSE_CONDITION_STATUS = "False"


def get_importer_pods(client: DynamicClient, namespace: str, migration_uid: str) -> list[Pod]:
    """List CDI importer pods in a migration's target namespace.

    Args:
        client (DynamicClient): OpenShift client.
        namespace (str): Destination namespace containing importer pods.
        migration_uid (str): UID of the Migration owning the importer pods.

    Returns:
        list[Pod]: CDI importer pods found in the namespace.
    """
    return list(
        Pod.get(
            client=client,
            namespace=namespace,
            label_selector=f"{_IMPORTER_POD_LABEL_SELECTOR},migration={migration_uid}",
        )
    )


def _get_pod_scheduling_details(pods: list[Pod]) -> list[dict[str, Any]]:
    """Collect scheduler-relevant Pod fields for failure diagnostics.

    Args:
        pods (list[Pod]): Pods whose scheduling state should be reported.

    Returns:
        list[dict[str, Any]]: Pod selectors, constraints, and PodScheduled conditions.
    """
    pod_details = []
    for pod in pods:
        pod_instance = pod.instance
        pod_spec = pod_instance.get("spec", {})
        pod_status = pod_instance.get("status", {})
        pod_details.append({
            "name": pod.name,
            "phase": pod_status.get("phase"),
            "nodeName": pod_spec.get("nodeName"),
            "nodeSelector": pod_spec.get("nodeSelector"),
            "affinity": pod_spec.get("affinity"),
            "tolerations": pod_spec.get("tolerations"),
            "podScheduledCondition": next(
                (
                    condition
                    for condition in pod_status.get("conditions") or []
                    if condition.get("type") == _POD_SCHEDULED_CONDITION_TYPE
                ),
                None,
            ),
        })
    return pod_details


def wait_for_importer_pods_pending(
    plan: Plan,
    migration_uid: str,
    selector: dict[str, str],
    timeout: int = TIMEOUT_10MINUTES,
) -> list[Pod]:
    """Wait until all DiskTransfer importer pods are stably unschedulable.

    Args:
        plan (Plan): Warm Plan whose DiskTransfer tasks are being migrated.
        migration_uid (str): UID of the Migration identifying its importer pods.
        selector (dict[str, str]): Plan ConvertorNodeSelector expected on each importer pod.
        timeout (int): Maximum wait time in seconds, including preflight inspection.

    Returns:
        list[Pod]: All DiskTransfer importer pods Pending with PodScheduled=Unschedulable.

    Raises:
        AssertionError: If importer pods do not inherit the selector and become unschedulable before timeout.
    """
    namespace = plan.instance.spec.targetNamespace
    previous_pod_names: set[str] = set()
    expected_importer_count = 0

    def _all_importer_pods_unschedulable() -> list[Pod]:
        """Return every DiskTransfer importer pod after two stable unschedulable samples.

        Returns:
            list[Pod]: All importer pods when their count matches DiskTransfer tasks and their
                names are stable across polls; otherwise an empty list.

        Raises:
            AssertionError: If a pod lacks the Plan selector or schedules before recovery.
        """
        nonlocal previous_pod_names, expected_importer_count
        importer_pods = get_importer_pods(
            client=plan.client,
            namespace=namespace,
            migration_uid=migration_uid,
        )
        plan_status = plan.instance.status
        migration_status = plan_status.migration if plan_status else None
        expected_importer_count = sum(
            len(step.tasks or [])
            for vm_status in ((migration_status.vms or []) if migration_status else [])
            for step in vm_status.pipeline or []
            if step.name == _DISK_TRANSFER_STEP_NAME
        )
        current_pod_names = {pod.name for pod in importer_pods}
        pod_instances = [(pod, pod.instance) for pod in importer_pods]
        for pod, pod_instance in pod_instances:
            pod_spec = pod_instance.get("spec", {})
            pod_selector = pod_spec.get("nodeSelector") or {}
            assert all(pod_selector.get(key) == value for key, value in selector.items()), (
                f"Importer pod '{pod.name}' does not contain Plan selector {selector}: {pod_selector}"
            )
            pod_status = pod_instance.get("status", {})
            assert not pod_spec.get("nodeName") and pod_status.get("phase") == Pod.Status.PENDING, (
                f"Importer pod '{pod.name}' scheduled before the test labeled a matching worker; "
                f"observed status: {pod_status}"
            )

        if (
            expected_importer_count > 0
            and len(importer_pods) == expected_importer_count
            and current_pod_names == previous_pod_names
            and all(_is_unschedulable(status=pod_instance.get("status", {})) for _, pod_instance in pod_instances)
        ):
            return importer_pods
        previous_pod_names = current_pod_names
        return []

    def _timeout_message() -> str:
        """Build diagnostics from importer pods observed when polling times out.

        Returns:
            str: Expected task count and importer Pod scheduling details.
        """
        importer_pods = get_importer_pods(
            client=plan.client,
            namespace=namespace,
            migration_uid=migration_uid,
        )
        return (
            f"Expected {expected_importer_count} CDI importer pod(s) for DiskTransfer tasks in Migration "
            f"'{migration_uid}', but they did not all reach Pending with PodScheduled=Unschedulable within "
            f"{timeout}s; observed: {_get_pod_scheduling_details(pods=importer_pods)}"
        )

    try:
        for importer_pods in TimeoutSampler(
            wait_timeout=timeout,
            sleep=TIMEOUT_5SEC,
            func=_all_importer_pods_unschedulable,
        ):
            if importer_pods:
                return importer_pods
    except TimeoutExpiredError as err:
        raise AssertionError(_timeout_message()) from err

    raise AssertionError(_timeout_message())


def run_selector_recovery_migration(
    plan: Plan,
    migration: Migration,
    selector: dict[str, str],
    candidate_worker_node_names: list[str],
    di_callback: Callable[[str], None] | None,
) -> None:
    """Recover a warm migration after its CDI importer pods fail scheduling.

    Args:
        plan (Plan): Warm Plan configured with ConvertorNodeSelector.
        migration (Migration): Migration resource created for the Plan.
        selector (dict[str, str]): Plan's ConvertorNodeSelector.
        candidate_worker_node_names (list[str]): Names of Ready, schedulable workers found during preflight.
        di_callback (Callable[[str], None] | None): Optional callback for deep-inspection capture.

    Raises:
        AssertionError: If importer pods do not pend, the selector does not propagate, or pods use another node.
    """
    importer_namespace = plan.instance.spec.targetNamespace
    migration_uid = migration.instance.metadata.uid
    importer_placements: set[str] = set()
    conversion_placements: set[str] = set()
    pending_importer_pods = wait_for_importer_pods_pending(
        plan=plan,
        migration_uid=migration_uid,
        selector=selector,
    )
    worker_node = _select_compatible_worker_node(
        client=plan.client,
        candidate_worker_node_names=candidate_worker_node_names,
        importer_pods=pending_importer_pods,
        selector=selector,
    )
    initially_pending_names = {pod.name for pod in pending_importer_pods}
    label_applied_at = monotonic()
    with ResourceEditor(patches={worker_node: {"metadata": {"labels": selector}}}):

        def _observe_recovery(status: str) -> None:
            """Record placements and fail promptly if a pod violates the expected selector.

            Args:
                status (str): Current migration status.

            Raises:
                AssertionError: If a scheduled pod violates the Plan selector or selected worker.
            """
            importer_pods = get_importer_pods(
                client=plan.client,
                namespace=importer_namespace,
                migration_uid=migration_uid,
            )
            _record_pod_placements(
                pods=importer_pods,
                observed_pod_names=importer_placements,
                selector=selector,
                expected_node=worker_node.name,
                pod_type="CDI importer",
            )
            if not initially_pending_names <= importer_placements:
                assert monotonic() - label_applied_at < TIMEOUT_2MINUTES, (
                    f"Initially pending CDI importer pod(s) {sorted(initially_pending_names)} did not schedule "
                    f"within {TIMEOUT_2MINUTES}s after labeling worker '{worker_node.name}' with selector "
                    f"{selector}; observed: {_get_pod_scheduling_details(pods=importer_pods)}"
                )

            conversion_pods = Pod.get(
                client=plan.client,
                namespace=plan.instance.spec.targetNamespace,
                label_selector=_CONVERSION_POD_LABEL_SELECTOR.format(
                    plan_name=plan.name,
                    plan_namespace=plan.namespace,
                ),
            )
            _record_pod_placements(
                pods=list(conversion_pods),
                observed_pod_names=conversion_placements,
                selector=selector,
                expected_node=worker_node.name,
                pod_type="virt-v2v conversion",
            )
            if di_callback is not None:
                di_callback(status)

        wait_for_migration_complate(plan=plan, on_status_poll=_observe_recovery)

    _assert_observed_pod_names(
        observed_pod_names=importer_placements,
        pod_type="CDI importer",
        expected_pod_names=initially_pending_names,
    )
    _assert_observed_pod_names(
        observed_pod_names=conversion_placements,
        pod_type="virt-v2v conversion",
    )


def _record_pod_placements(
    pods: list[Pod],
    observed_pod_names: set[str],
    selector: dict[str, str],
    expected_node: str,
    pod_type: str,
) -> None:
    """Validate and record each newly scheduled pod.

    Args:
        pods (list[Pod]): Pods to inspect.
        observed_pod_names (set[str]): Names already validated during migration polling.
        selector (dict[str, str]): Plan ConvertorNodeSelector.
        expected_node (str): Worker node selected for this migration.
        pod_type (str): Human-readable pod type for assertion messages.

    Raises:
        AssertionError: If a scheduled pod violates the Plan selector or selected worker.
    """
    for pod in pods:
        pod_instance = pod.instance
        node_name = pod_instance.get("spec", {}).get("nodeName")
        if not node_name or pod.name in observed_pod_names:
            continue

        assert node_name == expected_node, (
            f"{pod_type.capitalize()} pod '{pod.name}' ran on '{node_name}', expected '{expected_node}'"
        )
        pod_selector = pod_instance.get("spec", {}).get("nodeSelector") or {}
        assert all(pod_selector.get(key) == value for key, value in selector.items()), (
            f"{pod_type.capitalize()} pod '{pod.name}' did not inherit Plan selector {selector}"
        )
        observed_pod_names.add(pod.name)


def _assert_observed_pod_names(
    observed_pod_names: set[str],
    pod_type: str,
    expected_pod_names: set[str] | None = None,
) -> None:
    """Assert that expected Pod placements were observed during migration polling.

    Args:
        observed_pod_names (set[str]): Pod names observed with validated placement.
        pod_type (str): Human-readable pod type for assertion messages.
        expected_pod_names (set[str] | None): Pod names that must be observed, if specified.

    Raises:
        AssertionError: If no placement or any expected placement was not observed.
    """
    assert observed_pod_names, f"No {pod_type} pod placement was observed"
    if expected_pod_names is not None:
        assert expected_pod_names <= observed_pod_names, (
            f"Did not observe every initially pending {pod_type} pod after recovery. "
            f"Missing: {sorted(expected_pod_names - observed_pod_names)}; observed: {sorted(observed_pod_names)}"
        )


def _select_compatible_worker_node(
    client: DynamicClient,
    candidate_worker_node_names: list[str],
    importer_pods: list[Pod],
    selector: dict[str, str],
) -> Node:
    """Choose an eligible worker that satisfies CDI's other node selectors.

    Args:
        client (DynamicClient): OpenShift client used for memory-based worker selection.
        candidate_worker_node_names (list[str]): Worker names eligible during preflight.
        importer_pods (list[Pod]): Pending importer pods whose selectors must be honored.
        selector (dict[str, str]): ConvertorNodeSelector to exclude when checking existing pod selectors.

    Returns:
        Node: Worker node compatible with every importer pod.

    Raises:
        AssertionError: If no candidate worker satisfies CDI's other node selectors.
    """
    required_selectors: list[dict[str, str]] = []
    for pod in importer_pods:
        pod_selector = pod.instance.get("spec", {}).get("nodeSelector") or {}
        required_selectors.append({key: value for key, value in pod_selector.items() if key not in selector})

    worker_name_set = set(candidate_worker_node_names)
    current_candidate_nodes = [
        node for node in get_ready_schedulable_worker_nodes(ocp_client=client) if node.name in worker_name_set
    ]
    compatible_workers = [
        node
        for node in current_candidate_nodes
        if all(
            all(get_node_labels(node=node).get(key) == value for key, value in required_selector.items())
            for required_selector in required_selectors
        )
    ]
    assert compatible_workers, (
        "No Ready, schedulable worker satisfies the CDI importer nodeSelector entries other than "
        f"ConvertorNodeSelector. Required selectors: {required_selectors}; "
        f"candidate workers: {[node.name for node in current_candidate_nodes]}"
    )
    selected_node_name = select_node_by_available_memory(
        ocp_admin_client=client,
        worker_nodes=[node.name for node in compatible_workers],
    )
    return next(node for node in compatible_workers if node.name == selected_node_name)


def _is_unschedulable(status: dict[str, Any]) -> bool:
    """Check whether an importer pod is Pending because the scheduler cannot place it.

    Args:
        status (dict[str, Any]): Current Pod status.

    Returns:
        bool: Whether the pod has PodScheduled=False with reason Unschedulable.
    """
    return status.get("phase") == Pod.Status.PENDING and any(
        condition.get("type") == _POD_SCHEDULED_CONDITION_TYPE
        and condition.get("status") == _FALSE_CONDITION_STATUS
        and condition.get("reason") == _UNSCHEDULABLE_REASON
        for condition in status.get("conditions") or []
    )

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ocp_resources.config_map import ConfigMap
from ocp_resources.node import Node
from ocp_resources.pod import Pod
from ocp_resources.plan import Plan
from ocp_resources.resource import ResourceEditor
from ocp_resources.utils.constants import TIMEOUT_1MINUTE, TIMEOUT_5SEC
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.mtv_migration import wait_for_migration_complate
from utilities.worker_node_selection import select_node_by_available_memory

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

_IMPORTER_POD_LABEL_SELECTOR = "app=containerized-data-importer,cdi.kubevirt.io=importer"
_VDDK_CONFIG_MAP_LABEL_SELECTOR = "plan-name={plan_name},plan-namespace={plan_namespace},use=vddk-conf,resource=vddk-config"
_CONVERSION_POD_LABEL_SELECTOR = "forklift.app=virt-v2v,plan-name={plan_name},plan-namespace={plan_namespace}"


def get_importer_pods(client: DynamicClient, namespace: str) -> list[Pod]:
    """List CDI importer pods in a migration's target namespace.

    Args:
        client (DynamicClient): OpenShift client.
        namespace (str): Destination namespace containing the importer pods.

    Returns:
        list[Pod]: CDI importer pods found in the namespace.
    """
    return list(Pod.get(client=client, namespace=namespace, label_selector=_IMPORTER_POD_LABEL_SELECTOR))


def get_vddk_config_maps(plan: Plan) -> list[ConfigMap]:
    """List VDDK configuration ConfigMaps owned by a Plan.

    Args:
        plan (Plan): Migration Plan whose VDDK ConfigMaps should be listed.

    Returns:
        list[ConfigMap]: Matching ConfigMaps in the Plan's target namespace.
    """
    target_namespace = plan.instance.get("spec", {}).get("targetNamespace")
    label_selector = _VDDK_CONFIG_MAP_LABEL_SELECTOR.format(plan_name=plan.name, plan_namespace=plan.namespace)
    return list(
        ConfigMap.get(
            client=plan.client,
            namespace=target_namespace,
            label_selector=label_selector,
        )
    )


def get_conversion_pods(plan: Plan) -> list[Pod]:
    """List virt-v2v conversion pods owned by a Plan.

    Args:
        plan (Plan): Migration Plan whose conversion pods should be listed.

    Returns:
        list[Pod]: Matching conversion pods in the Plan's target namespace.
    """
    target_namespace = plan.instance.get("spec", {}).get("targetNamespace")
    label_selector = _CONVERSION_POD_LABEL_SELECTOR.format(plan_name=plan.name, plan_namespace=plan.namespace)
    return list(Pod.get(client=plan.client, namespace=target_namespace, label_selector=label_selector))


def wait_for_importer_pods_pending(
    client: DynamicClient,
    namespace: str,
    timeout: int = TIMEOUT_1MINUTE,
) -> list[Pod]:
    """Wait for CDI importer pods to become unschedulable while their selector has no match.

    Args:
        client (DynamicClient): OpenShift client.
        namespace (str): Destination namespace containing the importer pods.
        timeout (int): Maximum wait time in seconds.

    Returns:
        list[Pod]: Importer pods pending with an Unschedulable PodScheduled condition.

    Raises:
        AssertionError: If no importer pod reaches the expected pending state before timeout.
    """
    pending_pods: list[Pod] | None = None

    def _all_importer_pods_unschedulable() -> list[Pod]:
        """Return current importer pods only when all are Pending and unschedulable.

        Returns:
            list[Pod]: All current importer pods, or an empty list until all are unschedulable.
        """
        importer_pods = get_importer_pods(client=client, namespace=namespace)
        if importer_pods and all(_is_unschedulable(pod) for pod in importer_pods):
            return importer_pods
        return []

    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=TIMEOUT_5SEC,
            func=_all_importer_pods_unschedulable,
        ):
            if sample:
                pending_pods = sample
                break
    except TimeoutExpiredError as err:
        current_pods = get_importer_pods(client=client, namespace=namespace)
        observed = [
            {
                "name": pod.name,
                "phase": pod.instance.get("status", {}).get("phase"),
                "nodeName": pod.instance.get("status", {}).get("nodeName"),
            }
            for pod in current_pods
        ]
        raise AssertionError(
            f"Not all current CDI importer pods reached Pending with PodScheduled=Unschedulable "
            f"in namespace '{namespace}'. Observed importer pods: {observed}"
        ) from err

    if pending_pods is None:
        raise AssertionError(f"Importer pod polling ended without a result in namespace '{namespace}'")
    return pending_pods


def wait_for_vddk_config_map(plan: Plan, timeout: int = TIMEOUT_1MINUTE) -> ConfigMap:
    """Wait for the VDDK ConfigMap associated with a Plan.

    Args:
        plan (Plan): Migration Plan whose VDDK ConfigMap should be found.
        timeout (int): Maximum wait time in seconds.

    Returns:
        ConfigMap: The Plan's VDDK ConfigMap.

    Raises:
        AssertionError: If the ConfigMap is not created before timeout.
    """
    config_map: ConfigMap | None = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=TIMEOUT_5SEC,
            func=lambda: get_vddk_config_maps(plan=plan),
        ):
            if sample:
                config_map = sample[0]
                break
    except TimeoutExpiredError as err:
        raise AssertionError(f"VDDK ConfigMap for Plan '{plan.name}' was not created") from err

    if config_map is None:
        raise AssertionError(f"VDDK ConfigMap polling for Plan '{plan.name}' ended without a result")
    return config_map


def wait_for_vddk_config_maps_cleanup(plan: Plan, timeout: int = TIMEOUT_1MINUTE) -> None:
    """Wait until Forklift removes all VDDK ConfigMaps owned by a Plan.

    Args:
        plan (Plan): Completed Migration Plan.
        timeout (int): Maximum wait time in seconds.

    Raises:
        AssertionError: If a VDDK ConfigMap remains after timeout.
    """
    config_maps: list[ConfigMap] | None = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=TIMEOUT_5SEC,
            func=lambda: get_vddk_config_maps(plan=plan),
        ):
            if not sample:
                config_maps = sample
                break
    except TimeoutExpiredError as err:
        config_maps = get_vddk_config_maps(plan=plan)
        raise AssertionError(
            f"VDDK ConfigMap(s) {[config_map.name for config_map in config_maps]} remain after Plan '{plan.name}' completed"
        ) from err

    if config_maps is None:
        raise AssertionError(f"VDDK ConfigMap cleanup polling for Plan '{plan.name}' ended without a result")


def create_warm_migration_scheduling_callback(
    plan: Plan,
    client: DynamicClient,
    importer_namespace: str,
    importer_placements: list[dict[str, Any]],
    conversion_placements: list[dict[str, Any]],
    di_callback: Callable[[str], None] | None,
) -> Callable[[str], None]:
    """Create a migration callback that records importer and virt-v2v placement.

    Args:
        plan (Plan): Warm migration Plan being observed.
        client (DynamicClient): OpenShift client used to list CDI importer pods.
        importer_namespace (str): Namespace containing importer pods.
        importer_placements (list[dict[str, Any]]): Destination for importer placement records.
        conversion_placements (list[dict[str, Any]]): Destination for virt-v2v placement records.
        di_callback (Callable[[str], None] | None): Optional deep-inspection status callback.

    Returns:
        Callable[[str], None]: Callback compatible with migration status polling.
    """

    def _observe_scheduling(status: str) -> None:
        """Record pod placement and forward migration status to deep inspection.

        Args:
            status (str): Current migration status from the Plan.
        """
        _record_pod_placements(
            pods=get_importer_pods(client=client, namespace=importer_namespace),
            placements=importer_placements,
        )
        _record_pod_placements(
            pods=get_conversion_pods(plan=plan),
            placements=conversion_placements,
        )
        if di_callback is not None:
            di_callback(status)

    return _observe_scheduling


def run_default_scheduling_migration(
    plan: Plan,
    importer_placements: list[dict[str, Any]],
    on_status_poll: Callable[[str], None],
) -> None:
    """Wait for a default warm migration and confirm CDI importer scheduling and VDDK cleanup.

    Args:
        plan (Plan): Ready warm migration Plan without ConvertorNodeSelector.
        importer_placements (list[dict[str, Any]]): Importer pod placements recorded during migration polling.
        on_status_poll (Callable[[str], None]): Callback that captures pod placement and DI results.
    """

    def _observe_default_scheduling(status: str) -> None:
        _assert_default_vddk_config_map(plan=plan)
        on_status_poll(status)

    wait_for_migration_complate(plan=plan, on_status_poll=_observe_default_scheduling)
    assert importer_placements, "No scheduled CDI importer pod was observed during the default warm migration"
    wait_for_vddk_config_maps_cleanup(plan=plan)


def run_selector_recovery_migration(
    plan: Plan,
    client: DynamicClient,
    importer_namespace: str,
    selector: dict[str, str],
    candidate_worker_nodes: list[Node],
    importer_placements: list[dict[str, Any]],
    conversion_placements: list[dict[str, Any]],
    on_status_poll: Callable[[str], None],
) -> None:
    """Resolve an unsatisfied selector, complete the warm migration, and verify pod placement.

    Args:
        plan (Plan): Ready warm migration Plan with ConvertorNodeSelector.
        client (DynamicClient): OpenShift client used for importer and worker operations.
        importer_namespace (str): Namespace containing the destination VM and importer pods.
        selector (dict[str, str]): Plan selector absent from cluster nodes at migration start.
        candidate_worker_nodes (list[Node]): Ready, schedulable workers for recovery placement.
        importer_placements (list[dict[str, Any]]): Destination for importer pod placement records.
        conversion_placements (list[dict[str, Any]]): Destination for virt-v2v placement records.
        on_status_poll (Callable[[str], None]): Callback that captures pod placement and DI results.
    """
    config_map = wait_for_vddk_config_map(plan=plan)
    config_map_data = config_map.instance.get("data", {})
    assert "vddk-node-selector" in config_map_data, (
        f"VDDK ConfigMap '{config_map.name}' is missing vddk-node-selector"
    )
    assert json.loads(config_map_data["vddk-node-selector"]) == selector

    pending_importer_pods = wait_for_importer_pods_pending(client=client, namespace=importer_namespace)
    worker_node = _select_compatible_worker_node(
        client=client,
        candidate_worker_nodes=candidate_worker_nodes,
        importer_pods=pending_importer_pods,
        selector=selector,
    )
    with ResourceEditor(patches={worker_node: {"metadata": {"labels": selector}}}):
        wait_for_migration_complate(plan=plan, on_status_poll=on_status_poll)

    _assert_selector_on_placements(
        placements=importer_placements,
        selector=selector,
        expected_node=worker_node.name,
        pod_type="CDI importer",
        expected_pod_names={pod.name for pod in pending_importer_pods},
    )
    _assert_selector_on_placements(
        placements=conversion_placements,
        selector=selector,
        expected_node=worker_node.name,
        pod_type="virt-v2v conversion",
    )
    wait_for_vddk_config_maps_cleanup(plan=plan)


def _record_pod_placements(pods: list[Pod], placements: list[dict[str, Any]]) -> None:
    """Add each newly observed scheduled pod to a placement collection.

    Args:
        pods (list[Pod]): Pods whose scheduling state should be recorded.
        placements (list[dict[str, Any]]): Placement records collected during migration polling.
    """
    recorded_pod_names = {placement["pod_name"] for placement in placements}
    for pod in pods:
        pod_instance = pod.instance
        node_name = pod_instance.get("status", {}).get("nodeName")
        if node_name and pod.name not in recorded_pod_names:
            placements.append(
                {
                    "pod_name": pod.name,
                    "node_name": node_name,
                    "node_selector": pod_instance.get("spec", {}).get("nodeSelector", {}),
                }
            )
            recorded_pod_names.add(pod.name)


def _select_compatible_worker_node(
    client: DynamicClient,
    candidate_worker_nodes: list[Node],
    importer_pods: list[Pod],
    selector: dict[str, str],
) -> Node:
    """Choose a worker matching CDI nodeSelector entries other than the Plan selector.

    Args:
        client (DynamicClient): OpenShift client used for worker memory selection.
        candidate_worker_nodes (list[Node]): Ready, schedulable workers from the fixture.
        importer_pods (list[Pod]): Pending importer pods whose merged selectors must be honored.
        selector (dict[str, str]): Plan selector to exclude when matching existing node labels.

    Returns:
        Node: Selected worker node to label for importer scheduling recovery.

    Raises:
        AssertionError: If no candidate worker satisfies CDI's other nodeSelector entries.
    """
    required_selectors: list[dict[str, str]] = []
    for pod in importer_pods:
        pod_selector = pod.instance.get("spec", {}).get("nodeSelector", {})
        assert all(pod_selector.get(key) == value for key, value in selector.items()), (
            f"Importer pod '{pod.name}' does not contain the Plan selector: {pod_selector}"
        )
        required_selectors.append({key: value for key, value in pod_selector.items() if key not in selector})

    compatible_worker_nodes = [
        node
        for node in candidate_worker_nodes
        if all(
            all((node.labels or {}).get(key) == value for key, value in required_selector.items())
            for required_selector in required_selectors
        )
    ]
    assert compatible_worker_nodes, (
        "No Ready, schedulable worker satisfies the CDI importer nodeSelector entries other than "
        f"ConvertorNodeSelector. Required selectors: {required_selectors}; "
        f"candidate workers: {[node.name for node in candidate_worker_nodes]}"
    )
    selected_node_name = select_node_by_available_memory(
        ocp_admin_client=client,
        worker_nodes=[node.name for node in compatible_worker_nodes],
    )
    return next(node for node in compatible_worker_nodes if node.name == selected_node_name)


def _assert_default_vddk_config_map(plan: Plan) -> None:
    """Verify default warm migration ConfigMaps omit the custom selector key.

    Args:
        plan (Plan): Warm migration Plan created without ConvertorNodeSelector.
    """
    for config_map in get_vddk_config_maps(plan=plan):
        assert "vddk-node-selector" not in config_map.instance.get("data", {}), (
            f"Default warm Plan unexpectedly added vddk-node-selector to ConfigMap '{config_map.name}'"
        )


def _assert_selector_on_placements(
    placements: list[dict[str, Any]],
    selector: dict[str, str],
    expected_node: str,
    pod_type: str,
    expected_pod_names: set[str] | None = None,
) -> None:
    """Verify observed pods inherited the Plan selector and ran on the selected worker.

    Args:
        placements (list[dict[str, Any]]): Pod placement records captured during migration.
        selector (dict[str, str]): Plan convertorNodeSelector.
        expected_node (str): Worker node labeled to satisfy the selector.
        pod_type (str): Human-readable pod type for assertion messages.
        expected_pod_names (set[str] | None): Required pod names that must be observed. Defaults to None.

    Raises:
        AssertionError: If no pod placement was captured or a pod violated the selector.
    """
    assert placements, f"No {pod_type} pod placement was observed"
    if expected_pod_names is not None:
        observed_pod_names = {placement["pod_name"] for placement in placements}
        assert expected_pod_names.issubset(observed_pod_names), (
            f"Did not observe every initially pending {pod_type} pod after recovery. "
            f"Missing: {sorted(expected_pod_names - observed_pod_names)}; observed: {sorted(observed_pod_names)}"
        )
    for placement in placements:
        assert placement["node_name"] == expected_node, (
            f"{pod_type.capitalize()} pod '{placement['pod_name']}' ran on '{placement['node_name']}', "
            f"expected '{expected_node}'"
        )
        assert all(placement["node_selector"].get(key) == value for key, value in selector.items()), (
            f"{pod_type.capitalize()} pod '{placement['pod_name']}' did not inherit the Plan selector"
        )


def _is_unschedulable(pod: Pod) -> bool:
    """Check whether a pod is Pending because the scheduler cannot place it.

    Args:
        pod (Pod): CDI importer pod to inspect.

    Returns:
        bool: Whether the pod has a PodScheduled=Unschedulable condition.
    """
    pod_instance = pod.instance
    status = pod_instance.get("status", {})
    return status.get("phase") == Pod.Status.PENDING and any(
        condition.get("type") == "PodScheduled" and condition.get("reason") == "Unschedulable"
        for condition in status.get("conditions", [])
    )

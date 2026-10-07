from __future__ import annotations

import base64
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.cluster_role import ClusterRole
from ocp_resources.cluster_role_binding import ClusterRoleBinding
from ocp_resources.node import Node
from ocp_resources.provider import Provider
from ocp_resources.role_binding import RoleBinding
from ocp_resources.secret import Secret
from ocp_resources.service_account import ServiceAccount
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from libs.providers.openshift import OCPProvider
from utilities.resources import create_and_store_resource
from utilities.worker_node_selection import (
    get_session_unique_label_key,
    get_worker_nodes,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

FORKLIFT_MIGRATOR_ROLE_NAME = "forklift-migrator-role"


@pytest.fixture(scope="class")
def convertor_node_selector(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
) -> dict[str, str] | None:
    """Resolve an optional Plan selector to a unique label for this test session.

    Args:
        prepared_plan (dict[str, Any]): Prepared migration plan configuration.
        fixture_store (dict[str, Any]): Fixture store containing the unique session ID.

    Returns:
        dict[str, str] | None: Unique selector for the pending scenario, or None for the default scenario.

    Raises:
        ValueError: If the configured selector does not contain exactly one label.
    """
    configured_selector = prepared_plan.get("convertor_node_selector")
    if configured_selector is None:
        return None

    if len(configured_selector) != 1:
        raise ValueError(f"convertor_node_selector must contain exactly one label, got {len(configured_selector)}")

    base_label_key, configured_value = next(iter(configured_selector.items()))
    session_uuid: str = fixture_store["session_uuid"]
    label_key = get_session_unique_label_key(label_key=base_label_key, session_uuid=session_uuid)
    label_value = session_uuid if configured_value is None else configured_value
    return {label_key: label_value}


@pytest.fixture(scope="class")
def convertor_worker_nodes(
    convertor_node_selector: dict[str, str] | None,
    ocp_admin_client: "DynamicClient",
) -> list[Node] | None:
    """List ready, schedulable workers for selector recovery.

    Args:
        convertor_node_selector (dict[str, str] | None): Selector configured for this scenario.
        ocp_admin_client (DynamicClient): OpenShift admin client.

    Returns:
        list[Node] | None: Eligible worker nodes, or None for the default scenario.

    Raises:
        ValueError: If no eligible worker exists or the selector already matches a node.
    """
    if convertor_node_selector is None:
        return None

    nodes = list(Node.get(client=ocp_admin_client))
    matching_nodes = [
        node.name
        for node in nodes
        if all((node.labels or {}).get(key) == value for key, value in convertor_node_selector.items())
    ]
    if matching_nodes:
        raise ValueError(
            f"The pending scenario requires no node to match convertor_node_selector; matching nodes: {matching_nodes}"
        )

    worker_node_names = set(get_worker_nodes(ocp_client=ocp_admin_client))
    schedulable_worker_nodes: list[Node] = []
    for node in nodes:
        if node.name not in worker_node_names:
            continue

        status = node.instance.get("status", {})
        is_ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        is_schedulable = not node.instance.get("spec", {}).get("unschedulable", False)
        has_blocking_taint = any(
            taint.get("effect") in {"NoSchedule", "NoExecute"}
            for taint in node.instance.get("spec", {}).get("taints") or []
        )
        if is_ready and is_schedulable and not has_blocking_taint:
            schedulable_worker_nodes.append(node)

    if not schedulable_worker_nodes:
        raise ValueError("No Ready, schedulable, untainted worker node is available for selector recovery")

    return schedulable_worker_nodes


@pytest.fixture(scope="session")
def clusterrole_destination_ocp_provider(
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    session_uuid: str,
    target_namespace: str,
    mtv_namespace: str,
) -> OCPProvider:
    """Create a token-based OCP provider using the existing forklift-migrator-role and a fresh SA.

    Verifies the flow:
      1. Create a fresh ServiceAccount (in MTV operator namespace)
      2. Bind it ONLY to the existing ClusterRole forklift-migrator-role (from operator/PR)
      3. Create a token for that SA (equivalent to: oc create token <sa> -n <mtv-namespace>)
      4. Create Forklift Provider CR using that token (in target namespace)

    Does NOT create the ClusterRole; forklift-migrator-role must already exist in the cluster.

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking and teardown.
        ocp_admin_client (DynamicClient): OpenShift DynamicClient for cluster operations.
        session_uuid (str): Unique session identifier for resource naming.
        target_namespace (str): Namespace for provider resources (Provider CR, provider secret).
        mtv_namespace (str): MTV operator namespace for ServiceAccount and token.

    Returns:
        OCPProvider: Token-based OCP provider bound to forklift-migrator-role.

    Raises:
        ValueError: If the SA token is not populated within 60s.
    """
    sa_name = f"{session_uuid}-forklift-migrator-sa"
    binding_name = f"{session_uuid}-forklift-migrator-binding"
    token_secret_name = f"{session_uuid}-clusterrole-token"
    provider_name = f"{session_uuid}-clusterrole-destination-ocp-provider"

    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=ServiceAccount,
        name=sa_name,
        namespace=mtv_namespace,
    )

    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=ClusterRoleBinding,
        name=binding_name,
        cluster_role=FORKLIFT_MIGRATOR_ROLE_NAME,
        subjects=[{"kind": "ServiceAccount", "name": sa_name, "namespace": mtv_namespace}],
    )

    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        name=token_secret_name,
        namespace=mtv_namespace,
        type="kubernetes.io/service-account-token",
        annotations={"kubernetes.io/service-account.name": sa_name},
    )

    token_secret_ref = Secret(
        client=ocp_admin_client,
        name=token_secret_name,
        namespace=mtv_namespace,
    )

    try:
        for sample in TimeoutSampler(
            wait_timeout=60,
            sleep=2,
            func=lambda: (token_secret_ref.instance.data or {}).get("token"),
        ):
            if sample:
                token_b64 = sample
                break
    except TimeoutExpiredError:
        raise ValueError(
            f"Token was not populated in Secret {token_secret_name} for ServiceAccount {sa_name} within 60s"
        ) from None

    token_value = base64.b64decode(token_b64).decode("utf-8")

    provider_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        name=f"{provider_name}-secret",
        namespace=target_namespace,
        string_data={"token": token_value, "insecureSkipVerify": "true"},
    )

    provider = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Provider,
        name=provider_name,
        namespace=target_namespace,
        secret_name=provider_secret.name,
        secret_namespace=provider_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    )

    return OCPProvider(ocp_resource=provider, fixture_store=fixture_store)


@pytest.fixture(scope="class")
def forklift_scc_binding(
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    session_uuid: str,
    target_namespace: str,
) -> Generator[RoleBinding, None, None]:
    """Create ClusterRole and RoleBinding to grant forklift-controller-scc SCC to the default SA.

    Class-scoped so that resources are created per test class and torn down after
    the class completes. This ensures "without SCC" test classes run against a
    clean state where neither the ClusterRole nor the RoleBinding exist.

    Equivalent to: oc adm policy add-scc-to-user forklift-controller-scc -z default -n <namespace>

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking and teardown.
        ocp_admin_client (DynamicClient): OpenShift DynamicClient for cluster operations.
        session_uuid (str): Unique session identifier for resource naming.
        target_namespace (str): Namespace where migration pods run.

    Yields:
        RoleBinding: The created RoleBinding resource.
    """
    scc_cluster_role_name = "system:openshift:scc:forklift-controller-scc"

    scc_cluster_role = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=ClusterRole,
        name=scc_cluster_role_name,
        rules=[
            {
                "apiGroups": ["security.openshift.io"],
                "resourceNames": ["forklift-controller-scc"],
                "resources": ["securitycontextconstraints"],
                "verbs": ["use"],
            }
        ],
    )

    role_binding = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=RoleBinding,
        name=f"{session_uuid}-forklift-scc-binding",
        namespace=target_namespace,
        role_ref_kind="ClusterRole",
        role_ref_name=scc_cluster_role_name,
        subjects_kind="ServiceAccount",
        subjects_name="default",
        subjects_namespace=target_namespace,
    )

    yield role_binding

    role_binding.clean_up()
    scc_cluster_role.clean_up()

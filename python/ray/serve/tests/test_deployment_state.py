from dataclasses import dataclass
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch, Mock

import pytest

import ray
from ray.serve._private.common import (
    DeploymentConfig,
    DeploymentInfo,
    DeploymentStatus,
    ReplicaConfig,
    ReplicaTag,
    ReplicaName,
    ReplicaState,
)
from ray.serve._private.deployment_state import (
    DeploymentState,
    DriverDeploymentState,
    DeploymentStateManager,
    DeploymentVersion,
    DeploymentReplica,
    ReplicaStartupStatus,
    ReplicaStateContainer,
    VersionedReplica,
    rank_replicas_for_stopping,
)
from ray.serve._private.constants import (
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_S,
    DEFAULT_GRACEFUL_SHUTDOWN_WAIT_LOOP_S,
    DEFAULT_HEALTH_CHECK_PERIOD_S,
    DEFAULT_HEALTH_CHECK_TIMEOUT_S,
)
from ray.serve._private.storage.kv_store import RayInternalKVStore
from ray.serve._private.utils import get_random_letters
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy


class MockActorHandle:
    def __init__(self):
        self._actor_id = "fake_id"


class MockReplicaActorWrapper:
    def __init__(
        self,
        actor_name: str,
        detached: bool,
        controller_name: str,
        replica_tag: ReplicaTag,
        deployment_name: str,
        scheduling_strategy="SPREAD",
    ):
        self._actor_name = actor_name
        self._replica_tag = replica_tag
        self._deployment_name = deployment_name

        # Will be set when `start()` is called.
        self.started = False
        # Will be set when `recover()` is called.
        self.recovering = False
        # Will be set when `start()` is called.
        self.version = None
        # Initial state for a replica is PENDING_ALLOCATION.
        self.ready = ReplicaStartupStatus.PENDING_ALLOCATION
        # Will be set when `graceful_stop()` is called.
        self.stopped = False
        # Expected to be set in the test.
        self.done_stopping = False
        # Will be set when `force_stop()` is called.
        self.force_stopped_counter = 0
        # Will be set when `check_health()` is called.
        self.health_check_called = False
        # Returned by the health check.
        self.healthy = True
        self._is_cross_language = False
        self._scheduling_strategy = scheduling_strategy
        self._actor_handle = MockActorHandle()

    @property
    def is_cross_language(self) -> bool:
        return self._is_cross_language

    @property
    def replica_tag(self) -> str:
        return str(self._replica_tag)

    @property
    def deployment_name(self) -> str:
        return self._deployment_name

    @property
    def actor_handle(self) -> MockActorHandle:
        return self._actor_handle

    @property
    def max_concurrent_queries(self) -> int:
        return 100

    @property
    def node_id(self) -> Optional[str]:
        if isinstance(self._scheduling_strategy, NodeAffinitySchedulingStrategy):
            return self._scheduling_strategy.node_id
        if self.ready == ReplicaStartupStatus.SUCCEEDED or self.started:
            return "node-id"
        return None

    def set_ready(self):
        self.ready = ReplicaStartupStatus.SUCCEEDED

    def set_failed_to_start(self):
        self.ready = ReplicaStartupStatus.FAILED

    def set_done_stopping(self):
        self.done_stopping = True

    def set_unhealthy(self):
        self.healthy = False

    def set_starting_version(self, version: DeploymentVersion):
        """Mocked deployment_worker return version from reconfigure()"""
        self.starting_version = version

    def start(self, deployment_info: DeploymentInfo, version: DeploymentVersion):
        self.started = True
        self.version = version
        self.deployment_info = deployment_info

    def reconfigure(self, version: DeploymentVersion):
        self.started = True
        updating = self.version.requires_actor_reconfigure(version)
        self.version = version
        return updating

    def recover(self):
        self.recovering = True
        self.started = False
        self.version = None

    def check_ready(self) -> ReplicaStartupStatus:
        ready = self.ready
        self.ready = ReplicaStartupStatus.PENDING_INITIALIZATION
        if ready == ReplicaStartupStatus.SUCCEEDED and self.recovering:
            self.recovering = False
            self.started = True
            self.version = self.starting_version
        return ready

    def resource_requirements(self) -> Tuple[str, str]:
        assert self.started
        return str({"REQUIRED_RESOURCE": 1.0}), str({"AVAILABLE_RESOURCE": 1.0})

    @property
    def actor_resources(self) -> Dict[str, float]:
        return {"CPU": 0.1}

    @property
    def available_resources(self) -> Dict[str, float]:
        # Only used to print a warning.
        return {}

    def graceful_stop(self) -> None:
        assert self.started
        self.stopped = True
        return self.deployment_info.deployment_config.graceful_shutdown_timeout_s

    def check_stopped(self) -> bool:
        return self.done_stopping

    def force_stop(self):
        self.force_stopped_counter += 1

    def check_health(self):
        self.health_check_called = True
        return self.healthy


def deployment_info(
    version: Optional[str] = None,
    num_replicas: Optional[int] = 1,
    user_config: Optional[Any] = None,
    is_driver_deployment: bool = False,
    **config_opts,
) -> Tuple[DeploymentInfo, DeploymentVersion]:
    info = DeploymentInfo(
        version=version,
        start_time_ms=0,
        deployment_config=DeploymentConfig(
            num_replicas=num_replicas, user_config=user_config, **config_opts
        ),
        replica_config=ReplicaConfig.create(lambda x: x),
        deployer_job_id="",
        is_driver_deployment=is_driver_deployment,
    )

    if version is not None:
        code_version = version
    else:
        code_version = get_random_letters()

    version = DeploymentVersion(
        code_version, info.deployment_config, info.replica_config.ray_actor_options
    )

    return info, version


def deployment_version(code_version) -> DeploymentVersion:
    return DeploymentVersion(code_version, DeploymentConfig(), {})


class MockTimer:
    def __init__(self, start_time=None):
        if start_time is None:
            start_time = time.time()
        self._curr = start_time

    def time(self):
        return self._curr

    def advance(self, by):
        self._curr += by


@pytest.fixture
def mock_deployment_state(request) -> Tuple[DeploymentState, Mock, Mock]:
    timer = MockTimer()
    with patch(
        "ray.serve._private.deployment_state.ActorReplicaWrapper",
        new=MockReplicaActorWrapper,
    ), patch("time.time", new=timer.time), patch(
        "ray.serve._private.long_poll.LongPollHost"
    ) as mock_long_poll:

        def mock_save_checkpoint_fn(*args, **kwargs):
            pass

        # It is a driver deployment test
        if request.param is True:
            mock_client = "Fakeclient"
            deployment_state = DriverDeploymentState(
                "name",
                "name",
                True,
                mock_long_poll,
                mock_save_checkpoint_fn,
                mock_client,
            )
            yield deployment_state, timer
        else:
            deployment_state = DeploymentState(
                "name", "name", True, mock_long_poll, mock_save_checkpoint_fn
            )

            yield deployment_state, timer


def replica(version: Optional[DeploymentVersion] = None) -> VersionedReplica:
    if version is None:
        version = DeploymentVersion(get_random_letters(), DeploymentConfig(), {})

    class MockVersionedReplica(VersionedReplica):
        def __init__(self, version: DeploymentVersion):
            self._version = version

        @property
        def version(self):
            return self._version

    return MockVersionedReplica(version)


class TestReplicaStateContainer:
    def test_count(self):
        c = ReplicaStateContainer()
        r1, r2, r3 = (
            replica(deployment_version("1")),
            replica(deployment_version("2")),
            replica(deployment_version("2")),
        )
        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert c.count() == 3

        # Test filtering by state.
        assert c.count() == c.count(
            states=[ReplicaState.STARTING, ReplicaState.STOPPING]
        )
        assert c.count(states=[ReplicaState.STARTING]) == 2
        assert c.count(states=[ReplicaState.STOPPING]) == 1

        # Test filtering by version.
        assert c.count(version=deployment_version("1")) == 1
        assert c.count(version=deployment_version("2")) == 2
        assert c.count(version=deployment_version("3")) == 0
        assert c.count(exclude_version=deployment_version("1")) == 2
        assert c.count(exclude_version=deployment_version("2")) == 1
        assert c.count(exclude_version=deployment_version("3")) == 3

        # Test filtering by state and version.
        assert (
            c.count(version=deployment_version("1"), states=[ReplicaState.STARTING])
            == 1
        )
        assert (
            c.count(version=deployment_version("3"), states=[ReplicaState.STARTING])
            == 0
        )
        assert (
            c.count(
                version=deployment_version("2"),
                states=[ReplicaState.STARTING, ReplicaState.STOPPING],
            )
            == 2
        )
        assert (
            c.count(
                exclude_version=deployment_version("1"), states=[ReplicaState.STARTING]
            )
            == 1
        )
        assert (
            c.count(
                exclude_version=deployment_version("3"), states=[ReplicaState.STARTING]
            )
            == 2
        )
        assert (
            c.count(
                exclude_version=deployment_version("2"),
                states=[ReplicaState.STARTING, ReplicaState.STOPPING],
            )
            == 1
        )

    def test_get(self):
        c = ReplicaStateContainer()
        r1, r2, r3 = replica(), replica(), replica()

        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert c.get() == [r1, r2, r3]
        assert c.get() == c.get([ReplicaState.STARTING, ReplicaState.STOPPING])
        assert c.get([ReplicaState.STARTING]) == [r1, r2]
        assert c.get([ReplicaState.STOPPING]) == [r3]

    def test_pop_basic(self):
        c = ReplicaStateContainer()
        r1, r2, r3 = replica(), replica(), replica()

        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert c.pop() == [r1, r2, r3]
        assert not c.pop()

    def test_pop_exclude_version(self):
        c = ReplicaStateContainer()
        r1, r2, r3 = (
            replica(deployment_version("1")),
            replica(deployment_version("1")),
            replica(deployment_version("2")),
        )

        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STARTING, r3)
        assert c.pop(exclude_version=deployment_version("1")) == [r3]
        assert not c.pop(exclude_version=deployment_version("1"))
        assert c.pop(exclude_version=deployment_version("2")) == [r1, r2]
        assert not c.pop(exclude_version=deployment_version("2"))
        assert not c.pop()

    def test_pop_max_replicas(self):
        c = ReplicaStateContainer()
        r1, r2, r3 = replica(), replica(), replica()

        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert not c.pop(max_replicas=0)
        assert len(c.pop(max_replicas=1)) == 1
        assert len(c.pop(max_replicas=2)) == 2
        c.add(ReplicaState.STARTING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert len(c.pop(max_replicas=10)) == 3

    def test_pop_states(self):
        c = ReplicaStateContainer()
        r1, r2, r3, r4 = replica(), replica(), replica(), replica()

        # Check popping single state.
        c.add(ReplicaState.STOPPING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        assert c.pop(states=[ReplicaState.STARTING]) == [r2]
        assert not c.pop(states=[ReplicaState.STARTING])
        assert c.pop(states=[ReplicaState.STOPPING]) == [r1, r3]
        assert not c.pop(states=[ReplicaState.STOPPING])

        # Check popping multiple states. Ordering of states should be
        # preserved.
        c.add(ReplicaState.STOPPING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.STOPPING, r3)
        c.add(ReplicaState.STARTING, r4)
        assert c.pop(states=[ReplicaState.STOPPING, ReplicaState.STARTING]) == [
            r1,
            r3,
            r2,
            r4,
        ]
        assert not c.pop(states=[ReplicaState.STOPPING, ReplicaState.STARTING])
        assert not c.pop(states=[ReplicaState.STOPPING])
        assert not c.pop(states=[ReplicaState.STARTING])
        assert not c.pop()

    def test_pop_integration(self):
        c = ReplicaStateContainer()
        r1, r2, r3, r4 = (
            replica(deployment_version("1")),
            replica(deployment_version("2")),
            replica(deployment_version("2")),
            replica(deployment_version("3")),
        )

        c.add(ReplicaState.STOPPING, r1)
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.RUNNING, r3)
        c.add(ReplicaState.RUNNING, r4)
        assert not c.pop(
            exclude_version=deployment_version("1"), states=[ReplicaState.STOPPING]
        )
        assert c.pop(
            exclude_version=deployment_version("1"),
            states=[ReplicaState.RUNNING],
            max_replicas=1,
        ) == [r3]
        assert c.pop(
            exclude_version=deployment_version("1"),
            states=[ReplicaState.RUNNING],
            max_replicas=1,
        ) == [r4]
        c.add(ReplicaState.RUNNING, r3)
        c.add(ReplicaState.RUNNING, r4)
        assert c.pop(
            exclude_version=deployment_version("1"), states=[ReplicaState.RUNNING]
        ) == [r3, r4]
        assert c.pop(
            exclude_version=deployment_version("1"), states=[ReplicaState.STARTING]
        ) == [r2]
        c.add(ReplicaState.STARTING, r2)
        c.add(ReplicaState.RUNNING, r3)
        c.add(ReplicaState.RUNNING, r4)
        assert c.pop(
            exclude_version=deployment_version("1"),
            states=[ReplicaState.RUNNING, ReplicaState.STARTING],
        ) == [r3, r4, r2]
        assert c.pop(
            exclude_version=deployment_version("nonsense"),
            states=[ReplicaState.STOPPING],
        ) == [r1]


def check_counts(
    deployment_state: DeploymentState,
    total: Optional[int] = None,
    version: Optional[str] = None,
    by_state: Optional[List[Tuple[ReplicaState, int]]] = None,
):
    if total is not None:
        assert deployment_state._replicas.count(version=version) == total

    if by_state is not None:
        for state, count in by_state:
            assert isinstance(state, ReplicaState)
            assert isinstance(count, int) and count >= 0
            curr_count = deployment_state._replicas.count(
                version=version, states=[state]
            )
            msg = f"Expected {count} for state {state} but got {curr_count}."
            assert curr_count == count, msg


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_create_delete_single_replica(mock_get_all_node_ids, mock_deployment_state):
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info()
    updating = deployment_state.deploy(b_info_1)
    assert updating

    # Single replica should be created.
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])

    # update() should not transition the state if the replica isn't ready.
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])
    deployment_state._replicas.get()[0]._actor.set_ready()
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Now the replica should be marked running.
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Removing the replica should transition it to stopping.
    deployment_state.delete()
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])
    assert deployment_state._replicas.get()[0]._actor.stopped
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Once it's done stopping, replica should be removed.
    replica = deployment_state._replicas.get()[0]
    replica._actor.set_done_stopping()
    deleted, _ = deployment_state.update()
    assert deleted
    check_counts(deployment_state, total=0)


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_force_kill(mock_get_all_node_ids, mock_deployment_state):
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    grace_period_s = 10
    b_info_1, b_version_1 = deployment_info(graceful_shutdown_timeout_s=grace_period_s)

    # Create and delete the deployment.
    deployment_state.deploy(b_info_1)
    deployment_state.update()
    deployment_state._replicas.get()[0]._actor.set_ready()
    deployment_state.update()
    deployment_state.delete()
    deployment_state.update()

    # Replica should remain in STOPPING until it finishes.
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])
    assert deployment_state._replicas.get()[0]._actor.stopped

    for _ in range(10):
        deployment_state.update()

    # force_stop shouldn't be called until after the timer.
    assert not deployment_state._replicas.get()[0]._actor.force_stopped_counter
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])

    # Advance the timer, now the replica should be force stopped.
    timer.advance(grace_period_s + 0.1)
    deployment_state.update()
    assert deployment_state._replicas.get()[0]._actor.force_stopped_counter == 1
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Force stop should be called repeatedly until the replica stops.
    deployment_state.update()
    assert deployment_state._replicas.get()[0]._actor.force_stopped_counter == 2
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Once the replica is done stopping, it should be removed.
    replica = deployment_state._replicas.get()[0]
    replica._actor.set_done_stopping()
    deleted, _ = deployment_state.update()
    assert deleted
    check_counts(deployment_state, total=0)


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_redeploy_same_version(mock_get_all_node_ids, mock_deployment_state):
    # Redeploying with the same version and code should do nothing.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Test redeploying while the initial deployment is still pending.
    updating = deployment_state.deploy(b_info_1)
    assert not updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )

    # Mark the replica ready. After this, the initial goal should be complete.
    deployment_state._replicas.get()[0]._actor.set_ready()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Test redeploying after the initial deployment has finished.
    updating = deployment_state.deploy(b_info_1)
    assert not updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_redeploy_no_version(mock_get_all_node_ids, mock_deployment_state):
    # Redeploying with no version specified (`None`) should always redeploy
    # the replicas.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(version=None)
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Test redeploying while the initial deployment is still pending.
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # The initial replica should be stopping. The new replica shouldn't start
    # until the old one has completely stopped.
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()
    deployment_state.update()
    check_counts(deployment_state, total=0)

    # Now that the old replica has stopped, the new replica should be started.
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Check that the new replica has started.
    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])

    deployment_state.update()
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a third version after the transition has finished.
    b_info_3, b_version_3 = deployment_info(version="3")
    updating = deployment_state.deploy(b_info_3)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=0)

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deleted, _ = deployment_state.update()
    assert not deleted
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_redeploy_new_version(mock_get_all_node_ids, mock_deployment_state):
    # Redeploying with a new version should start a new replica.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Test redeploying while the initial deployment is still pending.
    b_info_2, b_version_2 = deployment_info(version="2")
    updating = deployment_state.deploy(b_info_2)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # The initial replica should be stopping. The new replica shouldn't start
    # until the old one has completely stopped.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()
    deployment_state.update()
    check_counts(deployment_state, total=0)

    # Now that the old replica has stopped, the new replica should be started.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()

    # Check that the new replica has started.
    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    deployment_state.update()
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a third version after the transition has finished.
    b_info_3, b_version_3 = deployment_info(version="3")
    updating = deployment_state.deploy(b_info_3)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=0)

    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    check_counts(
        deployment_state,
        version=b_version_3,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )

    deleted, _ = deployment_state.update()
    assert not deleted
    check_counts(
        deployment_state,
        version=b_version_3,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@pytest.mark.parametrize(
    "option,value",
    [
        ("user_config", {"hello": "world"}),
        ("max_concurrent_queries", 10),
        ("graceful_shutdown_timeout_s", DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_S + 1),
        ("graceful_shutdown_wait_loop_s", DEFAULT_GRACEFUL_SHUTDOWN_WAIT_LOOP_S + 1),
        ("health_check_period_s", DEFAULT_HEALTH_CHECK_PERIOD_S + 1),
        ("health_check_timeout_s", DEFAULT_HEALTH_CHECK_TIMEOUT_S + 1),
    ],
)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_deploy_new_config_same_code_version(
    mock_get_all_node_ids, mock_deployment_state, option, value
):
    # Deploying a new config with the same version should not deploy a new
    # replica.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(version="1")
    updated = deployment_state.deploy(b_info_1)
    assert updated
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Create the replica initially.
    deployment_state.update()
    deployment_state._replicas.get()[0]._actor.set_ready()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Update to a new config without changing the code version.
    b_info_2, b_version_2 = deployment_info(version="1", **{option: value})
    updated = deployment_state.deploy(b_info_2)
    assert updated
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    if option in ["user_config", "graceful_shutdown_wait_loop_s"]:
        deployment_state.update()
        check_counts(deployment_state, total=1)
        check_counts(
            deployment_state,
            version=b_version_2,
            total=1,
            by_state=[(ReplicaState.UPDATING, 1)],
        )
        # Mark the replica as ready.
        deployment_state._replicas.get()[0]._actor.set_ready()

    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_deploy_new_config_new_version(mock_get_all_node_ids, mock_deployment_state):
    # Deploying a new config with a new version should deploy a new replica.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    # Create the replica initially.
    deployment_state.update()
    deployment_state._replicas.get()[0]._actor.set_ready()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Update to a new config and a new version.
    b_info_2, b_version_2 = deployment_info(version="2", user_config={"hello": "world"})
    updating = deployment_state.deploy(b_info_2)
    assert updating

    # New version shouldn't start until old version is stopped.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()
    deployment_state.update()
    assert deployment_state._replicas.count() == 0
    check_counts(deployment_state, total=0)
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Now the new version should be started.
    deployment_state.update()
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )

    # Check that the new version is now running.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_initial_deploy_no_throttling(mock_get_all_node_ids, mock_deployment_state):
    # All replicas should be started at once for a new deployment.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(10)]

    b_info_1, b_version_1 = deployment_info(num_replicas=10, version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.STARTING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.RUNNING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_new_version_deploy_throttling(mock_get_all_node_ids, mock_deployment_state):
    # All replicas should be started at once for a new deployment.
    # When the version is updated, it should be throttled. The throttling
    # should apply to both code version and user config updates.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(10)]

    b_info_1, b_version_1 = deployment_info(
        num_replicas=10, version="1", user_config="1"
    )
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.STARTING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.RUNNING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a new version. Two old replicas should be stopped.
    b_info_2, b_version_2 = deployment_info(
        num_replicas=10, version="2", user_config="2"
    )
    updating = deployment_state.deploy(b_info_2)
    assert updating
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=10,
        by_state=[(ReplicaState.RUNNING, 8), (ReplicaState.STOPPING, 2)],
    )

    # Mark only one of the replicas as done stopping.
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=9,
        by_state=[(ReplicaState.RUNNING, 8), (ReplicaState.STOPPING, 1)],
    )

    # Now one of the new version replicas should start up.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=9,
        by_state=[(ReplicaState.RUNNING, 8), (ReplicaState.STOPPING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )

    # Mark the new version replica as ready. Another old version replica
    # should subsequently be stopped.
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    deployment_state.update()

    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=9,
        by_state=[(ReplicaState.RUNNING, 7), (ReplicaState.STOPPING, 2)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    # Mark the old replicas as done stopping.
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        1
    ]._actor.set_done_stopping()

    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Old replicas should be stopped and new versions started in batches of 2.
    new_replicas = 1
    old_replicas = 9
    while old_replicas > 3:
        deployment_state.update()

        check_counts(deployment_state, total=8)
        check_counts(
            deployment_state,
            version=b_version_1,
            total=old_replicas - 2,
            by_state=[(ReplicaState.RUNNING, old_replicas - 2)],
        )
        check_counts(
            deployment_state,
            version=b_version_2,
            total=new_replicas,
            by_state=[(ReplicaState.RUNNING, new_replicas)],
        )

        # Replicas starting up.
        deployment_state.update()
        check_counts(deployment_state, total=10)
        check_counts(
            deployment_state,
            version=b_version_1,
            total=old_replicas - 2,
            by_state=[(ReplicaState.RUNNING, old_replicas - 2)],
        )
        check_counts(
            deployment_state,
            version=b_version_2,
            total=new_replicas + 2,
            by_state=[(ReplicaState.RUNNING, new_replicas), (ReplicaState.STARTING, 2)],
        )

        # Set both ready.
        deployment_state._replicas.get(states=[ReplicaState.STARTING])[
            0
        ]._actor.set_ready()
        deployment_state._replicas.get(states=[ReplicaState.STARTING])[
            1
        ]._actor.set_ready()
        new_replicas += 2

        deployment_state.update()
        check_counts(deployment_state, total=10)
        check_counts(
            deployment_state,
            version=b_version_1,
            total=old_replicas - 2,
            by_state=[(ReplicaState.RUNNING, old_replicas - 2)],
        )
        check_counts(
            deployment_state,
            version=b_version_2,
            total=new_replicas,
            by_state=[(ReplicaState.RUNNING, new_replicas)],
        )

        # Two more old replicas should be stopped.
        old_replicas -= 2
        deployment_state.update()
        check_counts(deployment_state, total=10)
        check_counts(
            deployment_state,
            version=b_version_1,
            total=old_replicas,
            by_state=[
                (ReplicaState.RUNNING, old_replicas - 2),
                (ReplicaState.STOPPING, 2),
            ],
        )
        check_counts(
            deployment_state,
            version=b_version_2,
            total=new_replicas,
            by_state=[(ReplicaState.RUNNING, new_replicas)],
        )

        deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
            0
        ]._actor.set_done_stopping()
        deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
            1
        ]._actor.set_done_stopping()

        assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # 2 left to update.
    deployment_state.update()
    check_counts(deployment_state, total=8)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=new_replicas,
        by_state=[(ReplicaState.RUNNING, 7)],
    )

    # Replicas starting up.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=9,
        by_state=[(ReplicaState.RUNNING, 7), (ReplicaState.STARTING, 2)],
    )

    # Set both ready.
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[1]._actor.set_ready()

    # One replica remaining to update.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=9,
        by_state=[(ReplicaState.RUNNING, 9)],
    )

    # The last replica should be stopped.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=9,
        by_state=[(ReplicaState.RUNNING, 9)],
    )

    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=9)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=9,
        by_state=[(ReplicaState.RUNNING, 9)],
    )

    # The last replica should start up.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=10,
        by_state=[(ReplicaState.RUNNING, 9), (ReplicaState.STARTING, 1)],
    )

    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Set both ready.
    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=10,
        by_state=[(ReplicaState.RUNNING, 10)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_reconfigure_throttling(mock_get_all_node_ids, mock_deployment_state):
    # All replicas should be started at once for a new deployment.
    # When the version is updated, it should be throttled.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(
        num_replicas=2, version="1", user_config="1"
    )
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a new user_config. One replica should be updated.
    b_info_2, b_version_2 = deployment_info(
        num_replicas=2, version="1", user_config="2"
    )
    updating = deployment_state.deploy(b_info_2)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.UPDATING, 1)],
    )

    # Mark the updating replica as ready.
    deployment_state._replicas.get(states=[ReplicaState.UPDATING])[0]._actor.set_ready()

    # The updated replica should now be RUNNING.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # The second replica should now be updated.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.UPDATING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Mark the updating replica as ready.
    deployment_state._replicas.get(states=[ReplicaState.UPDATING])[0]._actor.set_ready()

    # Both replicas should now be RUNNING.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_new_version_and_scale_down(mock_get_all_node_ids, mock_deployment_state):
    # Test the case when we reduce the number of replicas and change the
    # version at the same time. First the number of replicas should be
    # turned down, then the rolling update should happen.
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    b_info_1, b_version_1 = deployment_info(num_replicas=10, version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.STARTING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=10, by_state=[(ReplicaState.RUNNING, 10)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a new version and scale down the number of replicas to 2.
    # First, 8 old replicas should be stopped to bring it down to the target.
    b_info_2, b_version_2 = deployment_info(num_replicas=2, version="2")
    updating = deployment_state.deploy(b_info_2)
    assert updating
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=10,
        by_state=[(ReplicaState.RUNNING, 2), (ReplicaState.STOPPING, 8)],
    )

    # Mark only one of the replicas as done stopping.
    # This should not yet trigger the rolling update because there are still
    # stopping replicas.
    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=9)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=9,
        by_state=[(ReplicaState.RUNNING, 2), (ReplicaState.STOPPING, 7)],
    )

    # Stop the remaining replicas.
    for replica in deployment_state._replicas.get(states=[ReplicaState.STOPPING]):
        replica._actor.set_done_stopping()

    # Now the rolling update should trigger, stopping one of the old replicas.
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )

    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.STOPPING, 1)],
    )

    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    # Old version stopped, new version should start up.
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )

    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    # New version is started, final old version replica should be stopped.
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    deployment_state._replicas.get(states=[ReplicaState.STOPPING])[
        0
    ]._actor.set_done_stopping()
    deployment_state.update()
    check_counts(deployment_state, total=1)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    # Final old version replica is stopped, final new version replica
    # should be started.
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.STARTING, 1)],
    )

    deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=2)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [False], indirect=True)
def test_new_version_and_scale_up(mock_deployment_state):
    # Test the case when we increase the number of replicas and change the
    # version at the same time. The new replicas should all immediately be
    # turned up. When they're up, rolling update should trigger.
    deployment_state, timer = mock_deployment_state

    b_info_1, b_version_1 = deployment_info(num_replicas=2, version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    # Now deploy a new version and scale up the number of replicas to 10.
    # 8 new replicas should be started.
    b_info_2, b_version_2 = deployment_info(num_replicas=10, version="2")
    updating = deployment_state.deploy(b_info_2)
    assert updating
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=8,
        by_state=[(ReplicaState.STARTING, 8)],
    )

    # Mark the new replicas as ready.
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=8,
        by_state=[(ReplicaState.RUNNING, 8)],
    )

    # Now that the new version replicas are up, rolling update should start.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=2,
        by_state=[(ReplicaState.RUNNING, 0), (ReplicaState.STOPPING, 2)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=8,
        by_state=[(ReplicaState.RUNNING, 8)],
    )

    # Mark the replicas as done stopping.
    for replica in deployment_state._replicas.get(states=[ReplicaState.STOPPING]):
        replica._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=8)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=8,
        by_state=[(ReplicaState.RUNNING, 8)],
    )

    # The remaining replicas should be started.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=10,
        by_state=[(ReplicaState.RUNNING, 8), (ReplicaState.STARTING, 2)],
    )

    # Mark the remaining replicas as ready.
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()

    # All new replicas should be up and running.
    deployment_state.update()
    check_counts(deployment_state, total=10)
    check_counts(
        deployment_state,
        version=b_version_2,
        total=10,
        by_state=[(ReplicaState.RUNNING, 10)],
    )

    deployment_state.update()
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_health_check(mock_get_all_node_ids, mock_deployment_state):
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2, version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()
        # Health check shouldn't be called until it's ready.
        assert not replica._actor.health_check_called

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    deployment_state.update()
    for replica in deployment_state._replicas.get():
        # Health check shouldn't be called until it's ready.
        assert replica._actor.health_check_called

    # Mark one replica unhealthy. It should be stopped.
    deployment_state._replicas.get()[0]._actor.set_unhealthy()
    deployment_state.update()
    check_counts(
        deployment_state,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.STOPPING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY

    replica = deployment_state._replicas.get(states=[ReplicaState.STOPPING])[0]
    replica._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY

    deployment_state.update()
    check_counts(
        deployment_state,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.STARTING, 1)],
    )

    replica = deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]
    replica._actor.set_ready()
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_update_while_unhealthy(mock_get_all_node_ids, mock_deployment_state):
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2, version="1")
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    for replica in deployment_state._replicas.get():
        replica._actor.set_ready()
        # Health check shouldn't be called until it's ready.
        assert not replica._actor.health_check_called

    # Check that the new replicas have started.
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY

    deployment_state.update()
    for replica in deployment_state._replicas.get():
        # Health check shouldn't be called until it's ready.
        assert replica._actor.health_check_called

    # Mark one replica unhealthy. It should be stopped.
    deployment_state._replicas.get()[0]._actor.set_unhealthy()
    deployment_state.update()
    check_counts(
        deployment_state,
        total=2,
        by_state=[(ReplicaState.RUNNING, 1), (ReplicaState.STOPPING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY

    replica = deployment_state._replicas.get(states=[ReplicaState.STOPPING])[0]
    replica._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY

    # Now deploy a new version (e.g., a rollback). This should update the status
    # to UPDATING and then it should eventually become healthy.
    b_info_2, b_version_2 = deployment_info(num_replicas=2, version="2")
    updating = deployment_state.deploy(b_info_2)
    assert updating

    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Check that a failure in the old version replica does not mark the
    # deployment as UNHEALTHY.
    deployment_state._replicas.get(states=[ReplicaState.RUNNING])[
        0
    ]._actor.set_unhealthy()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_1,
        total=1,
        by_state=[(ReplicaState.STOPPING, 1)],
    )
    check_counts(
        deployment_state,
        version=b_version_2,
        total=1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    replica = deployment_state._replicas.get(states=[ReplicaState.STOPPING])[0]
    replica._actor.set_done_stopping()

    # Another replica of the new version should get started.
    deployment_state.update()
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.STARTING, 2)],
    )

    # Mark new version replicas as ready.
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()

    # Both replicas should be RUNNING, deployment should be HEALTHY.
    deployment_state.update()
    check_counts(
        deployment_state,
        version=b_version_2,
        total=2,
        by_state=[(ReplicaState.RUNNING, 2)],
    )
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


def _constructor_failure_loop_two_replica(deployment_state, num_loops):
    """Helper function to exact constructor failure loops."""
    for i in range(num_loops):
        # Single replica should be created.
        deployment_state.update()
        check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])

        assert deployment_state._replica_constructor_retry_counter == i * 2

        replica_1 = deployment_state._replicas.get()[0]
        replica_2 = deployment_state._replicas.get()[1]

        replica_1._actor.set_failed_to_start()
        replica_2._actor.set_failed_to_start()
        # Now the replica should be marked SHOULD_STOP after failure.
        deployment_state.update()
        check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 2)])

        # Once it's done stopping, replica should be removed.
        replica_1._actor.set_done_stopping()
        replica_2._actor.set_done_stopping()
        deployment_state.update()
        check_counts(deployment_state, total=0)


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_deploy_with_consistent_constructor_failure(
    mock_get_all_node_ids, mock_deployment_state
):
    """
    Test deploy() multiple replicas with consistent constructor failure.

    The deployment should get marked FAILED.
    """
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2)
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING
    _constructor_failure_loop_two_replica(deployment_state, 3)

    assert deployment_state._replica_constructor_retry_counter == 6
    assert deployment_state.curr_status_info.status == DeploymentStatus.UNHEALTHY
    check_counts(deployment_state, total=0)
    assert deployment_state.curr_status_info.message != ""


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_deploy_with_partial_constructor_failure(
    mock_get_all_node_ids, mock_deployment_state
):
    """
    Test deploy() multiple replicas with constructor failure exceedining
    pre-set limit but achieved partial success with at least 1 running replica.

    Ensures:
        1) Deployment status doesn't get marked FAILED.
        2) There should be expected # of RUNNING replicas eventually that
            matches user intent
        3) Replica counter set as -1 to stop tracking current goal as it's
            already completed

    Same testing for same test case in test_deploy.py.
    """
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2)
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    _constructor_failure_loop_two_replica(deployment_state, 2)
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state._replica_constructor_retry_counter == 4
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Let one replica reach RUNNING state while the other still fails
    replica_1 = deployment_state._replicas.get()[0]
    replica_2 = deployment_state._replicas.get()[1]
    replica_1._actor.set_ready()
    replica_2._actor.set_failed_to_start()

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 1)])

    # Ensure failed to start replica is removed
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 1)])

    replica_2._actor.set_done_stopping()
    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 0)])

    # New update cycle should spawn new replica after previous one is removed
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 1)])

    # Set the starting one to fail again and trigger retry limit
    starting_replica = deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]
    starting_replica._actor.set_failed_to_start()

    deployment_state.update()
    # Ensure our goal returned with construtor start counter reset
    assert deployment_state._replica_constructor_retry_counter == -1
    # Deployment should NOT be considered complete yet
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 1)])

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 1)])
    starting_replica = deployment_state._replicas.get(states=[ReplicaState.STOPPING])[0]
    starting_replica._actor.set_done_stopping()

    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 0)])

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 1)])
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 1)])

    starting_replica = deployment_state._replicas.get(states=[ReplicaState.STARTING])[0]
    starting_replica._actor.set_ready()

    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])

    # Deployment should be considered complete
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_deploy_with_transient_constructor_failure(
    mock_get_all_node_ids, mock_deployment_state
):
    """
    Test deploy() multiple replicas with transient constructor failure.
    Ensures:
        1) Deployment status gets marked as RUNNING.
        2) There should be expected # of RUNNING replicas eventually that
            matches user intent.
        3) Replica counter set as -1 to stop tracking current goal as it's
            already completed.

    Same testing for same test case in test_deploy.py.
    """
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2)
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Burn 4 retries from both replicas.
    _constructor_failure_loop_two_replica(deployment_state, 2)
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    # Let both replicas succeed in last try.
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    assert deployment_state._replica_constructor_retry_counter == 4
    replica_1 = deployment_state._replicas.get()[0]
    replica_2 = deployment_state._replicas.get()[1]

    replica_1._actor.set_ready()
    replica_2._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])

    assert deployment_state._replica_constructor_retry_counter == 4
    assert deployment_state.curr_status_info.status == DeploymentStatus.HEALTHY


@pytest.mark.parametrize("mock_deployment_state", [False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_exponential_backoff(mock_get_all_node_ids, mock_deployment_state):
    """Test exponential backoff."""
    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(2)]

    b_info_1, b_version_1 = deployment_info(num_replicas=2)
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    _constructor_failure_loop_two_replica(deployment_state, 3)
    assert deployment_state._replica_constructor_retry_counter == 6
    last_retry = timer.time()

    for i in range(7):
        while timer.time() - last_retry < 2**i:
            deployment_state.update()
            assert deployment_state._replica_constructor_retry_counter == 6 + 2 * i
            # Check that during backoff time, no replicas are created
            check_counts(deployment_state, total=0)
            timer.advance(0.1)  # simulate time passing between each call to udpate

        # Skip past random additional backoff time used to avoid synchronization
        timer.advance(5)

        # Set new replicas to fail consecutively
        check_counts(deployment_state, total=0)  # No replicas
        deployment_state.update()
        last_retry = timer.time()  # This should be time at which replicas were retried
        check_counts(deployment_state, total=2)  # Two new replicas
        replica_1 = deployment_state._replicas.get()[0]
        replica_2 = deployment_state._replicas.get()[1]
        replica_1._actor.set_failed_to_start()
        replica_2._actor.set_failed_to_start()
        timer.advance(0.1)  # simulate time passing between each call to udpate

        # Now the replica should be marked STOPPING after failure.
        deployment_state.update()
        check_counts(deployment_state, total=2, by_state=[(ReplicaState.STOPPING, 2)])
        timer.advance(0.1)  # simulate time passing between each call to udpate

        # Once it's done stopping, replica should be removed.
        replica_1._actor.set_done_stopping()
        replica_2._actor.set_done_stopping()
        deployment_state.update()
        check_counts(deployment_state, total=0)
        timer.advance(0.1)  # simulate time passing between each call to udpate


@pytest.fixture
def mock_deployment_state_manager(request) -> Tuple[DeploymentStateManager, Mock]:
    ray.init()
    timer = MockTimer()
    with patch(
        "ray.serve._private.deployment_state.ActorReplicaWrapper",
        new=MockReplicaActorWrapper,
    ), patch("time.time", new=timer.time), patch(
        "ray.serve._private.long_poll.LongPollHost"
    ) as mock_long_poll:

        kv_store = RayInternalKVStore("test")
        all_current_actor_names = []
        deployment_state_manager = DeploymentStateManager(
            "name",
            True,
            kv_store,
            mock_long_poll,
            all_current_actor_names,
        )
        deployment_state = DeploymentState(
            "test",
            "name",
            True,
            mock_long_poll,
            deployment_state_manager._save_checkpoint_func,
        )

        yield deployment_state_manager, deployment_state, timer
    ray.shutdown()


@pytest.mark.parametrize("is_driver_deployment", [False, True])
def test_shutdown(mock_deployment_state_manager, is_driver_deployment):
    """
    Test that shutdown waits for all deployments to be deleted and they
    are force-killed without a grace period.
    """
    deployment_state_manager, deployment_state, timer = mock_deployment_state_manager

    tag = "test"

    grace_period_s = 10
    b_info_1, b_version_1 = deployment_info(
        graceful_shutdown_timeout_s=grace_period_s,
        is_driver_deployment=is_driver_deployment,
    )
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state_manager._deployment_states[tag] = deployment_state

    # Single replica should be created.
    deployment_state_manager.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])
    deployment_state._replicas.get()[0]._actor.set_ready()

    # Now the replica should be marked running.
    deployment_state_manager.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.RUNNING, 1)])

    # Test shutdown flow
    assert not deployment_state._replicas.get()[0]._actor.stopped

    deployment_state_manager.shutdown()

    timer.advance(grace_period_s + 0.1)
    deployment_state_manager.update()

    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STOPPING, 1)])
    assert deployment_state._replicas.get()[0]._actor.stopped
    assert len(deployment_state_manager.get_deployment_statuses()) > 0

    # Once it's done stopping, replica should be removed.
    replica = deployment_state._replicas.get()[0]
    replica._actor.set_done_stopping()
    deployment_state_manager.update()
    check_counts(deployment_state, total=0)
    assert len(deployment_state_manager.get_deployment_statuses()) == 0


@pytest.mark.parametrize("is_driver_deployment", [False, True])
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_resume_deployment_state_from_replica_tags(
    mock_get_all_node_ids, is_driver_deployment, mock_deployment_state_manager
):
    deployment_state_manager, deployment_state, timer = mock_deployment_state_manager
    mock_get_all_node_ids.return_value = [("node-id", "node-id")]

    tag = "test"

    # Step 1: Create some deployment info with actors in running state
    b_info_1, b_version_1 = deployment_info(
        version="1", is_driver_deployment=is_driver_deployment
    )
    updating = deployment_state.deploy(b_info_1)
    assert updating

    deployment_state_manager._deployment_states[tag] = deployment_state

    # Single replica should be created.
    any_recovering = deployment_state_manager.update()
    assert not any_recovering
    check_counts(
        deployment_state,
        total=1,
        version=b_version_1,
        by_state=[(ReplicaState.STARTING, 1)],
    )
    deployment_state._replicas.get()[0]._actor.set_ready()

    # Now the replica should be marked running.
    any_recovering = deployment_state_manager.update()
    assert not any_recovering
    check_counts(
        deployment_state,
        total=1,
        version=b_version_1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )

    mocked_replica = deployment_state._replicas.get(states=[ReplicaState.RUNNING])[0]

    # Step 2: Delete _replicas from deployment_state
    deployment_state._replicas = ReplicaStateContainer()

    # Step 3: Create new deployment_state by resuming from passed in replicas
    deployment_state_manager._recover_from_checkpoint(
        [ReplicaName.prefix + mocked_replica.replica_tag]
    )

    # Step 4: Ensure new deployment_state is correct
    # deployment state behind "test" is re-created in recovery flow
    deployment_state = deployment_state_manager._deployment_states[tag]
    # Ensure recovering replica begin with no version assigned
    check_counts(
        deployment_state, total=1, version=None, by_state=[(ReplicaState.RECOVERING, 1)]
    )

    # Now the replica should be marked running.
    deployment_state._replicas.get()[0]._actor.set_ready()
    deployment_state._replicas.get()[0]._actor.set_starting_version(b_version_1)
    any_recovering = deployment_state_manager.update()
    assert not any_recovering
    check_counts(
        deployment_state,
        total=1,
        version=b_version_1,
        by_state=[(ReplicaState.RUNNING, 1)],
    )
    # Ensure same replica name is used
    assert deployment_state._replicas.get()[0].replica_tag == mocked_replica.replica_tag

    any_recovering = deployment_state_manager.update()
    assert not any_recovering


def test_stopping_replicas_ranking():
    @dataclass
    class MockReplica:
        actor_node_id: str

    def compare(before, after):
        before_replicas = [MockReplica(item) for item in before]
        after_replicas = [MockReplica(item) for item in after]
        result_replicas = rank_replicas_for_stopping(before_replicas)
        assert result_replicas == after_replicas

    compare(
        [None, 1, None], [None, None, 1]
    )  # replicas not allocated should be stopped first
    compare(
        [3, 3, 3, 2, 2, 1], [1, 2, 2, 3, 3, 3]
    )  # prefer to stop dangling replicas first
    compare([2, 2, 3, 3], [2, 2, 3, 3])  # if equal, ordering should be kept


@pytest.mark.parametrize("mock_deployment_state", [True, False], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_resource_requirements_none(mock_get_all_node_ids, mock_deployment_state):
    """Ensure resource_requirements doesn't break if a requirement is None"""

    class FakeActor:

        actor_resources = {"num_cpus": 2, "fake": None}
        available_resources = {}

    # Make a DeploymentReplica just to accesss its resource_requirement function
    replica = DeploymentReplica(None, None, None, None, None)
    replica._actor = FakeActor()

    # resource_requirements() should not error
    replica.resource_requirements()


@pytest.mark.parametrize("mock_deployment_state", [True], indirect=True)
@patch.object(DriverDeploymentState, "_get_all_node_ids")
def test_add_and_remove_nodes_for_driver_deployment(
    mock_get_all_node_ids, mock_deployment_state
):

    deployment_state, timer = mock_deployment_state
    mock_get_all_node_ids.return_value = [("0", "0")]

    b_info_1, b_version_1 = deployment_info()
    updating = deployment_state.deploy(b_info_1)
    assert updating
    assert deployment_state.curr_status_info.status == DeploymentStatus.UPDATING

    deployment_state.update()
    check_counts(deployment_state, total=1, by_state=[(ReplicaState.STARTING, 1)])

    # Add a node when previous one is in STARTING state
    mock_get_all_node_ids.return_value = [("0", "0"), ("1", "1")]
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.STARTING, 2)])
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=2, by_state=[(ReplicaState.RUNNING, 2)])

    # Add another two nodes
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(4)]
    deployment_state.update()
    check_counts(
        deployment_state,
        total=4,
        by_state=[(ReplicaState.RUNNING, 2), (ReplicaState.STARTING, 2)],
    )
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()
    deployment_state.update()
    check_counts(deployment_state, total=4, by_state=[(ReplicaState.RUNNING, 4)])

    # Remove one node and add another node, node3 is removed, node4 added
    mock_get_all_node_ids.return_value = [(str(i), str(i)) for i in range(3)] + [
        ("4", "4")
    ]
    deployment_state._replicas.get(states=[ReplicaState.RUNNING])[
        3
    ]._actor.set_unhealthy()
    deployment_state.update()
    check_counts(
        deployment_state,
        total=5,
        by_state=[
            (ReplicaState.RUNNING, 3),
            (ReplicaState.STARTING, 1),
            (ReplicaState.STOPPING, 1),
        ],
    )

    # Mark stopped replica finish stopping step and starting replica
    # finish starting step
    for replica in deployment_state._replicas.get(states=[ReplicaState.STARTING]):
        replica._actor.set_ready()
    for replica in deployment_state._replicas.get(states=[ReplicaState.STOPPING]):
        replica._actor.set_done_stopping()
    deployment_state.update()
    check_counts(deployment_state, total=4, by_state=[(ReplicaState.RUNNING, 4)])


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))

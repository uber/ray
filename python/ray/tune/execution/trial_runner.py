from typing import Any, Dict, List, Optional, Union, Tuple, Set

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
import traceback
import warnings

import ray
from ray.air._internal.uri_utils import URI
from ray.air.config import CheckpointConfig
from ray.air._internal.checkpoint_manager import CheckpointStorage, _TrackedCheckpoint
from ray.exceptions import RayTaskError
from ray.tune.error import _TuneStopTrialError, _TuneRestoreError
from ray.tune.execution.experiment_state import (
    _ExperimentCheckpointManager,
    _find_newest_experiment_checkpoint,
    _experiment_checkpoint_exists,
)
from ray.tune.utils.util import _split_remote_local_path
from ray.util import get_node_ip_address
from ray.tune import TuneError
from ray.tune.callback import CallbackList, Callback
from ray.tune.experiment import Experiment
from ray.tune.execution.insufficient_resources_manager import (
    _InsufficientResourcesManager,
)
from ray.tune.execution.ray_trial_executor import (
    RayTrialExecutor,
    _ExecutorEventType,
    _ExecutorEvent,
)
from ray.tune.result import (
    DEBUG_METRICS,
    DEFAULT_METRIC,
    DONE,
    TIME_THIS_ITER_S,
    RESULT_DUPLICATE,
    SHOULD_CHECKPOINT,
    _get_defaults_results_dir,
    DEFAULT_EXPERIMENT_NAME,
)
from ray.tune.schedulers import FIFOScheduler, TrialScheduler
from ray.tune.stopper import NoopStopper, Stopper
from ray.tune.search import BasicVariantGenerator, SearchAlgorithm
from ray.tune.syncer import SyncConfig, get_node_to_storage_syncer
from ray.tune.experiment import Trial
from ray.tune.utils import warn_if_slow, flatten_dict
from ray.tune.utils.log import Verbosity, has_verbosity
from ray.tune.execution.placement_groups import PlacementGroupFactory
from ray.tune.utils.serialization import TuneFunctionDecoder, TuneFunctionEncoder
from ray.tune.web_server import TuneServer
from ray.util.annotations import DeveloperAPI, Deprecated
from ray.util.debug import log_once

MAX_DEBUG_TRIALS = 20

logger = logging.getLogger(__name__)


class _TuneControllerBase:
    """A TrialRunner implements the event loop for scheduling trials on Ray.

    .. code-block: python

        runner = TrialRunner()
        runner.add_trial(Trial(...))
        runner.add_trial(Trial(...))
        while not runner.is_finished():
            runner.step()

    The main job of TrialRunner is scheduling trials to efficiently use cluster
    resources, without overloading the cluster.

    While Ray itself provides resource management for tasks and actors, this is
    not sufficient when scheduling trials that may instantiate multiple actors.
    This is because if insufficient resources are available, concurrent trials
    could deadlock waiting for new resources to become available. Furthermore,
    oversubscribing the cluster could degrade training performance, leading to
    misleading benchmark results.

    Args:
        search_alg: SearchAlgorithm for generating
            Trial objects.
        scheduler: Defaults to FIFOScheduler.
        experiment_path: Path where global experiment state checkpoints
            are saved and restored from. If this is a remote URI,
            experiment checkpoints will be synced to this location.
        sync_config: See :class:`~ray.tune.syncer.SyncConfig`.
        experiment_dir_name: Experiment directory name.
            See :class:`~ray.tune.experiment.Experiment`.
        stopper: Custom class for stopping whole experiments. See ``Stopper``.
        resume: see `tune.py:run`.
        server_port: Port number for launching TuneServer.
        fail_fast: Finishes as soon as a trial fails if True.
            If fail_fast='raise' provided, Tune will automatically
            raise the exception received by the Trainable. fail_fast='raise'
            can easily leak resources and should be used with caution.
        checkpoint_period: Trial runner checkpoint periodicity in
            seconds. Defaults to ``"auto"``, which adjusts checkpointing
            time so that at most 5% of the time is spent on writing
            checkpoints.
        callbacks: List of callbacks that will be called at different
            times in the training loop. Must be instances of the
            ``ray.tune.execution.trial_runner.Callback`` class.
        metric: Metric used to check received results. If a result is
            reported without this metric, an error will be raised. The error
            can be omitted by not providing a metric or by setting the env
            variable ``TUNE_DISABLE_STRICT_METRIC_CHECKING=0``

    """

    CKPT_FILE_TMPL = "experiment_state-{}.json"
    RAISE = "RAISE"

    def __init__(
        self,
        *,
        search_alg: Optional[SearchAlgorithm] = None,
        placeholder_resolvers: Optional[Dict[Tuple, Any]] = None,
        scheduler: Optional[TrialScheduler] = None,
        experiment_path: Optional[str] = None,
        sync_config: Optional[SyncConfig] = None,
        experiment_dir_name: Optional[str] = None,
        stopper: Optional[Stopper] = None,
        resume: Union[str, bool] = False,
        server_port: Optional[int] = None,
        fail_fast: bool = False,
        checkpoint_period: Union[str, int] = None,
        callbacks: Optional[List[Callback]] = None,
        metric: Optional[str] = None,
        trial_checkpoint_config: Optional[CheckpointConfig] = None,
    ):
        self._search_alg = search_alg or BasicVariantGenerator()
        self._placeholder_resolvers = placeholder_resolvers
        self._scheduler_alg = scheduler or FIFOScheduler()
        self._callbacks = CallbackList(callbacks or [])
        self._insufficient_resources_manager = _InsufficientResourcesManager()
        self._pending_trial_queue_times = {}

        self._max_pending_trials = _get_max_pending_trials(self._search_alg)

        self._sync_config = sync_config or SyncConfig()

        # Rename for better code readability
        local_experiment_path, remote_experiment_path = _split_remote_local_path(
            experiment_path, None
        )

        # Derive experiment dir name from local path
        if not experiment_dir_name and local_experiment_path:
            # Maybe derive experiment dir name from local storage dir
            experiment_dir_name = Path(local_experiment_path).name
        elif not experiment_dir_name:
            experiment_dir_name = DEFAULT_EXPERIMENT_NAME

        # Set default experiment dir name
        if not local_experiment_path:
            local_experiment_path = str(
                Path(_get_defaults_results_dir()) / experiment_dir_name
            )
            os.makedirs(local_experiment_path, exist_ok=True)

        self._experiment_dir_name = experiment_dir_name

        if self._sync_config.upload_dir and self._experiment_dir_name:
            if remote_experiment_path:
                if not remote_experiment_path.startswith(self.sync_config.upload_dir):
                    raise ValueError(
                        f"Both a `SyncConfig.upload_dir` and an `experiment_path` "
                        f"pointing to remote storage were passed, but they do not "
                        f"point to the same location. Got: "
                        f"`experiment_path={experiment_path}` and "
                        f"`SyncConfig.upload_dir={self.sync_config.upload_dir}`. "
                    )
                warnings.warn(
                    "If `experiment_path` points to a remote storage location, "
                    "do not set `SyncConfig.upload_dir`. ",
                    DeprecationWarning,
                )
            else:
                remote_experiment_path = str(
                    URI(self._sync_config.upload_dir) / self._experiment_dir_name
                )

        self._local_experiment_path = local_experiment_path
        self._remote_experiment_path = remote_experiment_path

        self._metric = metric

        self._total_time = 0
        self._iteration = 0
        self._has_errored = False
        self._fail_fast = fail_fast
        if isinstance(self._fail_fast, str):
            self._fail_fast = self._fail_fast.upper()
            if self._fail_fast == self.RAISE:
                warnings.warn(
                    "fail_fast='raise' detected. Be careful when using this "
                    "mode as resources (such as Ray processes, "
                    "file descriptors, and temporary files) may not be "
                    "cleaned up properly. To use "
                    "a safer mode, use fail_fast=True."
                )
            else:
                raise ValueError(
                    "fail_fast must be one of {bool, RAISE}. " f"Got {self._fail_fast}."
                )

        self._print_trial_errors = bool(
            int(os.environ.get("TUNE_PRINT_ALL_TRIAL_ERRORS", "1"))
        )

        self._server = None
        self._server_port = server_port
        if server_port is not None:
            self._server = TuneServer(self, self._server_port)

        self._trials: List[Trial] = []
        self._live_trials: Set[Trial] = set()  # Set of non-terminated trials
        self._cached_trial_decisions = {}
        self._queued_trial_decisions = {}

        self._stop_queue = []
        self._should_stop_experiment = False  # used by TuneServer

        if self._local_experiment_path:
            os.makedirs(self._local_experiment_path, exist_ok=True)

        self._stopper = stopper or NoopStopper()

        self._start_time = time.time()
        self._last_checkpoint_time = -float("inf")

        self._session_str = datetime.fromtimestamp(self._start_time).strftime(
            "%Y-%m-%d_%H-%M-%S"
        )

        if checkpoint_period is None:
            checkpoint_period = os.getenv("TUNE_GLOBAL_CHECKPOINT_S", "auto")

        self._checkpoint_period = checkpoint_period
        self._trial_checkpoint_config = trial_checkpoint_config or CheckpointConfig()
        self._checkpoint_manager = self._create_checkpoint_manager()

        self._resumed = False
        resume_config = self._checkpoint_manager.resume(resume_type=resume)

        if resume_config:
            try:
                self.resume(
                    resume_unfinished=resume_config.resume_unfinished,
                    resume_errored=resume_config.resume_errored,
                    restart_errored=resume_config.restart_errored,
                )
                self._resumed = True
            except Exception as e:
                if has_verbosity(Verbosity.V3_TRIAL_DETAILS):
                    logger.error(str(e))
                logger.exception("Runner restore failed.")
                if self._fail_fast:
                    raise
                logger.info("Restarting experiment.")
        else:
            logger.debug("Starting a new experiment.")

    def _wrapped(self):
        raise RuntimeError

    @property
    def resumed(self):
        return self._resumed

    @property
    def search_alg(self):
        return self._search_alg

    @property
    def scheduler_alg(self):
        return self._scheduler_alg

    def setup_experiments(
        self, experiments: List[Experiment], total_num_samples: int
    ) -> None:
        """Obtains any necessary information from experiments.

        Mainly used to setup callbacks.

        Args:
            experiments: List of Experiments
                to use.
            total_num_samples: Total number of samples
                factoring in grid search samplers.
        """
        experiment = experiments[0]
        spec = experiment.public_spec if experiment else {}
        spec["total_num_samples"] = total_num_samples
        self._callbacks.setup(**spec)

    def end_experiment_callbacks(self) -> None:
        """Calls ``on_experiment_end`` method in callbacks."""
        self._callbacks.on_experiment_end(trials=self._trials)

    @Deprecated("Use `TrialRunner.experiment_state_path` instead.")
    @property
    def checkpoint_file(self) -> str:
        return self.experiment_state_path

    @property
    def experiment_state_file_name(self) -> str:
        return self.CKPT_FILE_TMPL.format(self._session_str)

    @property
    def experiment_state_path(self) -> str:
        """Returns the local experiment checkpoint path."""
        return os.path.join(
            self._local_experiment_path, self.experiment_state_file_name
        )

    @property
    def experiment_path(self) -> str:
        return self._remote_experiment_path or self._local_experiment_path

    def _create_checkpoint_manager(self):
        return _ExperimentCheckpointManager(
            local_checkpoint_dir=self._local_experiment_path,
            remote_checkpoint_dir=self._remote_experiment_path,
            checkpoint_period=self._checkpoint_period,
            sync_config=self._sync_config,
            sync_every_n_trial_checkpoints=self._trial_checkpoint_config.num_to_keep,
        )

    @classmethod
    def checkpoint_exists(cls, directory: str) -> bool:
        if not os.path.exists(directory):
            return False

        return _experiment_checkpoint_exists(directory)

    def save_to_dir(self, experiment_dir: Optional[str] = None):
        """Save TrialRunner state to experiment directory.

        Accepts an ``experiment_dir`` argument which defaults to the
        local checkpoint directory.

        This method will save the trial runner state, the searcher state,
        and the callback states into the experiment directory.
        """
        experiment_dir = experiment_dir or self._local_experiment_path

        # Get state from trial executor and runner
        runner_state = {
            # Trials
            "checkpoints": list(self._get_trial_checkpoints().values()),
            # Experiment data
            "runner_data": self.__getstate__(),
            # Metadata
            "stats": {
                "start_time": self._start_time,
                "timestamp": self._last_checkpoint_time,
            },
        }

        tmp_file_name = os.path.join(experiment_dir, ".tmp_experiment_state")

        with open(tmp_file_name, "w") as f:
            json.dump(runner_state, f, indent=2, cls=TuneFunctionEncoder)

        os.replace(
            tmp_file_name,
            os.path.join(experiment_dir, self.experiment_state_file_name),
        )

        self._search_alg.save_to_dir(
            self._local_experiment_path, session_str=self._session_str
        )
        self._callbacks.save_to_dir(
            self._local_experiment_path, session_str=self._session_str
        )

    def restore_from_dir(self, experiment_dir: Optional[str] = None) -> List[Trial]:
        """Restore TrialRunner state from experiment directory.

        Accepts an ``experiment_dir`` argument which defaults to the
        local checkpoint directory.

        This method will restore the trial runner state, the searcher state,
        and the callback states. It will then parse the trial states
        and return them as a list of Trial objects.
        """
        experiment_dir = experiment_dir or self._local_experiment_path

        # Update local checkpoint dir
        self._local_experiment_path = experiment_dir

        # Find newest state file
        newest_state_path = _find_newest_experiment_checkpoint(
            self._local_experiment_path
        )

        if not newest_state_path:
            raise ValueError(
                f"Tried to resume experiment from directory "
                f"`{self._local_experiment_path}`, but no "
                f"experiment checkpoint data was found."
            )

        # Set checkpoint file to load
        logger.warning(
            f"Attempting to resume experiment from {self._local_experiment_path}. "
            "This will ignore any new changes to the specification."
        )
        logger.info(
            "Using the newest experiment state file found within the "
            f"experiment directory: {Path(newest_state_path).name}"
        )

        # Actually load data
        with open(newest_state_path, "r") as f:
            runner_state = json.load(f, cls=TuneFunctionDecoder)

        # 1. Restore trial runner state
        self.__setstate__(runner_state["runner_data"])

        # 2. Restore search algorithm and callback state
        if self._search_alg.has_checkpoint(self._local_experiment_path):
            self._search_alg.restore_from_dir(self._local_experiment_path)

        if self._callbacks.can_restore(self._local_experiment_path):
            self._callbacks.restore_from_dir(self._local_experiment_path)

        # 3. Load trials
        trials = []
        for trial_json_state in runner_state["checkpoints"]:
            trial = Trial.from_json_state(trial_json_state)

            # The following properties may be updated on restoration
            # Ex: moved local/cloud experiment directory
            # ATTN: Set `local_experiment_path` to update trial checkpoints!
            trial.local_experiment_path = self._local_experiment_path
            trial.remote_experiment_path = self._remote_experiment_path
            trial.sync_config = self._sync_config
            trial.experiment_dir_name = self._experiment_dir_name

            # Avoid creating logdir in client mode for returned trial results,
            # since the dir might not be creatable locally.
            # TODO(ekl) this is kind of a hack.
            if not ray.util.client.ray.is_connected():
                trial.init_local_path()  # Create logdir if it does not exist

            trials.append(trial)

        return trials

    def checkpoint(self, force: bool = False, wait: bool = False):
        """Saves execution state to `self._local_experiment_path`.

        Overwrites the current session checkpoint, which starts when self
        is instantiated. Throttle depends on self._checkpoint_period.

        Also automatically saves the search algorithm to the local
        checkpoint dir.

        Args:
            force: Forces a checkpoint despite checkpoint_period.
            wait: Wait until syncing to cloud has finished.

        """
        with warn_if_slow(
            "experiment_checkpoint",
            message="Checkpointing the experiment state took "
            "{duration:.3f} s, which may be a performance "
            "bottleneck. Please ensure the "
            "`TUNE_GLOBAL_CHECKPOINT_S` environment variable is "
            "something significantly higher than this duration "
            "to ensure compute time is mostly spent on the main "
            "training loop.",
            # No backlog warning if forced checkpoint as we wait
            # for previous sync to finish.
            disable=self._checkpoint_manager.auto_checkpoint_enabled or force or wait,
        ):
            self._checkpoint_manager.checkpoint(
                save_fn=self.save_to_dir, force=force, wait=wait
            )

    def resume(
        self,
        resume_unfinished: bool = True,
        resume_errored: bool = False,
        restart_errored: bool = False,
    ):
        """Resumes all checkpointed trials from previous run.

        Requires user to manually re-register their objects. Also stops
        all ongoing trials.
        """
        trials = self.restore_from_dir()

        # Set trial statuses according to the resume configuration
        for trial in sorted(trials, key=lambda t: t.last_update_time, reverse=True):
            trial_to_add = trial
            if trial.status == Trial.ERROR:
                if resume_errored:
                    # Keep trial ID on resume
                    trial_to_add.error_filename = None
                    trial_to_add.pickled_error_filename = None
                    trial_to_add.set_status(Trial.PENDING)
                    trial_to_add.restore_path = trial.checkpoint.dir_or_data
                elif restart_errored:
                    trial_to_add = trial.reset()
                    trial_to_add.restore_path = None
            elif trial.status != Trial.TERMINATED and not resume_unfinished:
                trial_to_add.status = Trial.TERMINATED
            self.add_trial(trial_to_add)

    def update_pending_trial_resources(
        self, resources: Union[dict, PlacementGroupFactory]
    ):
        """Update trial resources when resuming from checkpoint.

        Only updating the pending ones.
        """
        assert resources
        if isinstance(resources, dict) and "gpu" not in resources:
            resources["gpu"] = 0
        for trial in self._trials:
            if trial.status == Trial.PENDING:
                trial.update_resources(resources=resources)

    def is_finished(self):
        """Returns whether all trials have finished running."""
        # The checks here are partly redundant but optimized for quick
        # evaluation. Specifically, if there are live trials, we check
        # these live trials first. Only if none of the live trials is
        # live anymore do we loop over all trials for a final check.
        trials_done = (
            len(self._live_trials) == 0
            or all(trial.is_finished() for trial in self._live_trials)
        ) and all(trial.is_finished() for trial in self._trials)
        return trials_done and self._search_alg.is_finished()

    def get_trial(self, tid):
        trial = [t for t in self._trials if t.trial_id == tid]
        return trial[0] if trial else None

    def get_trials(self):
        """Returns the list of trials managed by this TrialRunner.

        Note that the caller usually should not mutate trial state directly.
        """
        return self._trials

    def get_live_trials(self):
        """Returns the set of trials that are not in Trial.TERMINATED state."""
        return self._live_trials

    def _get_trial_checkpoints(self) -> Dict[str, str]:
        raise NotImplementedError

    def _mark_trial_to_checkpoint(self, trial: Trial):
        raise NotImplementedError

    def _set_trial_status(self, trial: Trial, status: str):
        raise NotImplementedError

    def _cleanup_trials(self):
        raise NotImplementedError

    def add_trial(self, trial: Trial):
        """Adds a new trial to this TrialRunner.

        Trials may be added at any time.

        Args:
            trial: Trial to queue.
        """
        # If the config map has had all the references replaced with placeholders,
        # resolve them before adding the trial.
        if self._placeholder_resolvers:
            trial.resolve_config_placeholders(self._placeholder_resolvers)

        # With trial.config resolved, create placement group factory if needed.
        trial.create_placement_group_factory()

        self._trials.append(trial)
        if trial.status != Trial.TERMINATED:
            self._live_trials.add(trial)
        with warn_if_slow("scheduler.on_trial_add"):
            self._scheduler_alg.on_trial_add(self._wrapped(), trial)
        self._mark_trial_to_checkpoint(trial)

    def _used_resources_string(self) -> str:
        raise NotImplementedError

    def debug_string(self, delim="\n"):
        from ray.tune.progress_reporter import _trial_progress_str

        result_keys = [list(t.last_result) for t in self.get_trials() if t.last_result]
        metrics = set().union(*result_keys)
        messages = [
            self._scheduler_alg.debug_string(),
            self._used_resources_string(),
            _trial_progress_str(self.get_trials(), metrics, force_table=True),
        ]
        return delim.join(messages)

    def step(self):
        raise NotImplementedError

    def _maybe_execute_queued_decision(self, trial):
        # `self._queued_trial_decisions` now contains a final decision
        # based on all results
        final_decision = self._queued_trial_decisions.pop(trial.trial_id, None)
        if final_decision:
            logger.debug(
                f"Executing final queued decision for {trial}: {final_decision}"
            )
            self._execute_action(trial, final_decision)

    def _schedule_trial_stop(self, trial: Trial, exception: Optional[Exception] = None):
        raise NotImplementedError

    def _schedule_trial_pause(self, trial: Trial, should_checkpoint: bool = True):
        raise NotImplementedError

    def _stop_experiment_if_needed(self):
        """Stops all trials."""
        fail_fast = self._fail_fast and self._has_errored
        if self._stopper.stop_all() or fail_fast or self._should_stop_experiment:
            self._search_alg.set_finished()
            [
                self._schedule_trial_stop(t)
                for t in self._trials
                if t.status not in {Trial.ERROR, Trial.TERMINATED}
            ]

    ###
    # FAILURE

    def _process_trial_failure(
        self, trial: Trial, exception: Optional[Union[TuneError, RayTaskError]] = None
    ):
        """Handle trial failure.

        Attempt trial recovery if possible, clean up state otherwise.

        Args:
            trial: Failed trial.
            exception: Exception prior to invoking this method.
        """
        self._has_errored = True
        if trial.status == Trial.RUNNING:
            if trial.should_recover():
                self._try_recover(trial, exc=exception)
            else:
                self._scheduler_alg.on_trial_error(self, trial)
                self._search_alg.on_trial_complete(trial.trial_id, error=True)
                self._callbacks.on_trial_error(
                    iteration=self._iteration, trials=self._trials, trial=trial
                )
                self._schedule_trial_stop(trial, exception=exception)

    ###
    # STOP

    def stop_trial(self, trial):
        """The canonical implementation of stopping a trial.

        Trials may be in any external status when this function is called.
        If trial is in state PENDING or PAUSED, calls `on_trial_remove` for
        scheduler and `on_trial_complete()` for search_alg.
        If trial is in state RUNNING, calls `on_trial_complete` for scheduler
        and search_alg if RUNNING. Caller to ensure that there is no
        outstanding future to be handled for the trial. If there is, the future
        would be discarded.
        """
        try:
            if trial.status in [Trial.ERROR, Trial.TERMINATED]:
                return
            elif trial.status in [Trial.PENDING, Trial.PAUSED]:
                self._scheduler_alg.on_trial_remove(self, trial)
                self._search_alg.on_trial_complete(trial.trial_id)
            elif trial.status is Trial.RUNNING:
                # By this time trial.last_result should have been
                # updated already.
                self._scheduler_alg.on_trial_complete(
                    self, trial, flatten_dict(trial.last_result)
                )
                self._search_alg.on_trial_complete(
                    trial.trial_id, result=flatten_dict(trial.last_result)
                )
            self._callbacks.on_trial_complete(
                iteration=self._iteration, trials=self._trials, trial=trial
            )
            self._schedule_graceful_trial_stop(trial)
            self._live_trials.discard(trial)
        except Exception as e:
            logger.exception("Trial %s: Error stopping trial.", trial)
            if self._fail_fast == self.RAISE:
                raise
            if isinstance(e, TuneError):
                self._process_trial_failure(trial, exception=e)
            else:
                self._process_trial_failure(
                    trial, _TuneStopTrialError(traceback.format_exc())
                )

    def _schedule_graceful_trial_stop(self, trial: Trial):
        raise NotImplementedError

    ###
    # TRAIN

    def _schedule_trial_train(self, trial: Trial):
        raise NotImplementedError

    def _on_training_result(self, trial, result):
        if not isinstance(result, list):
            result = [result]
        with warn_if_slow("process_trial_result"):
            self._process_trial_results(trial, result)
        self._maybe_execute_queued_decision(trial)

    def _process_trial_results(self, trial, results):
        logger.debug(f"Processing trial results for trial {trial}: {results}")
        with warn_if_slow(
            "process_trial_results",
            message="Processing trial results took {duration:.3f} s, "
            "which may be a performance bottleneck. Please consider "
            "reporting results less frequently to Ray Tune.",
        ):
            for i, result in enumerate(results):
                with warn_if_slow("process_trial_result"):
                    decision = self._process_trial_result(trial, result)
                if decision is None:
                    # If we didn't get a decision, this means a
                    # non-training future (e.g. a save) was scheduled.
                    # We do not allow processing more results then.
                    if i < len(results) - 1:
                        if log_once("trial_runner_buffer_checkpoint"):
                            logger.warning(
                                f"Trial {trial} has a non-training future "
                                f"scheduled but {len(results) - i} results "
                                f"left to process. This means that a "
                                f"checkpoint was requested, but buffered "
                                f"training was continued before it was "
                                f"saved. Consider using non-buffered "
                                f"training by setting the env variable "
                                f"`TUNE_RESULT_BUFFER_LENGTH=1`."
                            )
                elif decision == TrialScheduler.STOP:
                    # If the decision is to stop the trial,
                    # ignore all results that came after that.
                    break

    def _process_trial_result(self, trial, result):
        result.update(trial_id=trial.trial_id)
        is_duplicate = RESULT_DUPLICATE in result
        force_checkpoint = result.get(SHOULD_CHECKPOINT, False)
        # TrialScheduler and SearchAlgorithm still receive a
        # notification because there may be special handling for
        # the `on_trial_complete` hook.
        if is_duplicate:
            logger.debug("Trial finished without logging 'done'.")
            result = trial.last_result
            result.update(done=True)

        self._total_time += result.get(TIME_THIS_ITER_S, 0)

        flat_result = flatten_dict(result)
        self._validate_result_metrics(flat_result)

        if self._stopper(trial.trial_id, result) or trial.should_stop(flat_result):
            decision = TrialScheduler.STOP
        else:
            with warn_if_slow("scheduler.on_trial_result"):
                decision = self._scheduler_alg.on_trial_result(
                    self._wrapped(), trial, flat_result
                )
        if decision == TrialScheduler.STOP:
            result.update(done=True)
        else:
            # Only updating search alg if the trial is not to be stopped.
            with warn_if_slow("search_alg.on_trial_result"):
                self._search_alg.on_trial_result(trial.trial_id, flat_result)

        # If this is not a duplicate result, the callbacks should
        # be informed about the result.
        if not is_duplicate:
            with warn_if_slow("callbacks.on_trial_result"):
                self._callbacks.on_trial_result(
                    iteration=self._iteration,
                    trials=self._trials,
                    trial=trial,
                    result=result.copy(),
                )
            trial.update_last_result(result)
            # Include in next experiment checkpoint
            self._mark_trial_to_checkpoint(trial)

        # Checkpoints to disk. This should be checked even if
        # the scheduler decision is STOP or PAUSE. Note that
        # PAUSE only checkpoints to memory and does not update
        # the global checkpoint state.
        self._checkpoint_trial_if_needed(trial, force=force_checkpoint)

        if trial.is_saving:
            logger.debug(f"Caching trial decision for trial {trial}: {decision}")
            # Cache decision to execute on after the save is processed.
            # This prevents changing the trial's state or kicking off
            # another training step prematurely.
            self._cached_trial_decisions[trial.trial_id] = decision
            return None
        else:
            self._queue_decision(trial, decision)
            return decision

    def _validate_result_metrics(self, result):
        """
        Check if any of the required metrics was not reported
        in the last result. If the only items are ``done`` or any of
        DEBUG_METRICS, this means that no result was ever received and
        the trial just returned. This is also okay and will not raise
        an error.

        This will ignore checking for the DEFAULT_METRIC.
        """
        if int(os.environ.get("TUNE_DISABLE_STRICT_METRIC_CHECKING", 0)) != 1 and (
            len({k for k in result if k not in list(DEBUG_METRICS) + [DONE]}) > 1
        ):
            base_metric = self._metric if self._metric != DEFAULT_METRIC else None
            scheduler_metric = (
                self._scheduler_alg.metric
                if self._scheduler_alg.metric != DEFAULT_METRIC
                else None
            )
            search_metrics = (
                self._search_alg.metric
                if self._search_alg.metric != DEFAULT_METRIC
                else None
            )

            if isinstance(search_metrics, str):
                search_metrics = [search_metrics]

            if base_metric and base_metric not in result:
                report_metric = base_metric
                location = "tune.TuneConfig()"
            elif scheduler_metric and scheduler_metric not in result:
                report_metric = scheduler_metric
                location = type(self._scheduler_alg).__name__
            elif search_metrics and any(
                search_metric not in result for search_metric in search_metrics
            ):
                report_metric = list(
                    filter(
                        lambda search_metric: search_metric not in result,
                        search_metrics,
                    )
                )
                if len(report_metric) == 1:
                    report_metric = report_metric[0]
                location = type(self._search_alg).__name__
            else:
                report_metric = None
                location = None

            if report_metric:
                raise ValueError(
                    "Trial returned a result which did not include the "
                    "specified metric(s) `{}` that `{}` expects. "
                    "Make sure your calls to `tune.report()` include the "
                    "metric, or set the "
                    "TUNE_DISABLE_STRICT_METRIC_CHECKING "
                    "environment variable to 1. Result: {}".format(
                        report_metric, location, result
                    )
                )

    ###
    # SAVE

    def _schedule_trial_save(
        self,
        trial: Trial,
        storage: CheckpointStorage = CheckpointStorage.PERSISTENT,
        result: Optional[Dict] = None,
    ) -> Optional[_TrackedCheckpoint]:
        raise NotImplementedError

    def _on_saving_result(self, trial, checkpoint_value: Union[ray.ObjectRef, str]):
        with warn_if_slow("process_trial_save") as _profile:
            self._process_trial_save(trial, checkpoint_value)
        with warn_if_slow("callbacks.on_trial_save"):
            self._callbacks.on_trial_save(
                iteration=self._iteration, trials=self._trials, trial=trial
            )
        if _profile.too_slow and trial.sync_on_checkpoint:
            # TODO(ujvl): Suggest using cloud checkpointing once
            #  API has converged.

            msg = (
                "Consider turning off forced head-worker trial "
                "checkpoint syncs by setting sync_on_checkpoint=False"
                ". Note that this may result in faulty trial "
                "restoration if a failure occurs while the checkpoint "
                "is being synced from the worker to the head node."
            )

            if trial.location.hostname and (
                trial.location.hostname != get_node_ip_address()
            ):
                if log_once("tune_head_worker_checkpoint"):
                    logger.warning(msg)

        self._maybe_execute_queued_decision(trial)

    def _process_trial_save(
        self, trial: Trial, checkpoint_value: Union[ray.ObjectRef, str]
    ):
        """Processes a trial save.

        Acts on the decision cached during the last `_process_trial` call.

        Args:
            trial: Trial being saved.
        """
        logger.debug("Trial %s: Processing trial save.", trial)

        try:
            trial.saving_to.dir_or_data = checkpoint_value
            self._callbacks.on_checkpoint(
                iteration=self._iteration,
                trials=self._trials,
                trial=trial,
                checkpoint=trial.saving_to,
            )
            trial.on_checkpoint(trial.saving_to)
            self._checkpoint_manager.on_trial_checkpoint(trial)
            if trial.checkpoint.storage_mode != CheckpointStorage.MEMORY:
                self._mark_trial_to_checkpoint(trial)
        except Exception:
            logger.exception(
                "Trial %s: Error handling checkpoint %s", trial, checkpoint_value
            )
            if self._fail_fast == TrialRunner.RAISE:
                raise

        trial.saving_to = None
        decision = self._cached_trial_decisions.pop(trial.trial_id, None)
        if decision and checkpoint_value:
            self._queue_decision(trial, decision)

    ###
    # RESTORE

    def _schedule_trial_restore(self, trial: Trial):
        raise NotImplementedError

    def _on_restoring_result(self, trial):
        with warn_if_slow("process_trial_restore"):
            self._process_trial_restore(trial)
        with warn_if_slow("callbacks.on_trial_restore"):
            self._callbacks.on_trial_restore(
                iteration=self._iteration, trials=self._trials, trial=trial
            )

    def _process_trial_restore(self, trial: Trial):
        """Processes a trial restore.

        Args:
            trial: Trial being restored.
        """
        logger.debug("Trial %s: Processing trial restore.", trial)
        trial.on_restore()
        logger.debug("Trial %s: Restore processed successfully", trial)
        self._set_trial_status(trial, Trial.RUNNING)
        self._schedule_trial_train(trial)
        self._live_trials.add(trial)

    ###
    # EXPORT
    def _schedule_trial_export(self, trial: Trial):
        raise NotImplementedError

    def _queue_decision(self, trial, decision):
        # Get old decision, setting it to the current decision if it isn't set
        old_decision = self._queued_trial_decisions.setdefault(trial.trial_id, decision)

        # Stopping always takes precedence. If we decided to stop, just quit
        if old_decision is TrialScheduler.STOP:
            return

        # The old decision wasn't STOP. We update the decision only if it is
        # STOP or PAUSE. The action will only be CONTINUE if it was set by
        # the first received result and was never updated after that.
        if decision is TrialScheduler.STOP or decision is TrialScheduler.PAUSE:
            self._queued_trial_decisions[trial.trial_id] = decision

    def _execute_action(self, trial: Trial, decision: str):
        """Executes action based on decision.

        Args:
            trial: Trial to act on.
            decision: Scheduling decision to undertake.
        """
        if decision == TrialScheduler.CONTINUE:
            self._schedule_trial_train(trial)
        elif decision == TrialScheduler.PAUSE:
            self.pause_trial(trial)
        elif decision == TrialScheduler.STOP:
            self.stop_trial(trial)
        elif decision == TrialScheduler.NOOP:
            pass
        else:
            raise ValueError("Invalid decision: {}".format(decision))

    def _checkpoint_trial_if_needed(self, trial, force=False):
        """Checkpoints trial based off trial.last_result."""
        if trial.should_checkpoint() or force:
            # Save trial runtime if possible.
            if trial.runner:
                self._schedule_trial_save(trial, storage=CheckpointStorage.PERSISTENT)

    def _try_recover(self, trial: Trial, exc: Union[TuneError, RayTaskError]):
        """Tries to recover trial.

        Notifies SearchAlgorithm and Scheduler if failure to recover.

        Args:
            trial: Trial to recover.
            exc: Exception prior to invoking this method.
        """
        self._cached_trial_decisions.pop(trial.trial_id, None)
        # Resetting this, in case that the trial is in saving status when it crashes.
        if trial.is_saving:
            trial.saving_to = None
        if trial.is_restoring and exc:
            exc = _TuneRestoreError(exc)
        self._schedule_trial_stop(trial, exception=exc)

        logger.debug("Trial %s: Notifying Scheduler and requeueing.", trial)
        self._requeue_trial(trial)

    def _requeue_trial(self, trial):
        """Notification to TrialScheduler and requeue trial.

        This does not notify the SearchAlgorithm because the function
        evaluation is still in progress.

        """
        self._scheduler_alg.on_trial_error(self, trial)
        self._set_trial_status(trial, status=Trial.PENDING)

        # TODO(rliaw): Right now, this pushes the trial to the end of queue
        # because restoration can be expensive. However, this is not
        # ideal since it just hides the issue - a better fix would
        # be to use an actor table to detect the IP of the Trainable
        # and rsync the files there.
        # See https://github.com/ray-project/ray/issues/5168
        self._trials.pop(self._trials.index(trial))
        self._trials.append(trial)
        self._live_trials.add(trial)

        with warn_if_slow("scheduler.on_trial_add"):
            self._scheduler_alg.on_trial_add(self._wrapped(), trial)

    def _update_trial_queue(self, blocking: bool = False, timeout: int = 600) -> bool:
        """Adds next trials to queue if possible.

        Note that the timeout is currently unexposed to the user.

        Args:
            blocking: Blocks until either a trial is available
                or is_finished (timeout or search algorithm finishes).
            timeout: Seconds before blocking times out.

        Returns:
            Boolean indicating if a new trial was created or not.
        """
        trial = self._search_alg.next_trial()
        if blocking and not trial:
            start = time.time()
            # Checking `is_finished` instead of _search_alg.is_finished
            # is fine because blocking only occurs if all trials are
            # finished and search_algorithm is not yet finished
            while (
                not trial and not self.is_finished() and time.time() - start < timeout
            ):
                logger.debug("Blocking for next trial...")
                trial = self._search_alg.next_trial()
                time.sleep(1)

        if trial:
            self.add_trial(trial)
            return True

        return False

    def request_stop_trial(self, trial):
        self._stop_queue.append(trial)

    def request_stop_experiment(self):
        self._should_stop_experiment = True

    def _process_stop_requests(self):
        while self._stop_queue:
            t = self._stop_queue.pop()
            self.stop_trial(t)

    def pause_trial(self, trial: Trial, should_checkpoint: bool = True):
        """Pause a trial and reset the necessary state variables for resuming later.

        Args:
            trial: Trial to pause.
            should_checkpoint: Whether or not an in-memory checkpoint should be created
                for this paused trial. Defaults to True.
        """
        # NOTE: The cached trial decision is not needed since we will overrule this
        # decision with PAUSE.
        self._cached_trial_decisions.pop(trial.trial_id, None)
        self._schedule_trial_pause(trial)

    def cleanup(self):
        """Cleanup trials and callbacks."""
        self._cleanup_trials()
        self.end_experiment_callbacks()

    def __getstate__(self):
        """Gets state for trial.

        Note that this is not used as a pickling override as
        does not have all fields.
        """
        state = self.__dict__.copy()
        for k in [
            "_trials",
            "_live_trials",
            "_stop_queue",
            "_server",
            "_search_alg",
            "_placeholder_resolvers",
            "_scheduler_alg",
            "_pending_trial_queue_times",
            "_callbacks",
            "_checkpoint_manager",
            "_local_experiment_path",
            "_remote_experiment_path",
            "_sync_config",
            "_experiment_dir_name",
            "_insufficient_resources_manager",
        ]:
            del state[k]
        state["launch_web_server"] = bool(self._server)
        return state

    def __setstate__(self, state):
        launch_web_server = state.pop("launch_web_server")

        # Use session_str from previous checkpoint if does not exist
        session_str = state.pop("_session_str")
        self.__dict__.setdefault("_session_str", session_str)
        # Use start_time from previous checkpoint if does not exist
        start_time = state.pop("_start_time")
        self.__dict__.setdefault("_start_time", start_time)

        self.__dict__.update(state)
        self._checkpoint_manager = self._create_checkpoint_manager()

        if launch_web_server:
            self._server = TuneServer(self, self._server_port)


@DeveloperAPI
class TrialRunner(_TuneControllerBase):
    """A TrialRunner implements the event loop for scheduling trials on Ray.

    .. code-block: python

        runner = TrialRunner()
        runner.add_trial(Trial(...))
        runner.add_trial(Trial(...))
        while not runner.is_finished():
            runner.step()
            print(runner.debug_string())

    The main job of TrialRunner is scheduling trials to efficiently use cluster
    resources, without overloading the cluster.

    While Ray itself provides resource management for tasks and actors, this is
    not sufficient when scheduling trials that may instantiate multiple actors.
    This is because if insufficient resources are available, concurrent trials
    could deadlock waiting for new resources to become available. Furthermore,
    oversubscribing the cluster could degrade training performance, leading to
    misleading benchmark results.

    Args:
        search_alg: SearchAlgorithm for generating
            Trial objects.
        scheduler: Defaults to FIFOScheduler.
        experiment_path: Path where global experiment state checkpoints
            are saved and restored from.
        experiment_dir_name: Experiment directory name.
            See :class:`~ray.tune.experiment.Experiment`.
        sync_config: See :class:`~ray.tune.syncer.SyncConfig`.
            Within sync config, the `upload_dir` specifies cloud storage, and
            experiment state checkpoints will be synced to the `remote_checkpoint_dir`:
            `{sync_config.upload_dir}/{experiment_name}`.
        stopper: Custom class for stopping whole experiments. See ``Stopper``.
        resume: see `tune.py:run`.
        server_port: Port number for launching TuneServer.
        fail_fast: Finishes as soon as a trial fails if True.
            If fail_fast='raise' provided, Tune will automatically
            raise the exception received by the Trainable. fail_fast='raise'
            can easily leak resources and should be used with caution.
        checkpoint_period: Trial runner checkpoint periodicity in
            seconds. Defaults to ``"auto"``, which adjusts checkpointing
            time so that at most 5% of the time is spent on writing
            checkpoints.
        trial_executor: Defaults to RayTrialExecutor.
        callbacks: List of callbacks that will be called at different
            times in the training loop. Must be instances of the
            ``ray.tune.execution.trial_runner.Callback`` class.
        metric: Metric used to check received results. If a result is
            reported without this metric, an error will be raised. The error
            can be omitted by not providing a metric or by setting the env
            variable ``TUNE_DISABLE_STRICT_METRIC_CHECKING=0``

    """

    def __init__(
        self,
        *,
        search_alg: Optional[SearchAlgorithm] = None,
        placeholder_resolvers: Optional[Dict[Tuple, Any]] = None,
        scheduler: Optional[TrialScheduler] = None,
        experiment_path: Optional[str] = None,
        experiment_dir_name: Optional[str] = None,
        sync_config: Optional[SyncConfig] = None,
        stopper: Optional[Stopper] = None,
        resume: Union[str, bool] = False,
        server_port: Optional[int] = None,
        fail_fast: bool = False,
        trial_executor: Optional[RayTrialExecutor] = None,
        checkpoint_period: Union[str, int] = None,
        callbacks: Optional[List[Callback]] = None,
        metric: Optional[str] = None,
        trial_checkpoint_config: Optional[CheckpointConfig] = None,
        # Deprecated
        local_checkpoint_dir: Optional[str] = None,
    ):

        if local_checkpoint_dir:
            if experiment_path:
                raise ValueError(
                    "Only one of `local_checkpoint_dir` or `experiment_path` "
                    "can be passed to `TrialRunner()`."
                )

            warnings.warn(
                "The `local_checkpoint_dir` argument is deprecated and will be "
                "removed in the future. Use `experiment_path` instead."
            )

            experiment_path = local_checkpoint_dir

        self.trial_executor = trial_executor or RayTrialExecutor()

        super().__init__(
            search_alg=search_alg,
            placeholder_resolvers=placeholder_resolvers,
            scheduler=scheduler,
            experiment_path=experiment_path,
            experiment_dir_name=experiment_dir_name,
            sync_config=sync_config,
            stopper=stopper,
            resume=resume,
            server_port=server_port,
            fail_fast=fail_fast,
            checkpoint_period=checkpoint_period,
            callbacks=callbacks,
            metric=metric,
            trial_checkpoint_config=trial_checkpoint_config,
        )

        self.trial_executor.setup(
            max_pending_trials=self._max_pending_trials,
            # TODO(ml-team): Remove these in 2.6.
            trainable_kwargs={
                "sync_timeout": self._sync_config.sync_timeout,
                "custom_syncer": get_node_to_storage_syncer(
                    self._sync_config, self._remote_experiment_path
                ),
            },
        )

    def _wrapped(self):
        return TrialRunnerWrapper(
            self,
            self.trial_executor,
            runner_whitelist_attr={"search_alg", "get_trials", "_set_trial_status"},
            executor_whitelist_attr={"has_resources_for_trial", "pause_trial", "save"},
        )

    def _used_resources_string(self) -> str:
        return self.trial_executor.debug_string()

    def _get_trial_checkpoints(self) -> Dict[str, str]:
        return self.trial_executor.get_checkpoints()

    def _mark_trial_to_checkpoint(self, trial: Trial):
        self.trial_executor.mark_trial_to_checkpoint(trial)

    def _set_trial_status(self, trial: Trial, status: str):
        self.trial_executor.set_status(trial, status=status)

    def _reconcile_live_trials(self):
        """Loop through live trials and remove if terminated"""
        for trial in list(self._live_trials):
            # Only for TERMINATED trials. ERRORed trials might be retried.
            if trial.status == Trial.TERMINATED:
                self._live_trials.remove(trial)

    def _cleanup_trials(self):
        self.trial_executor.cleanup()

    def _update_trial_queue_and_get_next_trial(self) -> Optional[Trial]:
        """Adding suggested trials to the live queue of trials (they start as PENDING trials).

        Returns:
            next_trial: Trial
        """
        wait_for_trial = True  # wait for new trials when all trials are finished
        num_pending_trials = 0
        for trial in self._live_trials:
            if not trial.is_finished():
                wait_for_trial = False
                if trial.status == Trial.PENDING:
                    num_pending_trials += 1

        if not self._search_alg.is_finished():
            # Create pending trials until it fails.
            while num_pending_trials < self._max_pending_trials:
                if not self._update_trial_queue(blocking=wait_for_trial):
                    break
                wait_for_trial = False  # wait at most one trial
                num_pending_trials += 1

        with warn_if_slow("choose_trial_to_run"):
            return self._scheduler_alg.choose_trial_to_run(self._wrapped())

    def step(self):
        """Runs one step of the trial event loop.

        Callers should typically run this method repeatedly in a loop. They
        may inspect or modify the runner's state in between calls to step().
        """
        if self.is_finished():
            raise TuneError("Called step when all trials finished?")
        with warn_if_slow("on_step_begin"):
            self.trial_executor.on_step_begin()
        with warn_if_slow("callbacks.on_step_begin"):
            self._callbacks.on_step_begin(
                iteration=self._iteration, trials=self._trials
            )

        next_trial = self._update_trial_queue_and_get_next_trial()
        if next_trial:
            logger.debug(f"Got new trial to run: {next_trial}")

        self._wait_and_handle_event(next_trial)

        self._stop_experiment_if_needed()

        try:
            self.checkpoint()
        except Exception as e:
            logger.warning(f"Trial Runner checkpointing failed: {str(e)}")
        self._iteration += 1

        if self._server:
            with warn_if_slow("server"):
                self._process_stop_requests()

            if self.is_finished():
                self._server.shutdown()

        self._reconcile_live_trials()

        with warn_if_slow("on_step_end"):
            self.trial_executor.on_step_end(search_ended=self._search_alg.is_finished())
        with warn_if_slow("callbacks.on_step_end"):
            self._callbacks.on_step_end(iteration=self._iteration, trials=self._trials)

    def _wait_and_handle_event(self, next_trial: Optional[Trial]):
        try:
            # Single wait of entire tune loop.
            event = self.trial_executor.get_next_executor_event(
                self._live_trials, next_trial is not None
            )
            if event.type == _ExecutorEventType.PG_READY:
                self._on_pg_ready(next_trial)
            elif event.type == _ExecutorEventType.NO_RUNNING_TRIAL_TIMEOUT:
                self._insufficient_resources_manager.on_no_available_trials(
                    self.get_trials()
                )
            elif event.type == _ExecutorEventType.YIELD:
                pass
            else:
                assert event.type in (
                    _ExecutorEventType.TRAINING_RESULT,
                    _ExecutorEventType.SAVING_RESULT,
                    _ExecutorEventType.RESTORING_RESULT,
                )
                trial = event.trial
                result = event.result
                if _ExecutorEvent.KEY_EXCEPTION in result:
                    self._on_executor_error(
                        trial, event.type, result[_ExecutorEvent.KEY_EXCEPTION]
                    )
                elif event.type == _ExecutorEventType.RESTORING_RESULT:
                    self._on_restoring_result(trial)
                else:
                    assert event.type in (
                        _ExecutorEventType.SAVING_RESULT,
                        _ExecutorEventType.TRAINING_RESULT,
                    ), f"Unexpected future type - {event.type}"
                    if event.type == _ExecutorEventType.TRAINING_RESULT:
                        self._on_training_result(
                            trial, result[_ExecutorEvent.KEY_FUTURE_RESULT]
                        )
                    else:
                        self._on_saving_result(
                            trial, result[_ExecutorEvent.KEY_FUTURE_RESULT]
                        )
        except Exception as e:
            if e is TuneError or self._fail_fast == self.RAISE:
                raise e
            else:
                raise TuneError(traceback.format_exc())

    def _on_pg_ready(self, next_trial: Optional[Trial]):
        def _start_trial(trial: Trial) -> bool:
            """Helper function to start trial and call callbacks"""
            with warn_if_slow("start_trial"):
                if self.trial_executor.start_trial(trial):
                    self._callbacks.on_trial_start(
                        iteration=self._iteration, trials=self._trials, trial=trial
                    )
                    return True
                return False

        assert next_trial is not None
        logger.debug(f"Trying to start trial: {next_trial}")

        trial_started = _start_trial(next_trial)
        if not trial_started and next_trial.status != Trial.ERROR:
            # Only try to start another trial if previous trial startup
            # did not error (e.g. it just didn't start because its
            # placement group is not ready, yet).
            # Without this clause, this test fails:
            # test_trial_runner_pg.py::
            # TrialRunnerPlacementGroupHeterogeneousTest::
            # testResourceDeadlock
            next_trial = self.trial_executor.get_ready_trial()

            if next_trial is not None:
                # Must be able to start.
                assert _start_trial(next_trial)

    def _on_executor_error(
        self, trial, event_type: _ExecutorEventType, e: Union[RayTaskError, TuneError]
    ):
        error_msg = f"Trial {trial}: Error happened when processing {str(event_type)}."
        if self._fail_fast == self.RAISE:
            raise e
        else:
            if self._print_trial_errors:
                logger.error(error_msg, exc_info=e)
            self._process_trial_failure(trial, exception=e)

    def _schedule_trial_stop(self, trial: Trial, exception: Optional[Exception] = None):
        return self.trial_executor.stop_trial(
            trial, error=bool(exception), exc=exception
        )

    def _schedule_graceful_trial_stop(self, trial: Trial):
        self._schedule_trial_export(trial)
        self._schedule_trial_stop(trial)

    def _schedule_trial_pause(self, trial: Trial, should_checkpoint: bool = True):
        self.trial_executor.pause_trial(trial, should_checkpoint=should_checkpoint)

    def _schedule_trial_train(self, trial: Trial):
        self.trial_executor.continue_training(trial)

    def _schedule_trial_save(
        self,
        trial: Trial,
        storage: CheckpointStorage = CheckpointStorage.PERSISTENT,
        result: Optional[Dict] = None,
    ) -> Optional[_TrackedCheckpoint]:
        return self.trial_executor.save(trial, storage=storage, result=result)

    def _schedule_trial_export(self, trial: Trial):
        return self.trial_executor.export_trial_if_needed(trial)

    def __getstate__(self):
        state = super().__getstate__()
        state.pop("trial_executor")
        return state


class _TrialExecutorWrapper:
    """Wraps around TrialExecutor class, intercepts API calls and warns users
    of restricted API access.

    This is meant to facilitate restricting
    the current API exposure of TrialExecutor by TrialScheduler.
    """

    def __init__(
        self, trial_executor: RayTrialExecutor, whitelist_attr: Optional[set] = None
    ):
        self._trial_executor = trial_executor
        self._whitelist_attr = whitelist_attr or set()

    def __getattr__(self, attr):
        if attr not in self._whitelist_attr:
            if log_once("restrict_accessing_trial_executor"):
                logger.warning(
                    f"You are trying to access {attr} interface of "
                    f"TrialExecutor in TrialScheduler, which is being "
                    f"restricted. If you believe it is reasonable for "
                    f"your scheduler to access this TrialExecutor API, "
                    f"please reach out to Ray team on GitHub. A more "
                    f"strict API access pattern would be enforced "
                    f"starting 1.12.0"
                )
        return getattr(self._trial_executor, attr)


@DeveloperAPI
class TrialRunnerWrapper:
    """Wraps around TrialRunner class, intercepts API calls and warns users
    of restricted API access.

    This is meant to facilitate restricting
    the current API exposure of TrialRunner by TrialScheduler.
    """

    _EXECUTOR_ATTR = "trial_executor"

    def __init__(
        self,
        trial_runner: TrialRunner,
        trial_executor: Any,
        runner_whitelist_attr: Optional[set] = None,
        executor_whitelist_attr: Optional[set] = None,
    ):
        self._trial_runner = trial_runner
        self._trial_executor = _TrialExecutorWrapper(
            trial_executor, executor_whitelist_attr
        )
        self._runner_whitelist_attr = runner_whitelist_attr or set()

    def __getattr__(self, attr):
        if attr == self._EXECUTOR_ATTR:
            return self._trial_executor
        if attr not in self._runner_whitelist_attr:
            if log_once("restrict_accessing_trial_runner"):
                logger.warning(
                    f"You are trying to access {attr} interface of "
                    f"TrialRunner in TrialScheduler, which is being "
                    f"restricted. If you believe it is reasonable for "
                    f"your scheduler to access this TrialRunner API, "
                    f"please reach out to Ray team on GitHub. A more "
                    f"strict API access pattern would be enforced "
                    f"starting 1.12s.0"
                )
        return getattr(self._trial_runner, attr)


def _get_max_pending_trials(search_alg: SearchAlgorithm) -> int:
    max_pending_trials = os.getenv("TUNE_MAX_PENDING_TRIALS_PG", "auto")

    if max_pending_trials != "auto":
        return int(max_pending_trials)

    # Else, auto detect.

    # Only BasicVariantGenerator supports > 1 pending trials.
    # This is because we don't want to generate too many trials
    # before we fit the searcher model.
    if not isinstance(search_alg, BasicVariantGenerator):
        return 1

    # Use a minimum of 16 to trigger fast autoscaling
    # Scale up to at most the number of available cluster CPUs
    cluster_cpus = ray.cluster_resources().get("CPU", 1.0)
    max_pending_trials = max(16, int(cluster_cpus * 1.1))

    if max_pending_trials > 128:
        logger.warning(
            f"The maximum number of pending trials has been "
            f"automatically set to the number of available "
            f"cluster CPUs, which is high "
            f"({max_pending_trials} CPUs/pending trials). "
            f"If you're running an experiment with a large number "
            f"of trials, this could lead to scheduling overhead. "
            f"In this case, consider setting the "
            f"`TUNE_MAX_PENDING_TRIALS_PG` environment variable "
            f"to the desired maximum number of concurrent trials."
        )

    return max_pending_trials

import threading
from typing import Any, List, Optional

import ray
from ray.experimental import tqdm_ray
from ray.types import ObjectRef
from ray.util.annotations import PublicAPI

try:
    import tqdm

    needs_warning = False
except ImportError:
    tqdm = None
    needs_warning = True

# Used a signal to cancel execution.
_canceled_threads = set()
_canceled_threads_lock = threading.Lock()


@PublicAPI
def set_progress_bars(enabled: bool) -> bool:
    """Set whether progress bars are enabled.

    The default behavior is controlled by the
    ``RAY_DATA_DISABLE_PROGRESS_BARS`` environment variable. By default,
    it is set to "0". Setting it to "1" will disable progress bars, unless
    they are reenabled by this method.

    Returns:
        Whether progress bars were previously enabled.
    """
    from ray.data import DataContext

    ctx = DataContext.get_current()
    old_value = ctx.enable_progress_bars
    ctx.enable_progress_bars = enabled
    return old_value


class ProgressBar:
    """Thin wrapper around tqdm to handle soft imports."""

    def __init__(
        self, name: str, total: int, position: int = 0, enabled: Optional[bool] = None
    ):
        self._desc = name
        if enabled is None:
            from ray.data import DataContext

            enabled = DataContext.get_current().enable_progress_bars
        if not enabled:
            self._bar = None
        elif tqdm:
            ctx = ray.data.context.DataContext.get_current()
            if ctx.use_ray_tqdm:
                self._bar = tqdm_ray.tqdm(total=total, position=position)
            else:
                self._bar = tqdm.tqdm(total=total, position=position)
            self._bar.set_description(self._desc)
        else:
            global needs_warning
            if needs_warning:
                print(
                    "[datastream]: Run `pip install tqdm` to enable progress reporting."
                )
                needs_warning = False
            self._bar = None

    def block_until_complete(self, remaining: List[ObjectRef]) -> None:
        t = threading.current_thread()
        while remaining:
            done, remaining = ray.wait(remaining, fetch_local=False, timeout=0.1)
            self.update(len(done))

            with _canceled_threads_lock:
                if t in _canceled_threads:
                    break

    def fetch_until_complete(self, refs: List[ObjectRef]) -> List[Any]:
        ref_to_result = {}
        remaining = refs
        t = threading.current_thread()
        while remaining:
            done, remaining = ray.wait(remaining, fetch_local=True, timeout=0.1)
            for ref, result in zip(done, ray.get(done)):
                ref_to_result[ref] = result
            self.update(len(done))

            with _canceled_threads_lock:
                if t in _canceled_threads:
                    break

        return [ref_to_result[ref] for ref in refs]

    def set_description(self, name: str) -> None:
        if self._bar and name != self._desc:
            self._desc = name
            self._bar.set_description(self._desc)

    def update(self, i: int) -> None:
        if self._bar and i != 0:
            self._bar.update(i)

    def close(self):
        if self._bar:
            self._bar.close()
            self._bar = None

    def __del__(self):
        self.close()

    def __getstate__(self):
        return {}

    def __setstate__(self, state):
        self._bar = None  # Progress bar is disabled on remote nodes.

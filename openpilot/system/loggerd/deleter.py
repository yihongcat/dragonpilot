#!/usr/bin/env python3
import os
import shutil
import threading
import time
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr
from openpilot.system.loggerd.recordings import (
  RecordingPolicy, clear_stale_locks, recording_lock, retention_candidates, is_missing_or_busy,
  current_recording_route, open_recording_directories,
  delete_recording,
)

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def deleter_step(recover_locks: bool = True) -> tuple[bool, str | None]:
  try:
    with recording_lock(Paths.log_root()):
      return _deleter_step(recover_locks)
  except OSError as e:
    if not is_missing_or_busy(e):
      cloudlog.exception("issue managing recordings")
    return False, None


def _deleter_step(recover_locks: bool) -> tuple[bool, str | None]:
  params = Params()
  current_route = current_recording_route(params)
  # Recover abandoned locks even when uploader/comma Connect is disabled.
  if recover_locks:
    clear_stale_locks(Paths.log_root(), current_route)
  out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
  out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT
  out_of_space = out_of_percent or out_of_bytes
  dirs = listdir_by_creation(Paths.log_root())
  preserved_dirs = get_preserved_segments(dirs)
  retention_dirs = retention_candidates(Paths.log_root(), RecordingPolicy.load(params), current_route,
                                        preserved_dirs=preserved_dirs)
  if not out_of_space and not retention_dirs:
    return False, None
  active_dirs = open_recording_directories(Paths.log_root())
  if active_dirs is None:
    return out_of_space, None
  current_segments = [d for d in dirs if current_route and d.startswith(current_route + "--") and d.rsplit('--', 1)[1].isdigit()]
  latest_segment = max(current_segments, key=lambda d: int(d.rsplit('--', 1)[1]), default=None)

  # remove the earliest directory we can
  for delete_dir in sorted(dirs, key=lambda d: (d in DELETE_LAST, d in preserved_dirs)):
    delete_path = os.path.join(Paths.log_root(), delete_dir)
    if delete_dir in active_dirs or os.path.islink(delete_path) or delete_dir == latest_segment:
      continue
    # Emergency cleanup can reclaim closed segments of a long ongoing drive.
    # Keep the newest segment protected even before its recorder lock appears.
    if not out_of_space and current_route and delete_dir.startswith(current_route + "--"):
      continue
    if not out_of_space and delete_dir not in retention_dirs:
      continue

    try:
      if any(name.endswith(".lock") for name in os.listdir(delete_path)):
        continue
      cloudlog.info(f"deleting {delete_path}")
      if delete_dir in retention_dirs:
        # User-selected retention removes whole completed drives, so a quota
        # boundary cannot leave half a drive behind. Emergency cleanup keeps
        # upstream's segment-at-a-time behavior and preservation priority.
        delete_recording(Paths.log_root(), delete_dir.rsplit('--', 1)[0], current_route)
      else:
        shutil.rmtree(delete_path)
      return True, delete_path
    except OSError:
      cloudlog.exception(f"issue deleting {delete_path}")
  return True, None


def deleter_thread(exit_event: threading.Event):
  next_recovery = 0.
  while not exit_event.is_set():
    recover_locks = time.monotonic() >= next_recovery
    _, deleted_path = deleter_step(recover_locks=recover_locks)
    if recover_locks:
      next_recovery = time.monotonic() + 30
    # Do not spin ten times a second when every remaining directory is locked.
    exit_event.wait(.1 if deleted_path else 30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()

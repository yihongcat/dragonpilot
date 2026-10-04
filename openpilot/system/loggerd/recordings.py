"""Local recording inventory, retention policy, and abandoned recorder locks.

The uploader is optional. Lock recovery must also work on devices without
comma Connect, without touching a live recorder or an in-progress export.
"""
import errno
import fcntl
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

GIB = 1024 ** 3
LOCK_GRACE_SECONDS = 300
POLICY_PARAM = "dp_recording_policy"
SEGMENT_RE = re.compile(r"^(?P<route>(?:[0-9a-f]{8}--[0-9a-f]{10}|\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2}))--(?P<segment>\d+)$")
VIDEO_FILES = {"fcamera.hevc", "ecamera.hevc", "dcamera.hevc", "qcamera.ts"}


def current_recording_route(params) -> str:
  # CurrentRoute survives the offroad transition. It must not permanently
  # prevent exporting the most recently completed drive while parked.
  if params.get_bool("IsOffroad"):
    return ""
  route = params.get("CurrentRoute") or ""
  return route.decode() if isinstance(route, bytes) else route


@dataclass(frozen=True)
class RecordingPolicy:
  enabled: bool = False
  keep_days: int = 7
  max_gb: int = 20

  @classmethod
  def parse(cls, value):
    if not isinstance(value, dict) or set(value) != {"enabled", "keep_days", "max_gb"}:
      raise ValueError("Policy requires enabled, keep_days and max_gb")
    if type(value["enabled"]) is not bool:
      raise ValueError("enabled must be a boolean")
    for key, maximum in (("keep_days", 365), ("max_gb", 1024)):
      if type(value[key]) is not int or not 0 <= value[key] <= maximum:
        raise ValueError(f"{key} must be an integer between 0 and {maximum}")
    return cls(**value)

  @classmethod
  def load(cls, params):
    try:
      raw = params.get(POLICY_PARAM)
      return cls.parse(raw if isinstance(raw, dict) else json.loads(raw)) if raw else cls()
    except (ValueError, TypeError):
      return cls()

  def save(self, params):
    params.put(POLICY_PARAM, asdict(self), block=True)


@contextmanager
def recording_lock(root: str):
  """Serialize deletion and export setup across serverd and deleter.

  Keep the lock outside the segment tree, where native loggerd cannot rotate it.
  Never wait for another client in the always-running deleter.
  """
  path = Path(root).resolve().parent / ".recordings-management.lock"
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
      yield
    finally:
      fcntl.flock(lock, fcntl.LOCK_UN)


def segment_directories(root: str) -> list[Path]:
  base = Path(root).resolve()
  if not base.is_dir():
    return []
  return sorted(p for p in base.iterdir() if SEGMENT_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink())


def segment_files(path: Path) -> list[Path]:
  # Native segments contain only flat, regular files. Never follow symlinks.
  entries = list(path.iterdir())
  if any(p.is_symlink() or not p.is_file() for p in entries):
    raise ValueError("Recording contains unexpected files")
  return entries


def open_recording_directories(root: str, proc_root: str = "/proc") -> set[str] | None:
  """Find held video/log/export descriptors; fail closed if inspection fails."""
  base = Path(root).resolve()
  active = set()
  try:
    processes = list(Path(proc_root).iterdir())
  except OSError:
    return None
  for proc in processes:
    if not proc.name.isdigit():
      continue
    try:
      # Only our user's processes can write these comma-owned recordings.
      if proc.stat().st_uid != os.getuid():
        continue
      descriptors = list((proc / "fd").iterdir())
      for fd in descriptors:
        try:
          target = Path(os.readlink(fd).removesuffix(" (deleted)"))
          if target.is_absolute() and target.parent.parent == base:
            active.add(target.parent.name)
        except FileNotFoundError:
          continue
    except (FileNotFoundError, ProcessLookupError):
      continue
    except PermissionError:
      # User systemd and sd-pam deliberately hide their descriptors, but do
      # not record video. Unknown inaccessible processes still block recovery.
      try:
        if (proc / 'comm').read_text().strip() in ('systemd', '(sd-pam)'):
          continue
      except OSError:
        pass
      return None
    except OSError:
      return None
  return active


def clear_stale_locks(root: str, current_route: str = "", now: float | None = None) -> int:
  now = time.time() if now is None else now  # noqa: TID251 -- compare filesystem wall-clock timestamps
  active = open_recording_directories(root)
  if active is None:
    return 0
  removed = 0
  for path in segment_directories(root):
    match = SEGMENT_RE.fullmatch(path.name)
    if path.name in active or match["route"] == current_route:
      continue
    try:
      files = segment_files(path)
      locks = [p for p in files if p.name.endswith(".lock")]
      if not locks or max([path.stat().st_mtime] + [p.stat().st_mtime for p in files]) > now - LOCK_GRACE_SECONDS:
        continue
      for lock in locks:
        lock.unlink(missing_ok=True)
        removed += 1
    except (OSError, ValueError):
      continue
  return removed


def recording_inventory(root: str, current_route: str = "") -> list[dict]:
  routes = {}
  active = open_recording_directories(root)
  for path in segment_directories(root):
    try:
      files = segment_files(path)
      match = SEGMENT_RE.fullmatch(path.name)
      route = match["route"]
      data = routes.setdefault(route, {"id": route, "bytes": 0, "mtime": 0, "segments": [], "locked": route == current_route})
      if active is None or path.name in active:
        data["locked"] = True
      videos = []
      for p in files:
        stat = p.stat()
        data["mtime"] = max(data["mtime"], stat.st_mtime)
        if p.name.endswith(".lock"):
          data["locked"] = True
          continue
        data["bytes"] += stat.st_size
        if p.name in VIDEO_FILES and stat.st_size:
          videos.append({"name": p.name, "path": f"{path.name}/{p.name}", "bytes": stat.st_size})
      data["segments"].append({"name": path.name, "number": int(match["segment"]), "videos": videos})
    except (FileNotFoundError, ValueError):
      continue
  for data in routes.values():
    data["segments"].sort(key=lambda s: s["number"])
  return sorted(routes.values(), key=lambda r: (r["mtime"], r["id"]), reverse=True)


def retention_candidates(root: str, policy: RecordingPolicy, current_route: str = "", now: float | None = None,
                         preserved_dirs: set[str] | None = None) -> set[str]:
  if not policy.enabled:
    return set()
  now = time.time() if now is None else now  # noqa: TID251 -- retention is expressed in calendar days
  routes = recording_inventory(root, current_route)
  total = sum(r["bytes"] for r in routes)
  candidates = set()
  for route in reversed(routes):
    expired = bool(policy.keep_days and route["mtime"] < now - policy.keep_days * 86400)
    over_quota = bool(policy.max_gb and total > policy.max_gb * GIB)
    preserved = any(s["name"] in (preserved_dirs or set()) for s in route["segments"])
    if not route["locked"] and not preserved and (expired or over_quota):
      candidates.update(s["name"] for s in route["segments"])
      total -= route["bytes"]
  return candidates


def delete_recording(root: str, route_id: str, current_route: str = "") -> int:
  routes = {r["id"]: r for r in recording_inventory(root, current_route)}
  if route_id not in routes:
    raise FileNotFoundError("Recording not found")
  route = routes[route_id]
  if route["locked"]:
    raise BlockingIOError("Recording is active or being exported")
  base = Path(root).resolve()
  for segment in route["segments"]:
    path = base / segment["name"]
    if path.resolve().parent != base or path.is_symlink():
      raise ValueError("Invalid recording path")
    if any(p.name.endswith(".lock") for p in segment_files(path)):
      raise BlockingIOError("Recording is active or being exported")
  for segment in route["segments"]:
    shutil.rmtree(base / segment["name"])
  return route["bytes"]


@contextmanager
def export_recording(root: str, route_id: str, current_route: str = ""):
  """Hold export lock descriptors for the entire streamed download."""
  locks = []
  try:
    with recording_lock(root):
      routes = {r["id"]: r for r in recording_inventory(root, current_route)}
      if route_id not in routes:
        raise FileNotFoundError("Recording not found")
      route = routes[route_id]
      if route["locked"]:
        raise BlockingIOError("Recording is active or being exported")
      files = []
      for segment in route["segments"]:
        path = Path(root).resolve() / segment["name"]
        files.extend(p for p in segment_files(path) if not p.name.endswith(".lock"))
        lock_path = path / ".export.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        locks.append((lock_path, fd))
    yield files
  finally:
    for path, fd in locks:
      try:
        path.unlink(missing_ok=True)
      finally:
        os.close(fd)


def is_missing_or_busy(error: OSError) -> bool:
  return error.errno in (errno.ENOENT, errno.EAGAIN, errno.EACCES)

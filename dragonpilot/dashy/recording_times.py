"""Human-readable recording times without changing native route IDs or retention."""
import struct
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import capnp
import zstandard as zstd

from openpilot.cereal import log
from openpilot.system.loggerd.recordings import VIDEO_FILES

RECORDING_TIMEZONE = timezone(timedelta(hours=8))
MAX_EVENT_BYTES = 2 * 1024 * 1024


def _read_event(stream):
  # Read only the two logger header events, never decompress a whole drive.
  header = stream.read(8)
  if len(header) != 8:
    raise ValueError("Incomplete log header")
  count = struct.unpack_from('<I', header)[0] + 1
  if count > 64:
    raise ValueError("Invalid log segment table")
  header += stream.read(((count + 2) // 2) * 8 - 8)
  words = sum(struct.unpack_from(f'<{count}I', header, 4))
  if words * 8 > MAX_EVENT_BYTES:
    raise ValueError("Log header too large")
  payload = stream.read(words * 8)
  if len(payload) != words * 8:
    raise ValueError("Incomplete log event")
  return header + payload


@lru_cache(maxsize=2048)
def _log_times(path: Path, revision: tuple[int, int]) -> tuple[float, float] | None:
  # Size/mtime in the cache key invalidate headers that were still being written.
  try:
    with path.open('rb') as source, zstd.ZstdDecompressor(max_window_size=32 * 1024).stream_reader(source) as stream:
      with log.Event.from_bytes(_read_event(stream)) as init:
        if init.which() != 'initData':
          return None
        wall = init.initData.wallTimeNanos
        mono = init.logMonoTime
      with log.Event.from_bytes(_read_event(stream)) as sentinel:
        if sentinel.which() != 'sentinel' or sentinel.sentinel.type not in ('startOfRoute', 'startOfSegment'):
          return None
        delta = sentinel.logMonoTime - mono
      # The initData clock is captured once per route and repeated in every
      # segment. Use the segment sentinel's monotonic offset for its real start.
      if wall < 1577836800 * 10**9 or wall >= 4102444800 * 10**9 or not 0 <= delta < 7 * 86400 * 10**9:
        return None
      return wall / 1e9, (wall + delta) / 1e9
  except (OSError, ValueError, struct.error, capnp.KjException, zstd.ZstdError):
    return None


def segment_times(path: Path) -> dict:
  for name in ('qlog.zst', 'rlog.zst'):
    file = path / name
    try:
      if file.is_symlink():
        continue
      stat = file.stat()
    except OSError:
      continue
    times = _log_times(file, (stat.st_size, stat.st_mtime_ns))
    if times is not None:
      return {'routeStartTime': times[0], 'startTime': times[1], 'timeSource': 'log'}

  # Old/video-only recordings may have lost their log header. Do not present
  # last-write timestamps as an exact recording start.
  stamps = []
  for file in path.iterdir():
    if file.name in VIDEO_FILES | {'qlog.zst', 'rlog.zst'} and not file.is_symlink():
      try:
        stamps.append(file.stat().st_mtime)
      except FileNotFoundError:
        continue
  stamp = min(stamps) if stamps else path.stat().st_mtime
  return {'routeStartTime': stamp, 'startTime': stamp, 'timeSource': 'file'}


def add_recording_times(root: str, routes: list[dict]) -> list[dict]:
  for route in routes:
    for segment in route['segments']:
      try:
        segment.update(segment_times(Path(root) / segment['name']))
      except FileNotFoundError:
        # Retention may remove a completed segment while the read-only list is
        # being assembled. The next refresh will remove the stale list entry.
        segment.update(routeStartTime=route['mtime'], startTime=route['mtime'], timeSource='file')
    logged = [s for s in route['segments'] if s['timeSource'] == 'log']
    route['startTime'] = min(s['routeStartTime'] for s in (logged or route['segments']))
    route['timeSource'] = 'log' if logged else 'file'
  return sorted(routes, key=lambda r: (r['startTime'], r['id']), reverse=True)


def recording_date(stamp: float, source: str) -> str:
  date = datetime.fromtimestamp(stamp, RECORDING_TIMEZONE).strftime('%Y-%m-%d_%H-%M-%S%z')
  return date + ('_estimated' if source == 'file' else '')

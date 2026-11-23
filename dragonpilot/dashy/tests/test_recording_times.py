import io
import os
import struct
from datetime import datetime

import pytest
import zstandard as zstd

from dragonpilot.dashy.recording_times import (
  MAX_EVENT_BYTES, RECORDING_TIMEZONE, _read_event, add_recording_times, recording_date, segment_times,
)
from openpilot.cereal import log
from openpilot.system.loggerd.recordings import recording_inventory
from openpilot.system.loggerd.tests.test_recordings import ROUTE, make_segment

START = datetime(2026, 11, 23, 14, 30, 5, tzinfo=RECORDING_TIMEZONE).timestamp()


def write_timing_log(path, offset=0, start=START, name='qlog.zst'):
  init = log.Event.new_message(logMonoTime=10 * 10**9)
  init.init('initData').wallTimeNanos = int(start * 1e9)
  sentinel = log.Event.new_message(logMonoTime=(10 + offset) * 10**9)
  sentinel.init('sentinel').type = 'startOfSegment' if offset else 'startOfRoute'
  (path / name).write_bytes(zstd.compress(init.to_bytes() + sentinel.to_bytes()))


def test_real_logger_headers_recover_route_and_segment_start_after_copy(tmp_path):
  first = make_segment(tmp_path)
  later = make_segment(tmp_path, number=2)
  write_timing_log(first)
  write_timing_log(later, offset=120)
  # A copied file's mtime must not replace the recorded timestamp.
  for path in (first, later):
    for file in path.iterdir():
      os.utime(file, (START + 86400, START + 86400))
  route = add_recording_times(str(tmp_path), recording_inventory(str(tmp_path)))[0]
  assert route['startTime'] == START and route['timeSource'] == 'log'
  assert [s['startTime'] for s in route['segments']] == [START, START + 120]
  assert route['mtime'] == START + 86400
  assert recording_date(route['startTime'], route['timeSource']) == '2026-11-23_14-30-05+0800'


def test_missing_first_segment_still_recovers_route_start_and_uses_rlog(tmp_path):
  segment = make_segment(tmp_path, number=10)
  write_timing_log(segment, offset=600, name='rlog.zst')
  route = add_recording_times(str(tmp_path), recording_inventory(str(tmp_path)))[0]
  assert route['startTime'] == START
  assert route['segments'][0]['startTime'] == START + 600


@pytest.mark.parametrize('contents', [b'not-a-log', zstd.compress(b'\x00' * 3),
                                    zstd.compress(struct.pack('<II', 0, MAX_EVENT_BYTES))])
def test_corrupt_or_oversized_header_uses_explicit_estimate(tmp_path, contents):
  segment = make_segment(tmp_path)
  (segment / 'qlog.zst').write_bytes(contents)
  for file in segment.iterdir():
    os.utime(file, (START, START))
  times = segment_times(segment)
  assert times['timeSource'] == 'file' and times['startTime'] == START
  assert recording_date(times['startTime'], times['timeSource']) == '2026-11-23_14-30-05+0800_estimated'


def test_header_cache_retries_after_incomplete_log_is_finished(tmp_path):
  segment = make_segment(tmp_path)
  assert segment_times(segment)['timeSource'] == 'file'
  write_timing_log(segment)
  assert segment_times(segment)['timeSource'] == 'log'


def test_header_reader_consumes_only_one_event_and_bounds_allocation():
  event = log.Event.new_message(logMonoTime=10)
  event.init('initData')
  encoded = event.to_bytes()
  source = io.BytesIO(encoded + b'video-sized-tail')
  assert _read_event(source) == encoded
  assert source.tell() == len(encoded)
  with pytest.raises(ValueError, match='too large'):
    _read_event(io.BytesIO(struct.pack('<II', 0, MAX_EVENT_BYTES)))


def test_legacy_date_route_remains_accessible(tmp_path):
  legacy = '2026-11-23--06-30-05'
  segment = make_segment(tmp_path, route=legacy)
  write_timing_log(segment)
  route = add_recording_times(str(tmp_path), recording_inventory(str(tmp_path)))[0]
  assert route['id'] == legacy and route['startTime'] == START
  assert segment.name == legacy + '--0'


def test_recording_clock_out_of_range_is_not_reported_as_exact(tmp_path):
  segment = make_segment(tmp_path)
  write_timing_log(segment, start=0)
  assert segment_times(segment)['timeSource'] == 'file'


def test_sort_by_recorded_time_without_changing_native_retention_order(tmp_path):
  old = make_segment(tmp_path, route=ROUTE, age=0)
  new = make_segment(tmp_path, route='00000005--4c4e99b08b', age=1000)
  write_timing_log(old, start=START)
  write_timing_log(new, start=START + 86400)
  for file in new.iterdir():
    os.utime(file, (START - 1000, START - 1000))
  native = recording_inventory(str(tmp_path))
  assert native[0]['id'] == ROUTE
  dated = add_recording_times(str(tmp_path), native)
  assert dated[0]['id'] == '00000005--4c4e99b08b'
  assert recording_inventory(str(tmp_path))[0]['id'] == ROUTE

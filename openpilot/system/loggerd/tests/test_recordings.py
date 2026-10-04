import json
import os
import time
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from openpilot.system.loggerd.recordings import (
  GIB, LOCK_GRACE_SECONDS, RecordingPolicy, clear_stale_locks, delete_recording,
  export_recording, recording_inventory, recording_lock, retention_candidates,
)

ROUTE = "00000004--0ac3964c96"
OTHER = "00000005--4c4e99b08b"


def make_segment(root, route=ROUTE, number=0, age=0, locked=False):
  path = root / f"{route}--{number}"
  path.mkdir()
  (path / "fcamera.hevc").write_bytes(b'video-data')
  (path / "qlog.zst").write_bytes(b'log-data')
  if locked:
    (path / 'rlog.lock').touch()
  stamp = time.time() - age  # noqa: TID251 -- test filesystem timestamps
  for child in path.iterdir():
    os.utime(child, (stamp, stamp))
  os.utime(path, (stamp, stamp))
  return path


def test_recover_abandoned_locks_without_uploader(tmp_path):
  segment = make_segment(tmp_path, age=LOCK_GRACE_SECONDS + 60, locked=True)
  assert clear_stale_locks(str(tmp_path)) == 1
  assert not (segment / 'rlog.lock').exists()
  assert (segment / 'fcamera.hevc').read_bytes() == b'video-data'


@pytest.mark.parametrize('protection', ['current', 'recent', 'open', 'recent-video'])
def test_never_clear_live_locks(tmp_path, protection):
  segment = make_segment(tmp_path, age=LOCK_GRACE_SECONDS + 60, locked=True)
  if protection == 'recent':
    (segment / 'rlog.lock').touch()
  if protection == 'recent-video':
    (segment / 'fcamera.hevc').touch()
  with (segment / 'fcamera.hevc').open('rb') if protection == 'open' else open(os.devnull, 'rb'):
    assert clear_stale_locks(str(tmp_path), ROUTE if protection == 'current' else '') == 0
  assert (segment / 'rlog.lock').exists()


def test_unreadable_process_inventory_preserves_locks(tmp_path, monkeypatch):
  make_segment(tmp_path, age=1000, locked=True)
  monkeypatch.setattr('openpilot.system.loggerd.recordings.open_recording_directories', lambda root: None)
  assert clear_stale_locks(str(tmp_path)) == 0


def test_symlink_cannot_escape_recording_root(tmp_path):
  root = tmp_path / 'recordings'
  outside = tmp_path / 'outside'
  root.mkdir()
  outside.mkdir()
  segment = make_segment(outside, age=1000, locked=True)
  (root / segment.name).symlink_to(segment, target_is_directory=True)
  assert clear_stale_locks(str(root)) == 0
  assert recording_inventory(str(root)) == []
  with pytest.raises(FileNotFoundError):
    delete_recording(str(root), ROUTE)
  assert (segment / 'rlog.lock').exists()


def test_inventory_groups_numeric_segments_and_skips_non_recordings(tmp_path):
  for number in (10, 2, 0):
    make_segment(tmp_path, number=number)
  (tmp_path / 'boot').mkdir()
  inventory = recording_inventory(str(tmp_path))
  assert len(inventory) == 1
  assert [s['number'] for s in inventory[0]['segments']] == [0, 2, 10]
  assert inventory[0]['bytes'] == 3 * len(b'video-data' + b'log-data')
  assert inventory[0]['segments'][0]['videos'][0]['path'] == f'{ROUTE}--0/fcamera.hevc'


def test_current_route_is_protected_even_without_lock(tmp_path):
  make_segment(tmp_path)
  assert recording_inventory(str(tmp_path), ROUTE)[0]['locked']
  with pytest.raises(BlockingIOError):
    delete_recording(str(tmp_path), ROUTE, ROUTE)


def test_open_video_is_protected_even_without_recorder_lock(tmp_path):
  segment = make_segment(tmp_path)
  with (segment / 'fcamera.hevc').open('rb'):
    assert recording_inventory(str(tmp_path))[0]['locked']
    with pytest.raises(BlockingIOError):
      delete_recording(str(tmp_path), ROUTE)
    with pytest.raises(BlockingIOError), export_recording(str(tmp_path), ROUTE):
      pass
  assert not recording_inventory(str(tmp_path))[0]['locked']


def test_retention_checks_whole_route_and_never_touches_active(tmp_path):
  make_segment(tmp_path, age=10 * 86400)
  make_segment(tmp_path, number=1, age=3 * 86400)
  make_segment(tmp_path, route=OTHER, age=10 * 86400, locked=True)
  policy = RecordingPolicy(enabled=True, keep_days=7, max_gb=0)
  assert retention_candidates(str(tmp_path), policy) == set()
  assert retention_candidates(str(tmp_path), RecordingPolicy()) == set()
  assert retention_candidates(str(tmp_path), RecordingPolicy(True, 0, 0)) == set()


def test_expired_routes_and_preserved_events(tmp_path):
  segment = make_segment(tmp_path, age=10 * 86400)
  make_segment(tmp_path, route=OTHER, age=3 * 86400)
  policy = RecordingPolicy(True, 7, 0)
  assert retention_candidates(str(tmp_path), policy) == {segment.name}
  assert not retention_candidates(str(tmp_path), policy, preserved_dirs={segment.name})


def test_quota_counts_preserved_and_active_routes(tmp_path):
  old = make_segment(tmp_path, age=1000)
  new = make_segment(tmp_path, route=OTHER)
  # Sparse files avoid allocating gigabytes just to test quota decisions.
  for path in (old, new):
    with (path / 'fcamera.hevc').open('wb') as f:
      f.truncate(GIB)
  assert retention_candidates(str(tmp_path), RecordingPolicy(True, 0, 1)) == {old.name, new.name}
  assert retention_candidates(str(tmp_path), RecordingPolicy(True, 0, 1), preserved_dirs={old.name}) == {new.name}
  assert retention_candidates(str(tmp_path), RecordingPolicy(True, 0, 1), OTHER) == {old.name}


def test_delete_entire_route_leaves_other_recordings(tmp_path):
  first = make_segment(tmp_path)
  second = make_segment(tmp_path, number=1)
  other = make_segment(tmp_path, route=OTHER)
  assert delete_recording(str(tmp_path), ROUTE) == 2 * len(b'video-data' + b'log-data')
  assert not first.exists() and not second.exists() and other.exists()
  with pytest.raises(FileNotFoundError):
    delete_recording(str(tmp_path), '../outside')


def test_export_pins_all_segments_until_finished(tmp_path):
  first = make_segment(tmp_path, age=1000)
  second = make_segment(tmp_path, number=1, age=1000)
  with export_recording(str(tmp_path), ROUTE) as files:
    assert len(files) == 4
    assert all((p / '.export.lock').exists() for p in (first, second))
    # Even after the grace period, held export descriptors protect the route.
    stamp = time.time() - 1000  # noqa: TID251 -- test filesystem timestamps
    for path in (first, second):
      os.utime(path / '.export.lock', (stamp, stamp))
      os.utime(path, (stamp, stamp))
    assert clear_stale_locks(str(tmp_path)) == 0
    with pytest.raises(BlockingIOError):
      delete_recording(str(tmp_path), ROUTE)
  assert all(not (p / '.export.lock').exists() for p in (first, second))


def test_export_releases_locks_after_cancellation(tmp_path):
  path = make_segment(tmp_path)
  with pytest.raises(RuntimeError), export_recording(str(tmp_path), ROUTE):
    raise RuntimeError('Client disconnected')
  assert not (path / '.export.lock').exists()


def test_management_lock_prevents_overlapping_mutations(tmp_path):
  with recording_lock(str(tmp_path)), pytest.raises(BlockingIOError), recording_lock(str(tmp_path)):
    pass
  with recording_lock(str(tmp_path)):
    pass


@pytest.mark.parametrize('value', [None, [], {}, {'enabled': 1, 'keep_days': 7, 'max_gb': 20},
                                   {'enabled': True, 'keep_days': -1, 'max_gb': 20},
                                   {'enabled': True, 'keep_days': 7.5, 'max_gb': 20},
                                   {'enabled': True, 'keep_days': 7, 'max_gb': True},
                                   {'enabled': True, 'keep_days': 7, 'max_gb': 1025}])
def test_reject_invalid_policy(value):
  with pytest.raises(ValueError):
    RecordingPolicy.parse(value)


def test_policy_uses_typed_params_and_handles_legacy_json():
  params = Mock()
  policy = RecordingPolicy(True, 14, 12)
  policy.save(params)
  params.put.assert_called_once_with('dp_recording_policy', asdict(policy), block=True)
  for raw in (asdict(policy), json.dumps(asdict(policy)), json.dumps(asdict(policy)).encode()):
    params.get.return_value = raw
    assert RecordingPolicy.load(params) == policy
  params.get.return_value = 'corrupt'
  assert RecordingPolicy.load(params) == RecordingPolicy()

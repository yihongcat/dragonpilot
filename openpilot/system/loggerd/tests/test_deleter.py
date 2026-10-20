from collections import namedtuple
from pathlib import Path
from collections.abc import Sequence
import os
import tempfile
import time
from unittest.mock import patch

import openpilot.system.loggerd.deleter as deleter
from openpilot.system.loggerd.tests.loggerd_tests_common import UploaderTestCase

Stats = namedtuple("Stats", ['f_bavail', 'f_blocks', 'f_frsize'])


class TestDeleter(UploaderTestCase):
  # Deletion behavior is independent of file size; use smaller files to keep these tests fast.
  def make_file_with_data(self, f_dir: str, fn: str, size_mb: float = .001, lock: bool = False,
                          upload_xattr: bytes | None = None, preserve_xattr: bytes | None = None) -> Path:
    return super().make_file_with_data(f_dir, fn, size_mb, lock, upload_xattr, preserve_xattr)

  def fake_statvfs(self, d):
    return self.fake_stats

  def setup_method(self):
    self.f_type = "fcamera.hevc"
    super().openpilot_setup_method()
    self.fake_stats = Stats(f_bavail=0, f_blocks=10, f_frsize=4096)
    stat_patch = patch('openpilot.system.loggerd.deleter.os.statvfs', side_effect=self.fake_statvfs)
    stat_patch.start()
    self.addCleanup(stat_patch.stop)

  def test_delete(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def assertDeleteOrder(self, f_paths: Sequence[Path]) -> None:
    deleted_order = []
    for _ in f_paths:
      out_of_space, deleted_path = deleter.deleter_step()
      assert out_of_space and deleted_path is not None
      deleted_order.append(next(f for f in f_paths if f.parent == Path(deleted_path)))

    assert deleted_order == f_paths, "Files not deleted in expected order"

  def test_delete_order(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
    ])

  def test_delete_many_preserved(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data(self.seg_format.format(2), self.f_type),
    ] + [
      self.make_file_with_data(self.seg_format2.format(i), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE)
      for i in range(5)
    ])

  def test_delete_last(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(0), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data("boot", self.seg_format[:-4]),
      self.make_file_with_data("crash", self.seg_format2[:-4]),
    ])

  def test_no_delete_when_available_space(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)

    block_size = 4096
    available = (10 * 1024 * 1024 * 1024) / block_size  # 10GB free
    self.fake_stats = Stats(f_bavail=available, f_blocks=10, f_frsize=block_size)

    assert deleter.deleter_step() == (False, None)
    assert f_path.exists(), "File deleted with available space"

  def test_no_delete_with_lock_file(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)

    assert deleter.deleter_step() == (True, None)
    assert f_path.exists(), "File deleted when locked"

  def test_low_storage_pauses_recording_when_all_files_protected(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)
    self.params.put('DisableLogging', False, block=True)
    assert deleter.deleter_step() == (True, None)
    assert self.params.get_bool('DisableLogging')
    assert f_path.exists()

  def test_low_storage_pauses_recording_when_process_inspection_fails(self):
    self.params.put('DisableLogging', False, block=True)
    with patch('openpilot.system.loggerd.deleter.open_recording_directories', return_value=None):
      assert deleter.deleter_step(recover_locks=False) == (True, None)
    assert self.params.get_bool('DisableLogging')

  def test_successful_cleanup_does_not_pause_recording(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    self.params.put('DisableLogging', False, block=True)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not self.params.get_bool('DisableLogging')

  def test_delete_abandoned_locked_segment_without_uploader(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)
    stamp = time.time() - 1000  # noqa: TID251 -- test filesystem timestamps
    for path in [f_path.parent, *f_path.parent.iterdir()]:
      os.utime(path, (stamp, stamp))
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def test_system_service_permission_error_still_allows_emergency_cleanup(self):
    from openpilot.system.loggerd.recordings import open_recording_directories
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)
    stamp = time.time() - 1000  # noqa: TID251 -- test filesystem timestamps
    for path in [f_path.parent, *f_path.parent.iterdir()]:
      os.utime(path, (stamp, stamp))
    temporary_proc = tempfile.TemporaryDirectory()
    self.addCleanup(temporary_proc.cleanup)
    proc_root = Path(temporary_proc.name)
    process = proc_root / '123'
    process.mkdir(parents=True)
    (process / 'comm').write_text('logd')
    (process / 'cmdline').write_bytes(b'/sbin/logd\0')
    original_iterdir = Path.iterdir

    def protected_iterdir(path):
      if path == process / 'fd':
        raise PermissionError('AGNOS protected process')
      return original_iterdir(path)

    def scan(root):
      return open_recording_directories(root, str(proc_root))

    with patch.object(Path, 'iterdir', protected_iterdir), \
         patch('openpilot.system.loggerd.recordings.open_recording_directories', side_effect=scan), \
         patch('openpilot.system.loggerd.deleter.open_recording_directories', side_effect=scan):
      assert deleter.deleter_step() == (True, str(f_path.parent))
      assert not f_path.exists()

  def test_never_delete_current_route_without_lock(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    self.params.put('CurrentRoute', self.seg_dir.rsplit('--', 1)[0], block=True)
    self.params.put_bool('IsOffroad', False, block=True)
    assert deleter.deleter_step() == (True, None)
    assert f_path.exists()

  def test_long_drive_emergency_reclaims_only_completed_segments(self):
    first = self.make_file_with_data(self.seg_format.format(0), self.f_type)
    held = self.make_file_with_data(self.seg_format.format(1), self.f_type)
    locked = self.make_file_with_data(self.seg_format.format(2), self.f_type, lock=True)
    latest = self.make_file_with_data(self.seg_format.format(10), self.f_type)
    self.params.put('CurrentRoute', self.seg_format.rsplit('--', 1)[0], block=True)
    self.params.put_bool('IsOffroad', False, block=True)
    with held.open('rb'):
      assert deleter.deleter_step() == (True, str(first.parent))
      assert deleter.deleter_step() == (True, None)
    assert not first.exists()
    assert held.exists() and locked.exists() and latest.exists()

  def test_retention_keeps_entire_current_route_when_space_available(self):
    first = self.make_file_with_data(self.seg_format.format(0), self.f_type)
    self.make_file_with_data(self.seg_format.format(10), self.f_type)
    self.params.put('CurrentRoute', self.seg_format.rsplit('--', 1)[0], block=True)
    self.params.put_bool('IsOffroad', False, block=True)
    self.params.put('dp_recording_policy', {'enabled': True, 'keep_days': 0, 'max_gb': 1}, block=True)
    with first.open('wb') as f:
      f.truncate(2 * 1024 ** 3)
    self.fake_stats = Stats(f_bavail=10 * 1024 ** 3 / 4096, f_blocks=10, f_frsize=4096)
    assert deleter.deleter_step() == (False, None)
    assert first.exists()

  def test_finished_route_can_be_deleted_when_parked(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    self.params.put('CurrentRoute', self.seg_dir.rsplit('--', 1)[0], block=True)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def test_retention_runs_with_plenty_of_free_space(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    stamp = time.time() - 2 * 86400  # noqa: TID251 -- test filesystem timestamps
    os.utime(f_path, (stamp, stamp))
    self.params.put('dp_recording_policy', {'enabled': True, 'keep_days': 1, 'max_gb': 0}, block=True)
    self.fake_stats = Stats(f_bavail=10 * 1024 ** 3 / 4096, f_blocks=10, f_frsize=4096)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def test_quota_removes_whole_completed_drive(self):
    first = self.make_file_with_data(self.seg_format.format(0), self.f_type)
    second = self.make_file_with_data(self.seg_format.format(1), self.f_type)
    with first.open('wb') as f:
      f.truncate(1024 ** 3)
    self.params.put('dp_recording_policy', {'enabled': True, 'keep_days': 0, 'max_gb': 1}, block=True)
    self.fake_stats = Stats(f_bavail=10 * 1024 ** 3 / 4096, f_blocks=10, f_frsize=4096)
    assert deleter.deleter_step() == (True, str(first.parent))
    assert not first.exists() and not second.exists()

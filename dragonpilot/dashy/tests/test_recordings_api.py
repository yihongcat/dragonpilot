import asyncio
import io
import threading
import zipfile
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dragonpilot.dashy import recordings_api as api
from openpilot.system.loggerd.tests.test_recordings import ROUTE, OTHER, make_segment


@pytest.fixture(autouse=True)
def recording_disk_space(monkeypatch):
  # Activation checks must not depend on the development host's free space.
  monkeypatch.setattr(api.shutil, 'disk_usage', lambda root: SimpleNamespace(total=30 * api.GIB, free=25 * api.GIB, used=5 * api.GIB))


class TestParams:
  __test__ = False

  def __init__(self, parked=True):
    self.values = {'IsOffroad': parked}

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.get(key))

  def put(self, key, value, block=False):
    self.values[key] = value


def client(root, params):
  app = web.Application()
  app['cache'] = SimpleNamespace(params=params)
  api.setup_recordings_api(app, str(root))
  return TestClient(TestServer(app))


def test_inventory_and_persistent_policy(tmp_path):
  make_segment(tmp_path)
  params = TestParams()
  params.values['DisableLogging'] = True

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.get('/api/recordings')
      result = await response.json()
      assert response.status == 200 and result['routes'][0]['id'] == ROUTE
      assert result['recordingEnabled'] is False
      policy = {'enabled': True, 'keep_days': 14, 'max_gb': 10}
      assert (await browser.post('/api/recordings/policy', json=policy)).status == 200
      assert params.values['dp_recording_policy'] == policy
      assert (await (await browser.get('/api/recordings')).json())['policy'] == policy
      assert params.values['DisableLogging'] is True

  asyncio.run(run())


@pytest.mark.parametrize('endpoint,body', [('/recording', {'enabled': True}), ('/recording', {'enabled': False}),
                                         ('/policy', {'enabled': True, 'keep_days': 7, 'max_gb': 20}),
                                         ('/delete', {'routes': [ROUTE]})])
def test_writes_rejected_while_driving(tmp_path, endpoint, body):
  segment = make_segment(tmp_path)
  params = TestParams(parked=False)

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.post('/api/recordings' + endpoint, json=body)
      assert response.status == 409
      response = await browser.get(f'/api/recordings/{ROUTE}/download')
      assert response.status == 409
  asyncio.run(run())
  assert segment.exists() and 'dp_recording_policy' not in params.values


def test_recording_toggle_persists_cleanup_before_enabling_and_can_disable(tmp_path, monkeypatch):
  params = TestParams()
  params.values['DisableLogging'] = True
  original_put = params.put
  writes = []

  def persist(key, value, block=False):
    assert block
    writes.append(key)
    if key == 'DisableLogging' and not value:
      assert params.values['dp_recording_policy'] == {'enabled': True, 'keep_days': 7, 'max_gb': 20}
    original_put(key, value, block)

  monkeypatch.setattr(params, 'put', persist)
  monkeypatch.setattr(api.shutil, 'disk_usage', lambda root: SimpleNamespace(total=30 * api.GIB, free=25 * api.GIB, used=5 * api.GIB))

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.post('/api/recordings/recording', json={'enabled': True})
      assert response.status == 200
      assert (await response.json())['recordingEnabled'] is True
      assert params.values['DisableLogging'] is False
      assert writes == ['dp_recording_policy', 'DisableLogging']
      assert (await (await browser.get('/api/recordings')).json())['recordingEnabled'] is True
      response = await browser.post('/api/recordings/recording', json={'enabled': False})
      assert response.status == 200 and params.values['DisableLogging'] is True
      assert params.values['dp_recording_policy']['enabled'] is True
  asyncio.run(run())


@pytest.mark.parametrize('body', [{}, {'enabled': 1}, {'enabled': 'false'}, {'enabled': None}, {'enabled': True, 'extra': 1}])
def test_recording_toggle_rejects_invalid_values(tmp_path, body):
  params = TestParams()
  params.values['DisableLogging'] = True

  async def run():
    async with client(tmp_path, params) as browser:
      assert (await browser.post('/api/recordings/recording', json=body)).status == 400
  asyncio.run(run())
  assert params.values == {'IsOffroad': True, 'DisableLogging': True}


@pytest.mark.parametrize('failure', ['low-space', 'unknown-process', 'car-started', 'persistence'])
def test_recording_enable_fails_closed(tmp_path, monkeypatch, failure):
  params = TestParams()
  params.values['DisableLogging'] = True
  if failure == 'low-space':
    monkeypatch.setattr(api.shutil, 'disk_usage', lambda root: SimpleNamespace(total=30 * api.GIB, free=5 * api.GIB, used=25 * api.GIB))
  elif failure == 'unknown-process':
    monkeypatch.setattr(api, 'open_recording_directories', lambda root: None)
  elif failure == 'car-started':
    def scan(root):
      params.values['IsOffroad'] = False
      return set()
    monkeypatch.setattr(api, 'open_recording_directories', scan)
  else:
    def fail_save(key, value, block=False):
      raise OSError('Persistence failed')
    monkeypatch.setattr(params, 'put', fail_save)

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.post('/api/recordings/recording', json={'enabled': True})
      assert response.status == (500 if failure == 'persistence' else 409)
  asyncio.run(run())
  assert params.values['DisableLogging'] is True
  assert 'dp_recording_policy' not in params.values


def test_disable_recording_remains_available_when_cleanup_check_fails(tmp_path, monkeypatch):
  params = TestParams()
  params.values['DisableLogging'] = False
  monkeypatch.setattr(api, 'recording_budget', lambda root: (_ for _ in ()).throw(OSError('Disk unavailable')))
  monkeypatch.setattr(api, 'open_recording_directories', lambda root: None)

  async def run():
    async with client(tmp_path, params) as browser:
      assert (await browser.post('/api/recordings/recording', json={'enabled': False})).status == 200
  asyncio.run(run())
  assert params.values['DisableLogging'] is True


@pytest.mark.parametrize('requested,expected', [(0, 20), (1024, 25), (12, 12)])
def test_recording_quota_preserves_headroom(tmp_path, monkeypatch, requested, expected):
  params = TestParams()
  params.values.update(DisableLogging=True, dp_recording_policy={'enabled': False, 'keep_days': 14, 'max_gb': requested})
  monkeypatch.setattr(api.shutil, 'disk_usage', lambda root: SimpleNamespace(total=30 * api.GIB, free=25 * api.GIB, used=5 * api.GIB))
  result = api.set_recording_enabled(params, str(tmp_path), True)
  assert result['policy'] == {'enabled': True, 'keep_days': 14, 'max_gb': expected}


@pytest.mark.parametrize('enabled,quota,expected', [(False, 20, 409), (True, 0, 409), (True, 1024, 400), (True, 10, 200)])
def test_recording_keeps_bounded_cleanup_enabled(tmp_path, monkeypatch, enabled, quota, expected):
  params = TestParams()
  params.values['DisableLogging'] = False
  monkeypatch.setattr(api.shutil, 'disk_usage', lambda root: SimpleNamespace(total=30 * api.GIB, free=25 * api.GIB, used=5 * api.GIB))

  async def run():
    async with client(tmp_path, params) as browser:
      policy = {'enabled': enabled, 'keep_days': 7, 'max_gb': quota}
      assert (await browser.post('/api/recordings/policy', json=policy)).status == expected
  asyncio.run(run())
  assert params.values['DisableLogging'] is False
  assert ('dp_recording_policy' in params.values) is (expected == 200)


def test_enabling_recording_activates_real_deletion(tmp_path, monkeypatch):
  import openpilot.system.loggerd.deleter as deleter

  segment = make_segment(tmp_path, age=10 * 86400, locked=True)
  params = TestParams()
  params.values['DisableLogging'] = True
  api.set_recording_enabled(params, str(tmp_path), True)
  monkeypatch.setattr(deleter, 'Params', lambda: params)
  monkeypatch.setattr(deleter.Paths, 'log_root', lambda: str(tmp_path))
  monkeypatch.setattr(deleter, 'get_available_bytes', lambda **kwargs: 10 * api.GIB)
  monkeypatch.setattr(deleter, 'get_available_percent', lambda **kwargs: 50)
  assert deleter.deleter_step() == (True, str(segment))
  assert not segment.exists()


def test_delete_batch_validates_before_removing_any_route(tmp_path):
  segment = make_segment(tmp_path)
  other = make_segment(tmp_path, route=OTHER, locked=True)

  async def run():
    async with client(tmp_path, TestParams()) as browser:
      response = await browser.post('/api/recordings/delete', json={'routes': [ROUTE, OTHER]})
      assert response.status == 409 and segment.exists()
      response = await browser.post('/api/recordings/delete', json={'routes': [ROUTE, '../escape']})
      assert response.status == 404 and segment.exists()
      response = await browser.post('/api/recordings/delete', json={'routes': [ROUTE]})
      assert response.status == 200
      assert (await response.json())['freed'] == len(b'video-data' + b'log-data')
  asyncio.run(run())
  assert not segment.exists() and other.exists()


def test_reject_cross_site_and_non_json_requests(tmp_path):
  segment = make_segment(tmp_path)

  async def run():
    async with client(tmp_path, TestParams()) as browser:
      response = await browser.post('/api/recordings/delete', json={'routes': [ROUTE]}, headers={'Origin': 'https://other.example'})
      assert response.status == 403
      response = await browser.post('/api/recordings/delete', data='{"routes": []}')
      assert response.status == 415
      response = await browser.post('/api/recordings/delete', json={'routes': [ROUTE, ROUTE]})
      assert response.status == 400
      response = await browser.post('/api/recordings/policy', json={'enabled': True, 'keep_days': -1, 'max_gb': 20})
      assert response.status == 400
      response = await browser.post('/api/recordings/recording', json={'enabled': True}, headers={'Origin': 'https://other.example'})
      assert response.status == 403
      assert (await browser.post('/api/recordings/recording', data='enabled=true')).status == 415
  asyncio.run(run())
  assert segment.exists()


def test_wifi_export_is_valid_zip_with_original_video_and_logs(tmp_path):
  first = make_segment(tmp_path)
  make_segment(tmp_path, number=1)

  params = TestParams()
  params.values['CurrentRoute'] = ROUTE  # The last drive remains in Params while parked.

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.get(f'/api/recordings/{ROUTE}/download')
      assert response.status == 200
      assert response.headers['Content-Disposition'] == f'attachment; filename="{ROUTE}.zip"'
      with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
        assert len(archive.namelist()) == 4
        assert archive.read(f'{ROUTE}--0/fcamera.hevc') == b'video-data'
        assert archive.read(f'{ROUTE}--1/qlog.zst') == b'log-data'
  asyncio.run(run())
  assert not (first / '.export.lock').exists()
  assert len(list(tmp_path.iterdir())) == 2  # No temporary ZIP stored beside recordings.


def test_export_requires_wifi_on_device(tmp_path, monkeypatch):
  make_segment(tmp_path)
  monkeypatch.setattr(api, 'PC', False)
  monkeypatch.setattr(api.HARDWARE, 'get_network_type', lambda: api.log.DeviceState.NetworkType.cell4G)

  async def run():
    async with client(tmp_path, TestParams()) as browser:
      response = await browser.get(f'/api/recordings/{ROUTE}/download')
      assert response.status == 409
  asyncio.run(run())


def test_archive_backpressure_cancellation_does_not_leave_blocked_worker():
  async def run():
    queue = asyncio.Queue(maxsize=1)
    await queue.put(b'occupied')
    stop = threading.Event()
    sink = api.ArchiveSink(asyncio.get_running_loop(), queue, stop)
    task = asyncio.create_task(asyncio.to_thread(sink.send, None))
    await asyncio.sleep(.05)
    stop.set()
    with pytest.raises(ConnectionAbortedError):
      await asyncio.wait_for(task, timeout=2)
  asyncio.run(run())


def test_recordings_page_and_api_registered_in_real_server(tmp_path, monkeypatch):
  from dragonpilot.dashy import serverd
  from openpilot.common.prefix import OpenpilotPrefix

  make_segment(tmp_path)
  monkeypatch.setattr(serverd, 'DEFAULT_DIR', str(tmp_path))

  async def run():
    app = serverd.setup_aiohttp_app('127.0.0.1', 0, False)
    async with TestClient(TestServer(app)) as browser:
      page = await browser.get('/pages/recordings.html')
      page_text = await page.text()
      assert page.status == 200 and '录像管理' in page_text and 'id="recording-toggle"' in page_text
      response = await browser.get('/api/recordings')
      assert response.status == 200
      assert (await response.json())['routes'][0]['id'] == ROUTE
      assert app['cache'].params.check_key('dp_recording_policy')
      params = app['cache'].params
      params.put('IsOffroad', True, block=True)
      params.put('DisableLogging', True, block=True)
      response = await browser.post('/api/settings/params/DisableLogging', json={'value': 'false'})
      assert response.status == 400 and params.get_bool('DisableLogging')
      response = await browser.post('/api/settings/params/DisableLogging', json={'value': False}, headers={'Origin': 'https://other.example'})
      assert response.status == 403
      response = await browser.post('/api/settings/params/DisableLogging', json={'value': False})
      assert response.status == 200
      from openpilot.common.params import Params
      from openpilot.system.loggerd.recordings import RecordingPolicy
      assert not Params().get_bool('DisableLogging')
      assert RecordingPolicy.load(Params()).enabled
      params.put('IsOffroad', False, block=True)
      response = await browser.post('/api/settings/params/DisableLogging', json={'value': True})
      assert response.status == 409 and not params.get_bool('DisableLogging')
      params.put('IsOffroad', True, block=True)
      response = await browser.post('/api/settings/params/DisableLogging', json={'value': True})
      assert response.status == 200 and Params().get_bool('DisableLogging')

  with OpenpilotPrefix():
    asyncio.run(run())

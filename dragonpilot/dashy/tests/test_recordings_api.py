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

  async def run():
    async with client(tmp_path, params) as browser:
      response = await browser.get('/api/recordings')
      result = await response.json()
      assert response.status == 200 and result['routes'][0]['id'] == ROUTE
      policy = {'enabled': True, 'keep_days': 14, 'max_gb': 10}
      assert (await browser.post('/api/recordings/policy', json=policy)).status == 200
      assert params.values['dp_recording_policy'] == policy
      assert (await (await browser.get('/api/recordings')).json())['policy'] == policy

  asyncio.run(run())


@pytest.mark.parametrize('endpoint,body', [('/policy', {'enabled': True, 'keep_days': 7, 'max_gb': 20}),
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
      assert page.status == 200 and '录像管理' in await page.text()
      response = await browser.get('/api/recordings')
      assert response.status == 200
      assert (await response.json())['routes'][0]['id'] == ROUTE
      assert app['cache'].params.check_key('dp_recording_policy')

  with OpenpilotPrefix():
    asyncio.run(run())

"""Parked-only local recording management and bounded-memory Wi-Fi exports."""
import asyncio
import io
import logging
import shutil
import threading
import time
import zipfile
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from openpilot.cereal import log
from openpilot.common.hardware import HARDWARE, PC
from openpilot.system.loggerd.deleter import MIN_BYTES, MIN_PERCENT
from openpilot.system.loggerd.recordings import (
  GIB, RecordingPolicy, clear_stale_locks, delete_recording, export_recording,
  recording_inventory, recording_lock,
  current_recording_route, open_recording_directories,
)

logger = logging.getLogger("dashy.recordings")


def recording_api(handler):
  @wraps(handler)
  async def wrapped(request):
    try:
      return await handler(request)
    except web.HTTPException:
      raise
    except FileNotFoundError:
      raise web.HTTPNotFound(text="Recording not found") from None
    except BlockingIOError:
      raise web.HTTPConflict(text="Recording is active or being exported; try again later") from None
    except ValueError as e:
      raise web.HTTPBadRequest(text=str(e)) from e
    except OSError:
      logger.exception("Recording operation failed")
      raise web.HTTPInternalServerError(text="Recording operation failed") from None
  return wrapped


def current_route(params):
  return current_recording_route(params)


def require_parked(params):
  if not params.get_bool("IsOffroad"):
    raise web.HTTPConflict(text="请停车后再切换录制、删除、修改设置或导出录像")


def recording_budget(root):
  usage = shutil.disk_usage(root)
  reserve = max(MIN_BYTES, int(usage.total * MIN_PERCENT / 100))
  return max(0, (usage.total - reserve) // GIB), usage.free - reserve


def set_recording_enabled(params, root, enabled):
  if type(enabled) is not bool:
    raise web.HTTPBadRequest(text="录制开关必须为开启或关闭")
  with recording_lock(root):
    require_parked(params)
    policy = RecordingPolicy.load(params)
    if enabled:
      budget, available = recording_budget(root)
      if budget < 1 or available < GIB:
        raise web.HTTPConflict(text="可用空间不足，请先清理旧录像再开启录制")
      if open_recording_directories(root) is None:
        raise web.HTTPConflict(text="暂时无法确认录像占用，自动清理检查未通过，请稍后重试")
      policy = RecordingPolicy(True, policy.keep_days, min(policy.max_gb or 20, budget))
      # Persist the cleanup policy before allowing the recorder to start.
      require_parked(params)
      policy.save(params)
    require_parked(params)
    params.put('DisableLogging', not enabled, block=True)
    return {'recordingEnabled': enabled, 'policy': asdict(policy)}


def set_recording_policy(params, root, policy):
  with recording_lock(root):
    require_parked(params)
    if not params.get_bool('DisableLogging'):
      budget, _ = recording_budget(root)
      if not policy.enabled or not policy.max_gb:
        raise web.HTTPConflict(text="录制开启时必须开启自动整理，并设置录像容量上限")
      if policy.max_gb > budget:
        raise web.HTTPBadRequest(text=f"当前设备录像容量上限不能超过 {budget}GB")
    require_parked(params)
    policy.save(params)


async def json_body(request):
  origin = request.headers.get("Origin")
  if origin and urlsplit(origin).netloc != request.host:
    raise web.HTTPForbidden(text="Use the recording manager on this device")
  if request.content_type != "application/json":
    raise web.HTTPUnsupportedMediaType(text="JSON body required")
  try:
    body = await request.json()
  except (ValueError, UnicodeError):
    raise web.HTTPBadRequest(text="Invalid JSON") from None
  if not isinstance(body, dict):
    raise web.HTTPBadRequest(text="JSON object required")
  return body


@recording_api
async def list_recordings(request):
  params = request.app['cache'].params
  root = request.app['recording_root']

  def inventory():
    # Inventory is read-only; deleter owns recovery of stale recorder locks.
    routes = recording_inventory(root, current_route(params))
    usage = shutil.disk_usage(root)
    return {"routes": routes, "storage": {"total": usage.total, "free": usage.free, "used": usage.used,
                                         "recordings": sum(r["bytes"] for r in routes)}}

  result = await asyncio.to_thread(inventory)
  result['policy'] = asdict(RecordingPolicy.load(params))
  result['isOffroad'] = params.get_bool("IsOffroad")
  result['recordingEnabled'] = not params.get_bool("DisableLogging")
  result['wifi'] = PC or await asyncio.to_thread(HARDWARE.get_network_type) == log.DeviceState.NetworkType.wifi
  return web.json_response(result)


@recording_api
async def save_policy(request):
  params = request.app['cache'].params
  body = await json_body(request)
  policy = RecordingPolicy.parse(body)
  await asyncio.to_thread(set_recording_policy, params, request.app['recording_root'], policy)
  return web.json_response({"policy": asdict(policy)})


@recording_api
async def save_recording_enabled(request):
  body = await json_body(request)
  if set(body) != {'enabled'}:
    raise web.HTTPBadRequest(text="请提供录制开关")
  result = await asyncio.to_thread(set_recording_enabled, request.app['cache'].params,
                                   request.app['recording_root'], body['enabled'])
  return web.json_response(result)


@recording_api
async def delete_routes(request):
  params = request.app['cache'].params
  body = await json_body(request)
  ids = body.get('routes')
  if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(r, str) for r in ids) or len(set(ids)) != len(ids):
    raise ValueError("Select between 1 and 100 different recordings")
  root = request.app['recording_root']

  def delete():
    with recording_lock(root):
      require_parked(params)
      active_route = current_route(params)
      clear_stale_locks(root, active_route)
      inventory = {r['id']: r for r in recording_inventory(root, active_route)}
      for route_id in ids:
        if route_id not in inventory:
          raise FileNotFoundError
        if inventory[route_id]['locked']:
          raise BlockingIOError
      freed = 0
      for route_id in ids:
        require_parked(params)
        freed += delete_recording(root, route_id, current_route(params))
      return freed

  freed = await asyncio.to_thread(delete)
  return web.json_response({"deleted": len(ids), "freed": freed})


class ArchiveSink(io.RawIOBase):
  """zipfile's non-seekable output backed by a bounded async queue."""
  def __init__(self, loop, queue, stop):
    self.loop, self.queue, self.stop = loop, queue, stop

  def writable(self):
    return True

  def write(self, data):
    if not data:
      return 0
    self.send(bytes(data))
    return len(data)

  def send(self, value):
    future = asyncio.run_coroutine_threadsafe(self.queue.put(value), self.loop)
    while not self.stop.is_set():
      try:
        future.result(timeout=.2)
        return
      except TimeoutError:
        continue
    future.cancel()
    raise ConnectionAbortedError("Export cancelled")


@recording_api
async def download_route(request):
  params = request.app['cache'].params
  require_parked(params)
  if not PC and await asyncio.to_thread(HARDWARE.get_network_type) != log.DeviceState.NetworkType.wifi:
    raise web.HTTPConflict(text="Connect the device to Wi-Fi before exporting recordings")

  root = request.app['recording_root']
  route_id = request.match_info['route_id']
  context = export_recording(root, route_id, current_route(params))
  files = await asyncio.to_thread(context.__enter__)
  stop = threading.Event()
  queue = asyncio.Queue(maxsize=4)
  loop = asyncio.get_running_loop()

  def archive():
    try:
      sink = ArchiveSink(loop, queue, stop)
      # Store already-compressed video directly; no temporary multi-GB archive.
      with zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        for path in files:
          if stop.is_set():
            return
          info = zipfile.ZipInfo.from_file(path, arcname=f"{path.parent.name}/{path.name}", strict_timestamps=False)
          with path.open('rb') as source, output.open(info, 'w', force_zip64=True) as target:
            while chunk := source.read(256 * 1024):
              if stop.is_set():
                return
              target.write(chunk)
    except Exception as e:
      if not stop.is_set():
        try:
          sink.send(e)
        except ConnectionAbortedError:
          pass
    finally:
      context.__exit__(None, None, None)
      if not stop.is_set():
        try:
          sink.send(None)
        except ConnectionAbortedError:
          pass

  worker = threading.Thread(target=archive, name="recording-export", daemon=True)
  response = web.StreamResponse(headers={'Content-Type': 'application/zip',
                                        'Content-Disposition': f'attachment; filename="{route_id}.zip"',
                                        'Cache-Control': 'no-store'})
  worker.start()
  try:
    await response.prepare(request)
    next_check = 0.
    while True:
      chunk = await queue.get()
      if chunk is None:
        break
      if isinstance(chunk, Exception):
        raise ConnectionAbortedError("Export interrupted") from chunk
      if time.monotonic() >= next_check:
        # Stop large transfers if ignition turns on while the browser is open.
        if not params.get_bool("IsOffroad"):
          raise ConnectionAbortedError("Car started during export")
        next_check = time.monotonic() + 1
      await response.write(chunk)
    await response.write_eof()
  finally:
    stop.set()
    await asyncio.to_thread(worker.join, 3)
  return response


def setup_recordings_api(app, root):
  app['recording_root'] = str(Path(root).resolve())
  app.router.add_get('/api/recordings', list_recordings)
  app.router.add_post('/api/recordings/policy', save_policy)
  app.router.add_post('/api/recordings/recording', save_recording_enabled)
  app.router.add_post('/api/recordings/delete', delete_routes)
  app.router.add_get('/api/recordings/{route_id}/download', download_route)

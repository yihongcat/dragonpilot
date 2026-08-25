from collections.abc import Collection

from openpilot.cereal.visionipc import VisionStreamType


DRIVER_CAMERA_PRESENT_PARAM = "DriverCameraPresent"
DRIVER_CAMERA_PROBE_TIME = 3.0
ROAD_CAMERA_STREAMS = {
  VisionStreamType.VISION_STREAM_NARROW_ROAD,
  VisionStreamType.VISION_STREAM_WIDE_ROAD,
}


def driver_camera_present(streams: Collection[VisionStreamType]) -> bool | None:
  """Return the driver-camera capability once both road cameras are ready."""
  available_streams = set(streams)
  if not ROAD_CAMERA_STREAMS.issubset(available_streams):
    return None
  return VisionStreamType.VISION_STREAM_CABIN in available_streams


def set_driver_camera_present(params, present: bool | None) -> None:
  stored = params.get(DRIVER_CAMERA_PRESENT_PARAM)
  if present is not None and (stored is None or params.get_bool(DRIVER_CAMERA_PRESENT_PARAM) != present):
    params.put_bool(DRIVER_CAMERA_PRESENT_PARAM, present)

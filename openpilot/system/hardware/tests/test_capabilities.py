from msgq.visionipc import VisionStreamType

from openpilot.common.hardware.capabilities import driver_camera_present


ROAD = VisionStreamType.VISION_STREAM_ROAD
WIDE_ROAD = VisionStreamType.VISION_STREAM_WIDE_ROAD
DRIVER = VisionStreamType.VISION_STREAM_DRIVER


def test_driver_camera_requires_both_road_streams():
  assert driver_camera_present(set()) is None
  assert driver_camera_present({ROAD}) is None
  assert driver_camera_present({WIDE_ROAD}) is None
  assert driver_camera_present({ROAD, DRIVER}) is None


def test_driver_camera_capability():
  assert driver_camera_present({ROAD, WIDE_ROAD}) is False
  assert driver_camera_present({ROAD, WIDE_ROAD, DRIVER}) is True

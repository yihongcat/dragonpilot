import importlib.util
import os
import platform
import time

from opendbc.car.structs import car
from openpilot.common.params import Params
from openpilot.common.hardware import PC, COMMA_HARDWARE
from openpilot.common.hardware.capabilities import DRIVER_CAMERA_PROBE_TIME, driver_camera_present, set_driver_camera_present
from openpilot.system.manager.process import PythonProcess, NativeProcess, DaemonProcess
from msgq.visionipc import VisionIpcClient

WEBCAM = os.getenv("USE_WEBCAM") is not None
LITE = os.getenv("LITE") is not None
TICI_DOS = "TICI_DOS" in os.environ
AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

driver_camera_missing_since: float | None = None
driver_camera_probe_result: bool | None = None

def driverview(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started or params.get_bool("IsDriverViewEnabled")

def driver_monitoring(started: bool, params: Params, CP: car.CarParams) -> bool:
  global driver_camera_missing_since, driver_camera_probe_result

  if not driverview(started, params, CP):
    driver_camera_missing_since = None
    return False
  if os.getenv("DISABLE_DRIVER") is not None:
    driver_camera_missing_since = None
    return False
  if WEBCAM and not os.getenv("DRIVER_CAM"):
    driver_camera_missing_since = None
    return False
  if driver_camera_probe_result is not None:
    return driver_camera_probe_result

  available_streams = VisionIpcClient.available_streams("camerad", block=False)
  present = driver_camera_present(available_streams)
  if present is True:
    driver_camera_missing_since = None
    set_driver_camera_present(params, True)
    driver_camera_probe_result = True
    return True
  if present is None:
    driver_camera_missing_since = None
    return False

  if driver_camera_missing_since is None:
    driver_camera_missing_since = time.monotonic()
  elif time.monotonic() - driver_camera_missing_since >= DRIVER_CAMERA_PROBE_TIME:
    set_driver_camera_present(params, False)
    driver_camera_probe_result = False
  return False

def notcar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and CP.notCar

def iscar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not CP.notCar

def logging(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("DisableLogging")

def ublox_available() -> bool:
  return os.path.exists('/dev/ttyHS0') and not os.path.exists('/persist/comma/use-quectel-gps')

def ublox(started: bool, params: Params, CP: car.CarParams) -> bool:
  use_ublox = ublox_available()
  if use_ublox != params.get_bool("UbloxAvailable"):
    params.put_bool("UbloxAvailable", use_ublox, block=True)
  return started and use_ublox

def joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("JoystickDebugMode")

def not_joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("JoystickDebugMode")

def long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LongitudinalManeuverMode")

def lat_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LateralManeuverMode")

def not_long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("LongitudinalManeuverMode")

def opview(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("dp_dev_opview")

def qcomgps(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not ublox_available()

def always_run(started: bool, params: Params, CP: car.CarParams) -> bool:
  return True

def only_onroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started

def only_offroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return not started

def beep(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("dp_dev_beep")

def dashy(started: bool, params: Params, CP: car.CarParams) -> bool:
  return params.get_bool("dp_dev_dashy")

def comma_connect(started: bool, params: Params, CP: car.CarParams) -> bool:
  return not params.get_bool("dp_dev_disable_connect")

def livestream(started: bool, params: Params, CP: car.CarParams) -> bool:
  return params.get_bool("IsLiveStreaming")

def or_(*fns):
  return lambda *args: any(fn(*args) for fn in fns)

def and_(*fns):
  return lambda *args: all(fn(*args) for fn in fns)

def not_(fn):
  return lambda *args: not fn(*args)

procs = [
  DaemonProcess("manage_athenad", "openpilot.system.athena.manage_athenad", "AthenadPid"),

  NativeProcess("loggerd", "openpilot/system/loggerd", ["./loggerd"], logging),
  NativeProcess("encoderd", "openpilot/system/loggerd", ["./encoderd"], only_onroad),
  NativeProcess("stream_encoderd", "openpilot/system/loggerd", ["./encoderd", "--stream"], or_(and_(livestream, not_(iscar)), notcar, opview)),
  PythonProcess("logmessaged", "openpilot.system.logmessaged", always_run),

  NativeProcess("camerad", "openpilot/system/camerad", ["./camerad"], or_(driverview, livestream), enabled=not WEBCAM),
  PythonProcess("webcamerad", "openpilot.system.camerad.webcam.camerad", driverview, enabled=WEBCAM),
  PythonProcess("proclogd", "openpilot.system.proclogd", only_onroad, enabled=platform.system() != "Darwin"),
  PythonProcess("journald", "openpilot.system.journald", only_onroad, platform.system() != "Darwin"),
  PythonProcess("micd", "openpilot.system.micd", iscar, enabled=not LITE),
  PythonProcess("timed", "openpilot.system.timed", always_run, enabled=not PC),

  PythonProcess("modeld", "openpilot.selfdrive.modeld.modeld", only_onroad),
  PythonProcess("dmonitoringmodeld", "openpilot.selfdrive.modeld.dmonitoringmodeld", driver_monitoring, enabled=(WEBCAM or not PC) and not LITE),

  PythonProcess("sensord", "openpilot.system.sensord.sensord", only_onroad, enabled=not PC),
  PythonProcess("ui", "openpilot.selfdrive.ui.ui", always_run),
  PythonProcess("soundd", "openpilot.selfdrive.ui.soundd", driverview, enabled=not LITE),
  PythonProcess("beepd", "dragonpilot.selfdrive.ui.beepd", beep, enabled=LITE),
  PythonProcess("locationd", "openpilot.selfdrive.locationd.locationd", only_onroad),
  NativeProcess("_pandad", "openpilot/selfdrive/pandad", ["./pandad"], always_run, enabled=False),
  PythonProcess("calibrationd", "openpilot.selfdrive.locationd.calibrationd", only_onroad),
  PythonProcess("torqued", "openpilot.selfdrive.locationd.torqued", only_onroad),
  PythonProcess("controlsd", "openpilot.selfdrive.controls.controlsd", and_(not_joystick, iscar)),
  PythonProcess("joystickd", "openpilot.tools.joystick.joystickd", or_(joystick, notcar)),
  PythonProcess("selfdrived", "openpilot.selfdrive.selfdrived.selfdrived", only_onroad),
  PythonProcess("card", "openpilot.selfdrive.car.card", only_onroad),
  PythonProcess("deleter", "openpilot.system.loggerd.deleter", always_run),
  PythonProcess("dmonitoringd", "openpilot.selfdrive.monitoring.dmonitoringd", driver_monitoring, enabled=(WEBCAM or not PC) and not LITE),
  PythonProcess("qcomgpsd", "openpilot.system.qcomgpsd.qcomgpsd", qcomgps, enabled=COMMA_HARDWARE),
  PythonProcess("pandad", "openpilot.selfdrive.pandad.pandad" if not TICI_DOS else "selfdrive.pandad_tici.pandad", always_run),
  PythonProcess("paramsd", "openpilot.selfdrive.locationd.paramsd", only_onroad),
  PythonProcess("lagd", "openpilot.selfdrive.locationd.lagd", only_onroad),
  PythonProcess("ubloxd", "openpilot.system.ubloxd.ubloxd", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("pigeond", "openpilot.system.ubloxd.pigeond", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("plannerd", "openpilot.selfdrive.controls.plannerd", not_long_maneuver),
  PythonProcess("maneuversd", "openpilot.tools.longitudinal_maneuvers.maneuversd", long_maneuver),
  PythonProcess("lateral_maneuversd", "openpilot.tools.lateral_maneuvers.lateral_maneuversd", lat_maneuver),
  PythonProcess("radard", "openpilot.selfdrive.controls.radard", only_onroad),
  PythonProcess("hardwared", "openpilot.system.hardware.hardwared", always_run),
  PythonProcess("modem", "openpilot.common.hardware.comma.modem", always_run, enabled=COMMA_HARDWARE and not LITE),
  PythonProcess("tombstoned", "openpilot.system.tombstoned", always_run, enabled=not PC),
  PythonProcess("updated", "openpilot.system.updated.updated", only_offroad, enabled=not PC),
  PythonProcess("uploader", "openpilot.system.loggerd.uploader", and_(comma_connect, always_run)),

  # debug procs
  NativeProcess("bridge", "openpilot/cereal/messaging", ["./bridge"], notcar),
  PythonProcess("webrtcd", "openpilot.system.webrtc.webrtcd", or_(and_(livestream, not_(iscar)), notcar, opview)),
  PythonProcess("joystick", "openpilot.tools.joystick.joystick_control", and_(joystick, iscar)),

  # dashy
  PythonProcess("serverd", "dragonpilot.dashy.serverd", always_run, enabled=AIOHTTP_AVAILABLE),
  PythonProcess("dashyd", "dragonpilot.dashy.dashyd", and_(dashy, only_onroad)),
]

managed_processes = {p.name: p for p in procs}

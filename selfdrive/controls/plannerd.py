#!/usr/bin/env python3
from cereal import car
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.ldw import LaneDepartureWarning
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner, DPFlags
import cereal.messaging as messaging


def main():
  config_realtime_process(5, Priority.CTRL_LOW)

  cloudlog.info("plannerd is waiting for CarParams")
  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  cloudlog.info("plannerd got CarParams: %s", CP.brand)

  ldw = LaneDepartureWarning()
  longitudinal_planner = LongitudinalPlanner(CP)
  pm = messaging.PubMaster(['longitudinalPlan', 'driverAssistance'])
  sm = messaging.SubMaster(['carControl', 'carState', 'controlsState', 'liveParameters', 'radarState', 'modelV2', 'selfdriveState'],
                           poll='modelV2')

  dp_flags = 0
  dp_curve_speed_reduction = int(params.get("dp_lon_curve_speed_reduction") or 0)
  dp_stop_distance = int(params.get("dp_lon_stop_distance") or 6)

  if params.get_bool("dp_lon_acm"):
    dp_flags |= DPFlags.ACM
  if params.get_bool("dp_lon_aem"):
    dp_flags |= DPFlags.AEM
  if params.get_bool("dp_lon_apm"):
    dp_flags |= DPFlags.APM

  while True:
    sm.update()
    if sm.updated['modelV2']:
      if sm.frame % 20 == 0:
        dp_curve_speed_reduction = int(params.get("dp_lon_curve_speed_reduction") or 0)
        dp_stop_distance = int(params.get("dp_lon_stop_distance") or 6)
      longitudinal_planner.update(sm, dp_flags, dp_curve_speed_reduction, dp_stop_distance)
      longitudinal_planner.publish(sm, pm)

      ldw.update(sm.frame, sm['modelV2'], sm['carState'], sm['carControl'])
      msg = messaging.new_message('driverAssistance')
      msg.valid = sm.all_checks(['carState', 'carControl', 'modelV2', 'liveParameters'])
      msg.driverAssistance.leftLaneDeparture = ldw.left
      msg.driverAssistance.rightLaneDeparture = ldw.right
      pm.send('driverAssistance', msg)


if __name__ == "__main__":
  main()

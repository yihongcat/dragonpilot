import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper


def test_native_modeld_first_frame_call():
  helper = DesireHelper()
  helper.update(car.CarState.new_message(vEgo=15), False, 0.0)
  assert helper.desire == log.Desire.none


@pytest.mark.parametrize('speed,expected', [(6.0, log.LaneChangeState.off), (10.0, log.LaneChangeState.preLaneChange)])
def test_default_lane_change_threshold_uses_twenty_mph(speed, expected):
  helper = DesireHelper()
  helper.update(car.CarState.new_message(vEgo=speed, leftBlinker=True), True, 1.0)
  assert helper.lane_change_state == expected


@pytest.mark.parametrize('edge,blindspot,expected', [
  (False, False, log.LaneChangeState.laneChangeStarting),
  (True, False, log.LaneChangeState.preLaneChange),
  (False, True, log.LaneChangeState.preLaneChange),
])
def test_optional_dp_edge_and_vehicle_blindspot_still_block_lane_change(edge, blindspot, expected):
  helper = DesireHelper(dp_lat_lca_speed=20)
  cs = car.CarState.new_message(vEgo=15, leftBlinker=True, steeringPressed=True,
                               steeringTorque=1, leftBlindspot=blindspot)
  helper.update(cs, True, 1.0, edge, False)
  helper.update(cs, True, 1.0, edge, False)
  assert helper.lane_change_state == expected

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openpilot.common.realtime import DT_MDL
from dragonpilot.selfdrive.controls.lib.longitudinal_extensions import LongitudinalExtensions
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import (
  CURVE_DECEL_JERK,
  MIN_CURVE_SPEED,
  CURVE_THROTTLE_RELEASE_JERK,
  CurveAccelerationController,
  CurveAccelLimit,
  CurveSpeedLimit,
  select_accel_with_curve,
)

CRUISE_SOURCE = "cruise"
E2E_SOURCE = "e2e"
LEAD_SOURCE = "lead"


@pytest.mark.parametrize('message_valid,alive,learned,roll,expected', [
  (True, True, True, 0.04, 0.04),
  (False, True, True, 0.04, 0.0),
  (True, False, True, 0.04, 0.0),
  (True, True, False, 0.04, 0.0),
  (True, True, True, float('nan'), 0.0),
])
def test_curve_bank_requires_live_valid_learned_vehicle_parameters(message_valid, alive, learned, roll, expected):
  class SubMaster(dict):
    pass

  sm = SubMaster(
    carState=SimpleNamespace(vEgo=25.0),
    carControl=SimpleNamespace(cruiseControl=SimpleNamespace(override=False)),
    modelV2=SimpleNamespace(),
    vehicleParameters=SimpleNamespace(valid=learned, roll=roll),
  )
  sm.valid = {'vehicleParameters': message_valid}
  sm.alive = {'vehicleParameters': alive}
  sm.frame = 1
  extensions = LongitudinalExtensions(SimpleNamespace(maxLateralAccel=1.8, longitudinalActuatorDelay=0.5), DT_MDL)
  extensions.curve.update = Mock(return_value=CurveSpeedLimit())
  assert extensions.curve_accel(sm, 50, 30.0, False, -1.0, -1.0) == -1.0
  assert extensions.curve.update.call_args.kwargs['roll'] == expected


def test_curve_acceleration_is_selected_without_requesting_stop():
  base_candidates = [(0.2, CRUISE_SOURCE, False)]
  curve_limit = CurveAccelLimit(-0.5, active=True)

  accel, source, should_stop = select_accel_with_curve(base_candidates, curve_limit, CRUISE_SOURCE)

  assert accel == -0.5
  assert source == CRUISE_SOURCE
  assert not should_stop


def test_curve_does_not_clear_a_real_base_stop_request():
  base_candidates = [(-0.1, E2E_SOURCE, True)]
  curve_limit = CurveAccelLimit(-0.5, active=True)

  _, _, should_stop = select_accel_with_curve(base_candidates, curve_limit, CRUISE_SOURCE)

  assert should_stop


def test_lead_or_e2e_safety_braking_can_bypass_curve_comfort_limit():
  base_candidates = [
    (-2.0, LEAD_SOURCE, False),
    (0.2, CRUISE_SOURCE, False),
  ]
  curve_limit = CurveAccelLimit(-0.5, active=True)

  accel, source, _ = select_accel_with_curve(base_candidates, curve_limit, CRUISE_SOURCE)

  assert accel == -2.0
  assert source == LEAD_SOURCE


def test_final_curve_only_output_respects_apply_jerk():
  controller = CurveAccelerationController()
  requested = CurveSpeedLimit(
    speed=MIN_CURVE_SPEED,
    required_decel=1.2,
    active=True,
    confirmed=True,
    target_speed=MIN_CURVE_SPEED,
  )
  outputs = [0.8]

  for _ in range(50):
    cap = controller.update(requested, 20.0, True, False, outputs[-1])
    base_candidates = [(0.8, CRUISE_SOURCE, False)]
    output, _, should_stop = select_accel_with_curve(base_candidates, cap, CRUISE_SOURCE)
    outputs.append(output)
    assert not should_stop

  output_jerk = -np.diff(outputs) / DT_MDL
  starting_accels = np.asarray(outputs[:-1])
  assert np.max(output_jerk[starting_accels > 0.0]) <= CURVE_THROTTLE_RELEASE_JERK + 1e-9
  assert np.max(output_jerk[starting_accels <= 0.0]) <= CURVE_DECEL_JERK + 1e-9
  assert outputs[-1] >= -1.2

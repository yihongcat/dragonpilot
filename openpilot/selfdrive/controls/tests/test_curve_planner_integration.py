import numpy as np

from openpilot.common.realtime import DT_MDL
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

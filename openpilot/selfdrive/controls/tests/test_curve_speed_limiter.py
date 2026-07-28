import math
from types import SimpleNamespace

import numpy as np

from openpilot.common.constants import CV
from openpilot.selfdrive.modeld.constants import ModelConstants
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import (
  CURVE_DECEL_JERK,
  MAX_CURVE_DECEL,
  MAX_STRENGTH_LAT_ACCEL_FACTOR,
  MIN_CURVE_SPEED,
  NORMAL_CURVE_DECEL,
  RELEASE_ACCEL,
  RELEASE_HOLD_FRAMES,
  CurveSpeedLimiter,
  get_curve_accel,
)


MODEL_SIZE = len(ModelConstants.T_IDXS)


def model_with_curve(curvature: float = 0.0, speed: float = 25.0,
                     curve_start: int = 0, curve_points: int | None = None,
                     spacing: float = 10.0):
  curve_points = MODEL_SIZE - curve_start if curve_points is None else curve_points
  rates = np.zeros(MODEL_SIZE)
  rates[curve_start:curve_start + curve_points] = curvature * speed
  return SimpleNamespace(
    position=SimpleNamespace(
      x=(np.arange(MODEL_SIZE) * spacing).tolist(),
      y=[0.0] * MODEL_SIZE,
    ),
    velocity=SimpleNamespace(x=[speed] * MODEL_SIZE),
    orientationRate=SimpleNamespace(z=rates.tolist()),
  )


def update(limiter, model, strength=100, speed=25.0, cruise=35.0,
           enabled=True, overriding=False):
  return limiter.update(model, speed, cruise, strength, enabled, overriding)


def test_disabled_does_not_limit():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.02), strength=0)
  assert not result.active
  assert math.isinf(result.speed)


def test_strength_uses_vehicle_lateral_accel_without_raising_cap():
  low_cap_limiter = CurveSpeedLimiter(1.3)
  rav4_limiter = CurveSpeedLimiter(1.8)

  assert low_cap_limiter._allowed_lateral_accel(1) == 1.3
  assert np.isclose(low_cap_limiter._allowed_lateral_accel(100), 1.3 * MAX_STRENGTH_LAT_ACCEL_FACTOR)
  assert np.isclose(rav4_limiter._allowed_lateral_accel(100), 1.8 * MAX_STRENGTH_LAT_ACCEL_FACTOR)


def test_higher_strength_reduces_curve_speed():
  model = model_with_curve(0.01, curve_start=8)
  weak = update(CurveSpeedLimiter(1.8), model, strength=20)
  strong = update(CurveSpeedLimiter(1.8), model, strength=80)

  assert weak.active and strong.active
  assert strong.speed < weak.speed


def test_future_curve_limits_speed_while_current_path_is_straight():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.012, curve_start=10))

  assert result.active
  assert limiter.target_distance > 0.0
  assert math.isfinite(result.speed)


def test_nearer_curve_has_lower_current_speed_boundary():
  near = update(CurveSpeedLimiter(1.8), model_with_curve(0.012, curve_start=5))
  far = update(CurveSpeedLimiter(1.8), model_with_curve(0.012, curve_start=15))

  assert near.active and far.active
  assert near.speed < far.speed


def test_boundary_uses_comfortable_deceleration_distance():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.012, curve_start=10), speed=25.0)
  expected = math.sqrt(limiter.target_curve_speed ** 2 + 2.0 * NORMAL_CURVE_DECEL * limiter.target_distance)

  assert np.isclose(result.speed, expected)


def test_future_curve_requests_decel_before_speed_boundary_is_exceeded():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.003, curve_start=15), speed=25.0, cruise=35.0)

  assert result.active
  assert result.speed > 25.0
  assert result.required_decel > 0.0
  assert get_curve_accel(result.required_decel, 0.0) < 0.0


def test_curve_accel_is_jerk_limited_when_applied_and_released():
  applied = get_curve_accel(0.8, 0.0)
  assert np.isclose(applied, -CURVE_DECEL_JERK * 0.05)

  released = get_curve_accel(0.0, -0.5)
  assert np.isclose(released, -0.5 + CURVE_DECEL_JERK * 0.05)


def test_actuator_delay_starts_slowing_earlier():
  model = model_with_curve(0.012, curve_start=10)
  no_delay_limiter = CurveSpeedLimiter(1.8, longitudinal_actuator_delay=0.0)
  delayed_limiter = CurveSpeedLimiter(1.8, longitudinal_actuator_delay=1.0)
  no_delay = update(no_delay_limiter, model)
  delayed = update(delayed_limiter, model)

  assert delayed_limiter.target_distance < no_delay_limiter.target_distance
  assert delayed.speed < no_delay.speed


def test_single_prediction_spike_is_filtered():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.04, curve_start=12, curve_points=1))

  assert not result.active
  assert math.isinf(result.speed)


def test_adjacent_curve_predictions_are_preserved():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.04, curve_start=12, curve_points=2))

  assert result.active
  assert math.isfinite(result.speed)


def test_curve_boundary_has_minimum_speed():
  limiter = CurveSpeedLimiter(1.3)
  result = update(limiter, model_with_curve(1.0), speed=25.0)

  assert result.speed == MIN_CURVE_SPEED == 20.0 * CV.KPH_TO_MS


def test_late_detection_caps_required_deceleration():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.02, curve_start=2), speed=30.0, cruise=35.0)

  assert result.active
  assert result.required_decel == MAX_CURVE_DECEL


def test_driver_override_releases_boundary():
  limiter = CurveSpeedLimiter(1.8)
  assert update(limiter, model_with_curve(0.02)).active

  result = update(limiter, model_with_curve(0.02), overriding=True)
  assert not result.active
  assert math.isinf(result.speed)


def test_curve_release_holds_then_raises_boundary_gradually():
  limiter = CurveSpeedLimiter(1.8)
  active = update(limiter, model_with_curve(0.02))
  straight = model_with_curve()

  held = update(limiter, straight)
  assert held.active
  assert held.speed == 25.0

  for _ in range(RELEASE_HOLD_FRAMES - 1):
    held = update(limiter, straight)
  released = update(limiter, straight)
  assert released.active
  assert np.isclose(released.speed, held.speed + RELEASE_ACCEL * 0.05)
  assert released.speed > active.speed


def test_invalid_plan_does_not_limit():
  model = model_with_curve(0.02)
  model.position.y = model.position.y[:-1]
  result = update(CurveSpeedLimiter(1.8), model)

  assert not result.active
  assert math.isinf(result.speed)

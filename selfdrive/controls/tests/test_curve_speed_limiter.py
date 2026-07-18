import math
from types import SimpleNamespace

import numpy as np

from openpilot.common.constants import CV
from openpilot.selfdrive.modeld.constants import ModelConstants
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import CurveSpeedLimiter, MAX_BOUNDARY_DECEL, MIN_CURVE_SPEED
from openpilot.common.realtime import DT_MDL


def model_with_lateral_accel(lateral_accel: float, speed: float = 25.0):
  return SimpleNamespace(
    velocity=SimpleNamespace(x=[speed] * len(ModelConstants.T_IDXS)),
    orientationRate=SimpleNamespace(z=[lateral_accel / speed] * len(ModelConstants.T_IDXS)),
  )


def activate(limiter, model, strength=50, speed=25.0):
  limiter.update(model, 0.0, speed, strength, True, False)
  return limiter.update(model, 0.0, speed, strength, True, False)


def test_disabled_does_not_limit():
  limiter = CurveSpeedLimiter(2.0)
  assert math.isinf(activate(limiter, model_with_lateral_accel(2.0), strength=0))


def test_higher_strength_reduces_curve_speed():
  model = model_with_lateral_accel(2.5)
  weak_limiter = CurveSpeedLimiter(2.0)
  strong_limiter = CurveSpeedLimiter(2.0)
  weak = activate(weak_limiter, model, strength=20)
  strong = activate(strong_limiter, model, strength=80)
  for _ in range(500):
    weak = weak_limiter.update(model, 0.0, 25.0, 20, True, False)
    strong = strong_limiter.update(model, 0.0, 25.0, 80, True, False)
  assert strong < weak


def test_curve_boundary_has_minimum_speed():
  limiter = CurveSpeedLimiter(0.4)
  model = model_with_lateral_accel(8.0)
  target = activate(limiter, model, strength=100)
  for _ in range(1000):
    target = limiter.update(model, 0.0, 25.0, 100, True, False)
  assert target == MIN_CURVE_SPEED == 20.0 * CV.KPH_TO_MS


def test_single_prediction_spike_is_filtered():
  model = model_with_lateral_accel(1.0)
  rates = np.asarray(model.orientationRate.z)
  rates[-1] = 6.0 / 25.0
  model.orientationRate.z = rates.tolist()
  target = activate(CurveSpeedLimiter(2.0), model)
  assert math.isinf(target)


def test_driver_override_releases_boundary():
  limiter = CurveSpeedLimiter(2.0)
  model = model_with_lateral_accel(2.5)
  assert math.isfinite(activate(limiter, model))
  assert math.isinf(limiter.update(model, 0.0, 25.0, 50, True, True))


def test_curve_boundary_does_not_drop_abruptly():
  limiter = CurveSpeedLimiter(1.0)
  model = model_with_lateral_accel(8.0)
  limiter.update(model, 0.0, 25.0, 100, True, False)
  target = limiter.update(model, 0.0, 25.0, 100, True, False)
  assert target == 25.0 - MAX_BOUNDARY_DECEL * DT_MDL

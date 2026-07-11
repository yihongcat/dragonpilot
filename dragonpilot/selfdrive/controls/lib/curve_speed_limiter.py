import math

import numpy as np

from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL


MIN_CURVE_SPEED = 20.0 * CV.KPH_TO_MS
NO_OVERSHOOT_TIME_HORIZON = 4.0
MAX_BOUNDARY_DECEL = 0.8  # m/s^2, limits how quickly the curve speed boundary can fall

ENTERING_PRED_LAT_ACCEL = 1.3
ABORT_PRED_LAT_ACCEL = 1.1
TURNING_LAT_ACCEL = 1.6
LEAVING_LAT_ACCEL = 1.3
FINISH_LAT_ACCEL = 1.1


class CurveSpeedLimiter:
  """Vision-based curve speed boundary inspired by sunnypilot V-TSC."""

  def __init__(self, max_lateral_accel: float):
    self.max_lateral_accel = max_lateral_accel
    self.state = "disabled"
    self.current_lateral_accel = 0.0
    self.max_predicted_lateral_accel = 0.0
    self.target_speed = math.inf
    self.output_speed = math.inf

  def _allowed_lateral_accel(self, strength: int) -> float:
    # maxLateralAccel varies significantly by platform. Clamp it before using it
    # as the vehicle-specific anchor so weak steering platforms remain usable.
    vehicle_cap = float(np.clip(self.max_lateral_accel, 1.0, 3.0))
    relaxed = min(3.0, max(2.0, vehicle_cap * 1.25))
    restrictive = max(0.8, min(1.5, vehicle_cap * 0.6))
    return float(np.interp(strength, [1, 100], [relaxed, restrictive]))

  def _reset(self):
    self.state = "disabled"
    self.current_lateral_accel = 0.0
    self.max_predicted_lateral_accel = 0.0
    self.target_speed = math.inf
    self.output_speed = math.inf

  def _smooth_target(self, raw_target: float, v_ego: float) -> float:
    previous = v_ego if not math.isfinite(self.output_speed) else self.output_speed
    self.output_speed = max(raw_target, previous - MAX_BOUNDARY_DECEL * DT_MDL)
    return self.output_speed

  def update(self, model, current_curvature: float, v_ego: float,
             strength: int, enabled: bool, overriding: bool) -> float:
    strength = int(np.clip(strength, 0, 100))
    rate_plan = np.asarray(model.orientationRate.z, dtype=float)
    velocity_plan = np.asarray(model.velocity.x, dtype=float)

    valid_plan = len(rate_plan) > 1 and len(rate_plan) == len(velocity_plan)
    if not enabled or strength == 0 or overriding or not valid_plan:
      self._reset()
      return math.inf

    self.current_lateral_accel = abs(current_curvature) * v_ego ** 2
    predicted_lateral_accels = np.abs(rate_plan) * np.maximum(velocity_plan, 0.0)
    self.max_predicted_lateral_accel = float(np.percentile(predicted_lateral_accels, 97))

    if self.state == "disabled":
      self.state = "enabled"
    elif self.state == "enabled" and v_ego > MIN_CURVE_SPEED and self.max_predicted_lateral_accel >= ENTERING_PRED_LAT_ACCEL:
      self.state = "entering"
    elif self.state == "entering":
      if self.current_lateral_accel >= TURNING_LAT_ACCEL:
        self.state = "turning"
      elif self.max_predicted_lateral_accel < ABORT_PRED_LAT_ACCEL:
        self.state = "enabled"
    elif self.state == "turning" and self.current_lateral_accel <= LEAVING_LAT_ACCEL:
      self.state = "leaving"
    elif self.state == "leaving":
      if self.current_lateral_accel >= TURNING_LAT_ACCEL:
        self.state = "turning"
      elif self.current_lateral_accel < FINISH_LAT_ACCEL and self.max_predicted_lateral_accel < ABORT_PRED_LAT_ACCEL:
        self.state = "enabled"

    if self.state not in ("entering", "turning", "leaving"):
      self.target_speed = math.inf
      self.output_speed = math.inf
      return self.target_speed

    v_safe = max(v_ego, 0.1)
    max_predicted_curvature = self.max_predicted_lateral_accel / v_safe ** 2
    allowed_lateral_accel = self._allowed_lateral_accel(strength)
    curve_speed = math.sqrt(allowed_lateral_accel / max(max_predicted_curvature, 1e-4))

    if self.state == "entering":
      boundary_accel = float(np.interp(self.max_predicted_lateral_accel, [1.3, 3.0], [-0.2, -1.0]))
    elif self.state == "turning":
      boundary_accel = float(np.interp(self.current_lateral_accel, [1.5, 2.3, 3.0], [0.5, 0.0, -0.4]))
    else:
      boundary_accel = 0.5

    # This floor only limits this feature. The model can still request a lower speed.
    self.target_speed = max(MIN_CURVE_SPEED, curve_speed + boundary_accel * NO_OVERSHOOT_TIME_HORIZON)
    return self._smooth_target(self.target_speed, v_ego)

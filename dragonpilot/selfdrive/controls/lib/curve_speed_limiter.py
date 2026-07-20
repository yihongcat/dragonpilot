import math
from dataclasses import dataclass

import numpy as np

from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL


MIN_CURVE_SPEED = 20.0 * CV.KPH_TO_MS
NORMAL_CURVE_DECEL = 1.0  # m/s^2, used to decide when to begin slowing on the straight
MAX_CURVE_DECEL = 1.5  # m/s^2, fallback when the model detects a curve late
MIN_MODEL_SPEED = 1.0  # m/s, avoids unstable curvature estimates near a predicted stop
MIN_CURVATURE = 1e-4
MAX_STRENGTH_LAT_ACCEL_FACTOR = 0.6
DEFAULT_MAX_LATERAL_ACCEL = 1.5

CURVATURE_FILTER_SIZE = 3
RELEASE_HOLD_FRAMES = round(0.5 / DT_MDL)
RELEASE_ACCEL = 0.5  # m/s^2, limits how quickly the speed boundary rises after a turn


@dataclass(frozen=True)
class CurveSpeedLimit:
  speed: float = math.inf
  required_decel: float = 0.0
  active: bool = False


class CurveSpeedLimiter:
  """Distance-aware vision curve speed boundary for blended longitudinal control."""

  def __init__(self, max_lateral_accel: float, longitudinal_actuator_delay: float = 0.0):
    self.max_lateral_accel = (float(max_lateral_accel)
                              if math.isfinite(max_lateral_accel) and max_lateral_accel > 0.0
                              else DEFAULT_MAX_LATERAL_ACCEL)
    self.longitudinal_actuator_delay = max(0.0, float(longitudinal_actuator_delay))
    self.allowed_lateral_accel = self.max_lateral_accel
    self.target_curve_speed = math.inf
    self.target_distance = math.inf
    self.output_speed = math.inf
    self.required_decel = 0.0
    self.release_frames = 0

  def _allowed_lateral_accel(self, strength: int) -> float:
    factor = float(np.interp(strength, [1, 100], [1.0, MAX_STRENGTH_LAT_ACCEL_FACTOR]))
    return self.max_lateral_accel * factor

  @staticmethod
  def _filtered_curvatures(rate_plan: np.ndarray, velocity_plan: np.ndarray) -> np.ndarray:
    curvatures = np.zeros_like(rate_plan)
    valid_speed = velocity_plan >= MIN_MODEL_SPEED
    curvatures[valid_speed] = np.abs(rate_plan[valid_speed]) / velocity_plan[valid_speed]

    # A centered median rejects an isolated model spike while preserving a curve
    # represented by two or more adjacent trajectory points, including at the ends.
    padding = CURVATURE_FILTER_SIZE // 2
    padded = np.pad(curvatures, (padding, padding), mode="constant")
    return np.array([np.median(padded[i:i + CURVATURE_FILTER_SIZE]) for i in range(len(curvatures))])

  @staticmethod
  def _distances(position_x: np.ndarray, position_y: np.ndarray) -> np.ndarray:
    segment_distances = np.hypot(np.diff(position_x), np.diff(position_y))
    return np.concatenate(([0.0], np.cumsum(segment_distances)))

  def _reset(self) -> CurveSpeedLimit:
    self.allowed_lateral_accel = self.max_lateral_accel
    self.target_curve_speed = math.inf
    self.target_distance = math.inf
    self.output_speed = math.inf
    self.required_decel = 0.0
    self.release_frames = 0
    return CurveSpeedLimit()

  def _release(self, v_ego: float, v_cruise: float) -> CurveSpeedLimit:
    self.target_curve_speed = math.inf
    self.target_distance = math.inf
    self.required_decel = 0.0
    if not math.isfinite(self.output_speed):
      return CurveSpeedLimit()

    self.release_frames += 1
    self.output_speed = max(self.output_speed, min(v_ego, v_cruise))
    if self.release_frames > RELEASE_HOLD_FRAMES:
      self.output_speed = min(v_cruise, self.output_speed + RELEASE_ACCEL * DT_MDL)

    if self.output_speed >= v_cruise - 0.1:
      return self._reset()
    return CurveSpeedLimit(self.output_speed, active=True)

  def update(self, model, v_ego: float, v_cruise: float,
             strength: int, enabled: bool, overriding: bool) -> CurveSpeedLimit:
    strength = int(np.clip(strength, 0, 100))
    if not enabled or strength == 0 or overriding:
      return self._reset()

    rate_plan = np.asarray(model.orientationRate.z, dtype=float)
    velocity_plan = np.asarray(model.velocity.x, dtype=float)
    position_x = np.asarray(model.position.x, dtype=float)
    position_y = np.asarray(model.position.y, dtype=float)

    plan_length = len(rate_plan)
    valid_plan = (plan_length > 1 and len(velocity_plan) == plan_length and
                  len(position_x) == plan_length and len(position_y) == plan_length and
                  np.all(np.isfinite(rate_plan)) and np.all(np.isfinite(velocity_plan)) and
                  np.all(np.isfinite(position_x)) and np.all(np.isfinite(position_y)))
    if not valid_plan or v_ego <= MIN_CURVE_SPEED or v_cruise <= MIN_CURVE_SPEED:
      return self._reset()

    curvatures = self._filtered_curvatures(rate_plan, velocity_plan)
    distances = self._distances(position_x, position_y)
    effective_distances = np.maximum(0.0, distances - v_ego * (self.longitudinal_actuator_delay + DT_MDL))

    self.allowed_lateral_accel = self._allowed_lateral_accel(strength)
    curve_speeds = np.full(plan_length, math.inf)
    curve_points = curvatures >= MIN_CURVATURE
    curve_speeds[curve_points] = np.maximum(
      MIN_CURVE_SPEED,
      np.sqrt(self.allowed_lateral_accel / curvatures[curve_points]),
    )
    speed_boundaries = np.sqrt(curve_speeds ** 2 + 2.0 * NORMAL_CURVE_DECEL * effective_distances)
    target_idx = int(np.argmin(speed_boundaries))
    raw_target = float(speed_boundaries[target_idx])

    if not math.isfinite(raw_target) or raw_target >= v_cruise:
      return self._release(v_ego, v_cruise)

    self.release_frames = 0
    self.target_curve_speed = float(curve_speeds[target_idx])
    self.target_distance = float(effective_distances[target_idx])

    if v_ego > self.target_curve_speed:
      distance_for_decel = max(self.target_distance, 0.1)
      self.required_decel = float(np.clip(
        (v_ego ** 2 - self.target_curve_speed ** 2) / (2.0 * distance_for_decel),
        0.0,
        MAX_CURVE_DECEL,
      ))
    else:
      self.required_decel = 0.0

    # Tightening is immediate so a late model detection is not delayed. Relaxing
    # an existing boundary is gradual to prevent acceleration pulses through a turn.
    if math.isfinite(self.output_speed) and raw_target > self.output_speed:
      self.output_speed = min(raw_target, self.output_speed + RELEASE_ACCEL * DT_MDL)
    else:
      self.output_speed = raw_target

    return CurveSpeedLimit(self.output_speed, self.required_decel, active=True)

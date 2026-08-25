import math
from dataclasses import dataclass

import numpy as np

from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL


MIN_CURVE_SPEED = 20.0 * CV.KPH_TO_MS
CURVE_BRAKE_MIN_SPEED = 25.0 * CV.KPH_TO_MS
NORMAL_CURVE_DECEL = 0.8  # m/s^2, comfortable deceleration used for the backward speed envelope
MAX_CURVE_DECEL = 1.2  # m/s^2, vision-only curves never request panic braking
CURVE_DECEL_JERK = 0.8  # m/s^3, smoothly applies curve deceleration
CURVE_THROTTLE_RELEASE_JERK = 1.5  # m/s^3, lift throttle promptly before applying gentle braking
CURVE_RELEASE_JERK = 1.0  # m/s^3, smoothly releases curve deceleration
MIN_MODEL_SPEED = 1.0  # m/s, avoids unstable curvature estimates near a predicted stop
MIN_CURVATURE = 1e-4
MIN_REQUIRED_DECEL = 0.1
MAX_STRENGTH_LAT_ACCEL_FACTOR = 0.6
DEFAULT_MAX_LATERAL_ACCEL = 1.5

SPATIAL_SAMPLE_STEP = 2.0  # m, model trajectory points are not evenly spaced
EVENT_CURVATURE_PERCENTILE = 85.0
EVENT_ENTRY_CURVATURE_FRACTION = 0.35
CURVE_CONFIRMATION_FRAMES = 3
CURVE_CONFIRMATION_MIN_TRAVEL = 3.0  # m, rejects features fixed in prediction space
TRACK_WORLD_TOLERANCE = 6.0  # m
TRACK_DISTANCE_TOLERANCE_FACTOR = 0.05
TRACK_MISS_FRAMES = 2
TRACK_PROGRESS_MISS_FRAMES = 2
MIN_APPROACH_RATIO = 0.7
APPROACH_ERROR_TOLERANCE = 5.0  # m, allows for sparse far-horizon model points
APPROACH_ERROR_TOLERANCE_FACTOR = 0.35
PLANNER_RESPONSE_MARGIN = 0.2  # s
POSITIVE_ACCEL_RESPONSE_MARGIN = 0.75  # s, curve cap can start while accelerating
CURVE_ENTRY_MARGIN = 3.0  # m
CURVE_COAST_ACCEL = 0.25  # m/s^2, preview/at-target cap prevents hard re-acceleration
TARGET_SPEED_RELEASE_BUFFER = 0.3  # m/s, stop curve braking before crossing the target speed
RELEASE_HOLD_FRAMES = round(0.5 / DT_MDL)
RELEASE_ACCEL = 0.5  # m/s^2, limits how quickly the speed boundary rises after a turn
CURVE_ACCEL_RELEASE_HOLD_FRAMES = round(0.3 / DT_MDL)
CURVE_BASE_OVERRIDE_HOLD_FRAMES = round(0.5 / DT_MDL)


@dataclass(frozen=True)
class CurveSpeedLimit:
  speed: float = math.inf
  required_decel: float = 0.0
  active: bool = False
  confirmed: bool = False
  unreachable: bool = False
  target_speed: float = math.inf


@dataclass(frozen=True)
class CurveAccelLimit:
  accel: float = math.inf
  active: bool = False


@dataclass(frozen=True)
class _CurveCandidate:
  speed_boundary: float
  curve_speed: float
  distance: float
  effective_distance: float


@dataclass
class _CurveTrack:
  world_anchor: float
  start_ego_distance: float
  start_distance: float
  last_distance: float
  frames: int = 1
  missed_frames: int = 0
  confirmed: bool = False
  candidate: _CurveCandidate | None = None
  progress_misses: int = 0
  progress_frames: int = 0


def get_curve_accel(required_decel: float, previous_accel: float, dt: float = DT_MDL) -> float:
  """Return the jerk-limited acceleration needed to reach the upcoming curve speed."""
  target_accel = -max(0.0, float(required_decel))
  jerk = CURVE_DECEL_JERK if target_accel < previous_accel else CURVE_RELEASE_JERK
  if target_accel < previous_accel and previous_accel > 0.0:
    return float(max(target_accel, 0.0, previous_accel - CURVE_THROTTLE_RELEASE_JERK * dt))
  max_delta = jerk * dt
  return float(np.clip(target_accel, previous_accel - max_delta, previous_accel + max_delta))


def select_accel_with_curve(base_candidates, curve_accel_limit: CurveAccelLimit, curve_source):
  """Select the most conservative acceleration without letting curve request a stop."""
  candidates = list(base_candidates)
  if curve_accel_limit.active:
    candidates.append((curve_accel_limit.accel, curve_source, False))
  output_accel, source, _ = min(candidates, key=lambda candidate: candidate[0])
  should_stop_output = any(stop for _, _, stop in base_candidates)
  return output_accel, source, should_stop_output


class CurveAccelerationController:
  """Applies curve deceleration as an independent cap that can never request a stop."""

  def __init__(self, dt: float = DT_MDL):
    self.dt = dt
    self.accel = math.inf
    self.active = False
    self.release_frames = 0
    self.base_wins_frames = 0
    self.last_target_speed = math.inf

  def _reset(self) -> CurveAccelLimit:
    self.accel = math.inf
    self.active = False
    self.release_frames = 0
    self.base_wins_frames = 0
    self.last_target_speed = math.inf
    return CurveAccelLimit()

  def update(self, curve_limit: CurveSpeedLimit, v_ego: float, enabled: bool,
             overriding: bool, previous_output_accel: float, base_accel: float = 0.0) -> CurveAccelLimit:
    if overriding or not enabled:
      return self._reset()

    has_curve = (
      curve_limit.active and
      math.isfinite(curve_limit.speed) and
      math.isfinite(curve_limit.target_speed)
    )
    release_speed_margin = TARGET_SPEED_RELEASE_BUFFER
    if self.active and self.accel < 0.0:
      release_speed_margin += self.accel ** 2 / (2.0 * CURVE_RELEASE_JERK)

    should_brake = (
      has_curve and curve_limit.confirmed and
      v_ego >= CURVE_BRAKE_MIN_SPEED and
      v_ego > curve_limit.target_speed + release_speed_margin and
      curve_limit.required_decel >= MIN_REQUIRED_DECEL
    )

    if has_curve:
      target_accel = -curve_limit.required_decel if should_brake else CURVE_COAST_ACCEL
      self.last_target_speed = curve_limit.target_speed
      self.base_wins_frames = 0
      if not self.active:
        # Begin at the previous final output so activation is jerk-continuous in either direction.
        initial_accel = float(previous_output_accel) if math.isfinite(previous_output_accel) else target_accel
        self.accel = max(-MAX_CURVE_DECEL, initial_accel)
        self.active = True

      self.release_frames = 0
      if target_accel < self.accel:
        if self.accel > 0.0:
          self.accel = max(target_accel, 0.0, self.accel - CURVE_THROTTLE_RELEASE_JERK * self.dt)
        else:
          self.accel = max(target_accel, self.accel - CURVE_DECEL_JERK * self.dt)
      else:
        self.accel = min(target_accel, self.accel + CURVE_RELEASE_JERK * self.dt)
      if math.isfinite(previous_output_accel):
        recovery_ceiling = max(-MAX_CURVE_DECEL, previous_output_accel + CURVE_RELEASE_JERK * self.dt)
        self.accel = max(-MAX_CURVE_DECEL, min(self.accel, recovery_ceiling))
      return CurveAccelLimit(self.accel, active=True)

    if not self.active:
      return CurveAccelLimit()

    # A short hold prevents one missing model frame from producing an
    # acceleration pulse. Then raise the cap toward the current non-curve output.
    near_target = (
      math.isfinite(self.last_target_speed) and
      v_ego <= self.last_target_speed + release_speed_margin
    )
    self.release_frames += 1
    if not near_target and self.release_frames <= CURVE_ACCEL_RELEASE_HOLD_FRAMES:
      if math.isfinite(previous_output_accel):
        recovery_ceiling = max(-MAX_CURVE_DECEL, previous_output_accel + CURVE_RELEASE_JERK * self.dt)
        self.accel = max(-MAX_CURVE_DECEL, min(self.accel, recovery_ceiling))
      return CurveAccelLimit(self.accel, active=True)

    release_target = max(
      CURVE_COAST_ACCEL,
      float(base_accel) if math.isfinite(base_accel) else 0.0,
    )
    release_ceiling = (
      max(-MAX_CURVE_DECEL, previous_output_accel + CURVE_RELEASE_JERK * self.dt)
      if math.isfinite(previous_output_accel) else release_target
    )
    self.accel = max(-MAX_CURVE_DECEL, min(release_target, self.accel + CURVE_RELEASE_JERK * self.dt, release_ceiling))

    base_is_stronger = math.isfinite(base_accel) and base_accel < self.accel - 1e-3
    if base_is_stronger:
      self.base_wins_frames += 1
    else:
      self.base_wins_frames = 0

    if self.base_wins_frames > CURVE_BASE_OVERRIDE_HOLD_FRAMES:
      return self._reset()
    if self.accel >= release_target - 1e-3 and not base_is_stronger:
      return self._reset()
    return CurveAccelLimit(self.accel, active=True)


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
    self.ego_distance = 0.0
    self.tracks: list[_CurveTrack] = []
    self.confirmation_frames = 0

  def _allowed_lateral_accel(self, strength: int) -> float:
    factor = float(np.interp(strength, [1, 100], [1.0, MAX_STRENGTH_LAT_ACCEL_FACTOR]))
    return self.max_lateral_accel * factor

  @staticmethod
  def _curvatures(rate_plan: np.ndarray, velocity_plan: np.ndarray) -> np.ndarray:
    curvatures = np.zeros_like(rate_plan)
    valid_speed = velocity_plan >= MIN_MODEL_SPEED
    curvatures[valid_speed] = np.abs(rate_plan[valid_speed]) / velocity_plan[valid_speed]
    return curvatures

  @staticmethod
  def _distances(position_x: np.ndarray, position_y: np.ndarray) -> np.ndarray:
    segment_distances = np.hypot(np.diff(position_x), np.diff(position_y))
    return np.concatenate(([0.0], np.cumsum(segment_distances)))

  @classmethod
  def _spatial_curvatures(cls, rate_plan: np.ndarray, velocity_plan: np.ndarray,
                          position_x: np.ndarray, position_y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    distances = cls._distances(position_x, position_y)
    curvatures = cls._curvatures(rate_plan, velocity_plan)

    # np.interp requires increasing x values. Predicted stops can leave repeated
    # trajectory positions near the end, which contain no useful curve distance.
    unique = np.concatenate(([True], np.diff(distances) > 0.1))
    distances = distances[unique]
    curvatures = curvatures[unique]
    if len(distances) < 2 or distances[-1] < 0.1:
      return np.array([]), np.array([])

    sample_distances = np.arange(0.0, distances[-1] + 1e-6, SPATIAL_SAMPLE_STEP)
    if sample_distances[-1] < distances[-1] - 0.1:
      sample_distances = np.append(sample_distances, distances[-1])
    return sample_distances, np.interp(sample_distances, distances, curvatures)

  def _distance_compensation(self, v_ego: float, previous_accel: float) -> float:
    # The constant-deceleration equation assumes the requested acceleration is
    # available immediately. Reserve time for planning, actuator delay, and half
    # of the jerk ramp so the intent begins before the physical braking deadline.
    previous_accel = max(0.0, float(previous_accel)) if math.isfinite(previous_accel) else 0.0
    jerk_equivalent_time = (
      (previous_accel + NORMAL_CURVE_DECEL) ** 2 /
      (2.0 * NORMAL_CURVE_DECEL * CURVE_DECEL_JERK)
    )
    jerk_build_time = max(
      NORMAL_CURVE_DECEL / (2.0 * CURVE_DECEL_JERK) + POSITIVE_ACCEL_RESPONSE_MARGIN,
      jerk_equivalent_time,
    )
    response_time = (self.longitudinal_actuator_delay + DT_MDL + PLANNER_RESPONSE_MARGIN +
                     jerk_build_time)
    return v_ego * response_time + CURVE_ENTRY_MARGIN

  def _curve_candidates(self, distances: np.ndarray, curvatures: np.ndarray,
                        v_ego: float, v_cruise: float, previous_accel: float) -> list[_CurveCandidate]:
    threshold = max(MIN_CURVATURE, self.allowed_lateral_accel / max(v_cruise, MIN_CURVE_SPEED) ** 2)
    curve_indices = np.flatnonzero(curvatures >= threshold)
    if len(curve_indices) == 0:
      return []

    groups = np.split(curve_indices, np.flatnonzero(np.diff(curve_indices) > 1) + 1)
    distance_compensation = self._distance_compensation(v_ego, previous_accel)
    candidates = []
    for group in groups:
      event_curvatures = curvatures[group]
      event_curvature = float(np.percentile(event_curvatures, EVENT_CURVATURE_PERCENTILE))
      if event_curvature < MIN_CURVATURE:
        continue

      entry_points = group[event_curvatures >= event_curvature * EVENT_ENTRY_CURVATURE_FRACTION]
      entry_idx = int(entry_points[0])
      distance = float(distances[entry_idx])
      effective_distance = max(0.0, distance - distance_compensation)
      curve_speed = max(MIN_CURVE_SPEED, math.sqrt(self.allowed_lateral_accel / event_curvature))
      speed_boundary = math.sqrt(curve_speed ** 2 + 2.0 * NORMAL_CURVE_DECEL * effective_distance)
      candidates.append(_CurveCandidate(speed_boundary, curve_speed, distance, effective_distance))
    return candidates

  @staticmethod
  def _track_tolerance(distance: float) -> float:
    return max(TRACK_WORLD_TOLERANCE, TRACK_DISTANCE_TOLERANCE_FACTOR * distance)

  def _update_tracks(self, candidates: list[_CurveCandidate]) -> tuple[_CurveCandidate | None, _CurveTrack | None]:
    for track in self.tracks:
      track.missed_frames += 1
      track.candidate = None

    available_tracks = set(range(len(self.tracks)))
    for candidate in sorted(candidates, key=lambda item: item.distance):
      world_position = self.ego_distance + candidate.distance
      tolerance = self._track_tolerance(candidate.distance)
      matches = [
        (abs(self.tracks[index].world_anchor - world_position), index)
        for index in available_tracks
        if abs(self.tracks[index].world_anchor - world_position) <= tolerance
      ]

      if matches:
        _, track_index = min(matches)
        track = self.tracks[track_index]
        available_tracks.remove(track_index)
        track.frames += 1
        track.missed_frames = 0
        track.last_distance = candidate.distance
        track.candidate = candidate

        traveled = self.ego_distance - track.start_ego_distance
        approached = track.start_distance - candidate.distance
        approach_tolerance = max(APPROACH_ERROR_TOLERANCE,
                                 APPROACH_ERROR_TOLERANCE_FACTOR * traveled)
        progress_matches = (
          traveled >= CURVE_CONFIRMATION_MIN_TRAVEL and
          approached >= MIN_APPROACH_RATIO * traveled and
          abs(approached - traveled) <= approach_tolerance
        )
        if progress_matches:
          track.progress_misses = 0
          track.progress_frames += 1
          if track.progress_frames >= CURVE_CONFIRMATION_FRAMES:
            track.confirmed = True
        elif traveled >= CURVE_CONFIRMATION_MIN_TRAVEL:
          track.progress_misses += 1
          track.progress_frames = 0
          if track.progress_misses >= TRACK_PROGRESS_MISS_FRAMES:
            track.confirmed = False
      else:
        self.tracks.append(_CurveTrack(
          world_anchor=world_position,
          start_ego_distance=self.ego_distance,
          start_distance=candidate.distance,
          last_distance=candidate.distance,
          candidate=candidate,
        ))

    self.tracks = [track for track in self.tracks if track.missed_frames <= TRACK_MISS_FRAMES]
    visible_tracks = [track for track in self.tracks if track.candidate is not None]
    if not visible_tracks:
      self.confirmation_frames = 0
      return None, None

    confirmed_tracks = [track for track in visible_tracks if track.confirmed]
    selectable_tracks = confirmed_tracks if confirmed_tracks else visible_tracks
    selected = min(selectable_tracks, key=lambda track: track.candidate.speed_boundary)
    self.confirmation_frames = selected.progress_frames
    return selected.candidate, selected

  def _clear_tracks(self) -> None:
    self.ego_distance = 0.0
    self.tracks = []
    self.confirmation_frames = 0

  def _reset(self) -> CurveSpeedLimit:
    self.allowed_lateral_accel = self.max_lateral_accel
    self.target_curve_speed = math.inf
    self.target_distance = math.inf
    self.output_speed = math.inf
    self.required_decel = 0.0
    self.release_frames = 0
    self._clear_tracks()
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
      self.output_speed = math.inf
      self.release_frames = 0
      return CurveSpeedLimit()
    return CurveSpeedLimit(self.output_speed, active=True)

  def update(self, model, v_ego: float, v_cruise: float,
             strength: int, enabled: bool, overriding: bool,
             previous_accel: float = 0.0) -> CurveSpeedLimit:
    strength = int(np.clip(strength, 0, 100))
    lane_change_state = getattr(getattr(model, "meta", None), "laneChangeState", log.LaneChangeState.off)
    lane_changing = lane_change_state in (log.LaneChangeState.laneChangeStarting,
                                          log.LaneChangeState.laneChangeFinishing)

    if not enabled or strength == 0 or overriding or lane_changing:
      return self._reset()

    rate_plan = np.asarray(model.orientationRate.z, dtype=float)
    velocity_x = np.asarray(model.velocity.x, dtype=float)
    velocity_y = np.asarray(getattr(model.velocity, "y", np.zeros_like(velocity_x)), dtype=float)
    velocity_plan = np.hypot(velocity_x, velocity_y)
    position_x = np.asarray(model.position.x, dtype=float)
    position_y = np.asarray(model.position.y, dtype=float)

    plan_length = len(rate_plan)
    valid_plan = (plan_length > 1 and len(velocity_x) == plan_length and len(velocity_y) == plan_length and
                  len(position_x) == plan_length and len(position_y) == plan_length and
                  np.all(np.isfinite(rate_plan)) and np.all(np.isfinite(velocity_x)) and
                  np.all(np.isfinite(velocity_y)) and
                  np.all(np.isfinite(position_x)) and np.all(np.isfinite(position_y)))
    valid_speeds = math.isfinite(v_ego) and math.isfinite(v_cruise)
    if not valid_plan or not valid_speeds or v_ego <= MIN_CURVE_SPEED or v_cruise <= MIN_CURVE_SPEED:
      return self._reset()

    self.ego_distance += v_ego * DT_MDL
    self.allowed_lateral_accel = self._allowed_lateral_accel(strength)
    distances, curvatures = self._spatial_curvatures(
      rate_plan, velocity_plan, position_x, position_y,
    )
    candidates = self._curve_candidates(distances, curvatures, v_ego, v_cruise, previous_accel)
    limiting_candidates = [
      candidate for candidate in candidates
      if math.isfinite(candidate.speed_boundary) and candidate.speed_boundary < v_cruise
    ]
    candidate, track = self._update_tracks(limiting_candidates)
    if candidate is None or track is None:
      return self._release(v_ego, v_cruise)

    raw_target = candidate.speed_boundary
    self.release_frames = 0
    self.target_curve_speed = candidate.curve_speed
    self.target_distance = candidate.effective_distance
    confirmed = track.confirmed

    raw_required_decel = 0.0
    if v_ego > self.target_curve_speed:
      distance_for_decel = max(self.target_distance, 0.1)
      raw_required_decel = (v_ego ** 2 - self.target_curve_speed ** 2) / (2.0 * distance_for_decel)

    unreachable = raw_required_decel > MAX_CURVE_DECEL
    self.required_decel = float(np.clip(raw_required_decel, 0.0, MAX_CURVE_DECEL)) if confirmed else 0.0

    # Tightening is immediate so a late model detection is not delayed. Relaxing
    # an existing boundary is gradual to prevent acceleration pulses through a turn.
    if math.isfinite(self.output_speed) and raw_target > self.output_speed:
      self.output_speed = min(raw_target, self.output_speed + RELEASE_ACCEL * DT_MDL)
    else:
      self.output_speed = raw_target

    return CurveSpeedLimit(
      self.output_speed,
      self.required_decel,
      active=True,
      confirmed=confirmed,
      unreachable=unreachable,
      target_speed=self.target_curve_speed,
    )

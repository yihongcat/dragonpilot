"""Optional DP longitudinal behavior. Native Autospeed does not instantiate this."""
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from dragonpilot.selfdrive.controls.lib.acm import ACM
from dragonpilot.selfdrive.controls.lib.aem import AEM
from dragonpilot.selfdrive.controls.lib.apm import APM
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import CurveAccelerationController, CurveSpeedLimiter


class DPFlags:
  ACM = 1
  AEM = 2
  APM = 4


class LongitudinalExtensions:
  def __init__(self, CP, dt):
    self.acm = ACM()
    self.aem = AEM()
    self.apm = APM()
    self.curve = CurveSpeedLimiter(CP.maxLateralAccel, CP.longitudinalActuatorDelay)
    self.curve_controller = CurveAccelerationController(dt)
    self.curve_active = False

  def personality(self, flags, sm):
    personality = sm['selfdriveState'].personality
    return self.apm.get_personality(sm['carState'].vEgo, personality) if flags & DPFlags.APM else personality

  def coasting(self, flags, sm, v_cruise, trajectory):
    self.acm.enabled = bool(flags & DPFlags.ACM)
    if self.acm.enabled:
      self.acm.update_states(sm['carControl'], sm['radarState'],
                             sm['controlsState'].longControlState == LongCtrlState.off, sm['carState'].vEgo, v_cruise)
      return self.acm.update_a_desired_trajectory(trajectory)
    return trajectory

  def candidates(self, flags, sm, candidates):
    if flags & DPFlags.AEM:
      self.aem.update_states(sm['modelV2'], sm['radarState'], sm['carState'].vEgo)
      if self.aem.get_mode('blended') == 'acc':
        return candidates[:2]
    return candidates

  def curve_accel(self, sm, strength, v_cruise, reset_state, base_accel, previous_accel):
    v_ego = sm['carState'].vEgo
    override = sm['carControl'].cruiseControl.override
    limit = self.curve.update(sm['modelV2'], v_ego, v_cruise, strength, not reset_state, override, previous_accel)
    cap = self.curve_controller.update(limit, v_ego, not reset_state, override, previous_accel, base_accel)
    if strength > 0 and (limit.active != self.curve_active or getattr(sm, 'frame', 0) % 20 == 0):
      cloudlog.info('dp_curve_speed active=%s cap_active=%s strength=%d v_ego=%.2f distance=%.2f required_decel=%.2f accel=%.2f',
                    limit.active, cap.active, strength, v_ego, self.curve.target_distance, limit.required_decel, cap.accel)
    self.curve_active = limit.active
    # A curve cap never changes Autospeed's shouldStop decision or source.
    return min(base_accel, cap.accel) if cap.active else base_accel

from opendbc.car.structs import car

from dragonpilot.selfdrive.controls.lib.acm import ACM
from openpilot.cereal import messaging


def make_inputs(lead_present: bool, d_rel: float = 50.0, v_lead: float = 20.0):
  cc = car.CarControl.new_message()
  cc.orientationNED = [0.0, 0.0, 0.0]

  radar_state = messaging.new_message("radarState").radarState
  radar_state.leadOne.present = lead_present
  radar_state.leadOne.dRel = d_rel
  radar_state.leadOne.vLead = v_lead
  return cc, radar_state


def test_absent_lead_uses_present_field_without_crashing():
  acm = ACM()
  acm.enabled = True
  cc, radar_state = make_inputs(False)

  acm.update_states(cc, radar_state, user_ctrl_lon=False, v_ego=25.0, v_cruise=20.0)

  assert acm.active
  assert not acm._has_lead


def test_present_emergency_lead_disables_acm():
  acm = ACM()
  acm.enabled = True
  acm.active = True
  cc, radar_state = make_inputs(True, d_rel=10.0, v_lead=5.0)

  acm.update_states(cc, radar_state, user_ctrl_lon=False, v_ego=25.0, v_cruise=20.0)

  assert not acm.active

from opendbc.car.structs import car
from openpilot.cereal import log
from dragonpilot.dashy.dashyd import extract_live_tracks


class RadarMessages(dict):
  valid = {'radarState': True}


def test_current_radar_schema_keeps_unmatched_moving_tracks():
  tracks = car.RadarData.new_message(points=[
    {'trackId': 1, 'dRel': 20, 'yRel': 1, 'vRel': -2},
    {'trackId': 2, 'dRel': 25, 'yRel': -1, 'vRel': -3},
    {'trackId': 3, 'dRel': 30, 'yRel': 2, 'vRel': 0},
  ])
  state = log.RadarState.new_message()
  state.leadOne.present = True
  state.leadOne.radarTrackId = 1
  messages = RadarMessages(radarTracks=tracks, radarState=state)
  assert extract_live_tracks(messages) == {'points': [{'d': 25.0, 'y': -1.0, 'v': -3.0}]}

from dragonpilot.settings import tr


ITEMS = [
  {
    "section": "Longitudinal",
    "key": "dp_lon_curve_lookahead_seconds",
    "type": "spin_button_item",
    "title": lambda: tr("Curve Prediction Time"),
    "description": lambda: tr(
      "How far ahead to inspect the predicted path for curves. Longer times allow earlier slowing but include less certain distant predictions. " +
      "The current model predicts up to 10 seconds. Requires DP Longitudinal Extensions and Curve Speed Reduction."
    ),
    "default": "6",
    "min_val": 1,
    "max_val": 10,
    "step": 1,
    "suffix": "s",
    "condition": "openpilotLongitudinalControl",
    "depends_on": "dp_lon_extensions_enabled == 1",
    "flags": "PERSISTENT",
    "param_type": "INT",
  },
  {
    "section": "Longitudinal",
    "key": "dp_lon_curve_speed_reduction",
    "type": "spin_button_item",
    "title": lambda: tr("Curve Speed Reduction"),
    "description": lambda: tr(
      "Uses the model's predicted path, curve distance, road bank when available, and the vehicle's lateral acceleration limit to slow before turns. " +
      "Higher values keep more margin below the vehicle limit. Requires DP Longitudinal Extensions; Off leaves Autospeed behavior unchanged."
    ),
    "default": "0",
    "min_val": 0,
    "max_val": 100,
    "step": 10,
    "suffix": "%",
    "special_value_text": lambda: tr("Off"),
    "condition": "openpilotLongitudinalControl",
    "depends_on": "dp_lon_extensions_enabled == 1",
    "flags": "PERSISTENT",
    "param_type": "INT",
  },
]

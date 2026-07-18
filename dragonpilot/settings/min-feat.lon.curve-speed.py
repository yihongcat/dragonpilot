from dragonpilot.settings import tr


ITEMS = [
  {
    "section": "Longitudinal",
    "key": "dp_lon_curve_speed_reduction",
    "type": "spin_button_item",
    "title": lambda: tr("Curve Speed Reduction"),
    "description": lambda: tr("Uses the model's predicted path and vehicle steering capability to limit speed through turns. Higher values slow down more. Only affects End-to-End longitudinal; Off leaves model behavior unchanged."),
    "default": "0",
    "min_val": 0,
    "max_val": 100,
    "step": 10,
    "suffix": "%",
    "special_value_text": lambda: tr("Off"),
    "condition": "openpilotLongitudinalControl",
    "flags": "PERSISTENT",
    "param_type": "INT",
  },
]

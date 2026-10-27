from dragonpilot.settings import tr


ITEMS = [
  {
    "section": "Longitudinal",
    "key": "dp_lon_stop_distance",
    "type": "spin_button_item",
    "title": lambda: tr("Aggressive Stop Distance"),
    "description": lambda: tr(
      "Sets the Aggressive personality's desired gap to a stopped lead vehicle. Standard and Relaxed receive the same "
      + "distance adjustment while keeping their existing moving time-gap differences."
    ),
    "default": "6",
    "min_val": 1,
    "max_val": 10,
    "step": 1,
    "suffix": lambda: tr("m"),
    "condition": "openpilotLongitudinalControl",
    "depends_on": "dp_lon_extensions_enabled == 1",
    "flags": "PERSISTENT",
    "param_type": "INT",
  },
]

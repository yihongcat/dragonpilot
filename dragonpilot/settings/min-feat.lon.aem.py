from dragonpilot.settings import tr

ITEMS = [
  {
    "section": "Longitudinal",
    "key": "dp_lon_aem",
    "type": "toggle_item",
    "title": lambda: tr("Adaptive Experimental Mode (AEM)"),
    "description": lambda: tr("Adaptive mode switcher between ACC and Blended based on driving context."),
    "condition": "openpilotLongitudinalControl",
    "depends_on": "dp_lon_extensions_enabled == 1",
    "flags": "PERSISTENT",
    "param_type": "BOOL",
    "default": "0",
  },
]

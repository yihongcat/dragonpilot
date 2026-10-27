from dragonpilot.settings import tr

ITEMS = [{
  "section": "Longitudinal",
  "key": "dp_lon_extensions_enabled",
  "type": "toggle_item",
  "title": lambda: tr("DP Longitudinal Extensions"),
  "description": lambda: tr("Off uses native openpilot Autospeed. Enable to test optional DP coasting, adaptive modes, curve slowing and stop distance settings."),
  "condition": "openpilotLongitudinalControl",
  "flags": "PERSISTENT",
  "param_type": "BOOL",
  "default": "0",
}]

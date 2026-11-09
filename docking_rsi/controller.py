"""Legacy wire profile over the shared RSI controller; no domain scheduler."""
from rsi_lab.controller import *  # noqa: F401,F403
from rsi_lab.controller import Controller as _Controller


class Controller(_Controller):
    schema_version = 'docking_control_v2'

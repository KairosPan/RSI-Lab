"""Legacy module alias for the replaceable docking capability package."""
import sys
from rsi_domains.docking import domain as _implementation
sys.modules[__name__] = _implementation

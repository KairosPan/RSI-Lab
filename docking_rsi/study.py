"""Compatibility import for the replaceable domain package study adapter.

Existing frozen plans and public call signatures remain readable. Generic
criteria live in rsi_lab.plans; raw domain interpretation stays in its package.
"""
from rsi_domains.docking.study import *  # noqa: F401,F403
from rsi_domains.docking import study as _implementation


def __getattr__(name):
    return getattr(_implementation, name)

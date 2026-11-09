"""Compatibility schema for historical packets; implementation is task neutral."""
from rsi_lab.packets import *
from rsi_lab.packets import PacketBuilder as _PacketBuilder


class PacketBuilder(_PacketBuilder):
    def __init__(self, evidence_catalog, max_input_bytes=128*1024, *, page_bytes=4096):
        super().__init__(evidence_catalog,max_input_bytes,page_bytes=page_bytes,
                         envelope_schema='docking_rsi_model_request_v2')

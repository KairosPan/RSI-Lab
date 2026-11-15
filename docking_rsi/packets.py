"""Compatibility schema for historical packets; implementation is task neutral."""
from rsi_lab.packets import *
from rsi_lab.packets import PacketBuilder as _PacketBuilder
from rsi_lab.images import MAX_TOTAL_IMAGE_BYTES, MAX_IMAGES


class PacketBuilder(_PacketBuilder):
    def __init__(self, evidence_catalog, max_input_bytes=128*1024, *, page_bytes=4096,
                 max_image_bytes=MAX_TOTAL_IMAGE_BYTES, max_images=MAX_IMAGES):
        super().__init__(evidence_catalog,max_input_bytes,page_bytes=page_bytes,
                         max_image_bytes=max_image_bytes, max_images=max_images,
                         envelope_schema='docking_rsi_model_request_v2')

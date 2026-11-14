"""Version 1 lossless BGR stream used by the LeRobot camera plugin."""
import struct

HEADER = struct.Struct('<4sQdIIII')


def frame_packet(sequence, timestamp, frame):
    height, width, channels = frame.shape
    payload = frame.tobytes()
    return HEADER.pack(b'RBFR', sequence, timestamp, width, height, channels, len(payload)) + payload

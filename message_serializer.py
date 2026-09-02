# message_serializer.py - Outbound message validation and serialization
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Outbound message validation and serialization for the inter-core bus.

Only JSON-safe values, string dict keys, and finite floats; the serialized message must not exceed MAX_OUTBOUND_MESSAGE_BYTES.
"""

import json
import math

from message_protocol import is_json_safe


# Maximum outbound MQTT payload bytes (16 KiB).
#
# MCU-scale ceiling. The limit is enforced after json.dumps() + utf-8 encode, so
# at peak allocation the object graph, the serialized str, and the encoded bytes
# are all resident at once; a 128 KiB payload could not be admitted safely on a
# Pico W (256 KiB SRAM, 64 KiB reserved). 16 KiB keeps a single message's transient
# peak (graph + str + bytes, ~3x) to roughly 48 KiB and leaves ~6x headroom over the
# largest legitimate message (the one-shot startup log, which is the only payload that
# grows with device count). The aggregate retained footprint is separately governed
# by the global minimum free-heap reserve that both inter-core queues enforce at
# admission (the reserve itself is owned by hardware.py).
MAX_OUTBOUND_MESSAGE_BYTES = 16 * 1024


# Transient working set a serializer needs free while json.dumps() builds the
# JSON string and .encode() builds the UTF-8 bytes on top of the (still-owned)
# message graph, before that transient memory is released: at peak the graph,
# the str, and the encoded bytes are all resident at once -- ~3x the wire
# ceiling, per the rationale above (a 16 KiB payload, roughly 48 KiB). Kept
# separate from the survival reserve (minimum_free_heap_bytes in hardware.py):
# the reserve is operational headroom that must survive, the working set is the
# temporary room a serialization needs in addition to it. A producer that must
# serialize (the outbound queue's put()) therefore requires
# minimum_free_heap_bytes + SERIALIZATION_HEADROOM_BYTES free before it
# serializes, so a peak allocation can never dip the free heap below the
# reserve into a MemoryError while eviction could still make room.
SERIALIZATION_HEADROOM_BYTES = 3 * MAX_OUTBOUND_MESSAGE_BYTES


class SerializationError(Exception):
    """Base exception for serialization failures."""
    pass


class UnsupportedValueError(SerializationError):
    """A value type is not supported in outbound messages."""
    pass


class NonStringKeyError(SerializationError):
    """A dictionary key is not a string."""
    pass


class NonFiniteFloatError(SerializationError):
    """A float value is not finite (NaN or Infinity)."""
    pass


class MessageTooLargeError(SerializationError):
    """The serialized message exceeds the maximum payload size."""
    pass


def _validate_value(value, path="root"):
    """Recursively validate a value (path is reported in errors); raises UnsupportedValueError, NonStringKeyError, or NonFiniteFloatError."""
    if value is None:
        return

    if isinstance(value, bool):
        # Must check bool before int since bool is a subclass of int
        return

    if isinstance(value, int):
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteFloatError("Non-finite float at {}: {}".format(path, value))
        return

    if isinstance(value, str):
        return

    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            item_path = "{}[{}]".format(path, index)
            _validate_value(item, item_path)
        return

    if isinstance(value, dict):
        for key, val in value.items():
            # Check that keys are strings
            if not isinstance(key, str):
                raise NonStringKeyError("Non-string key at {}: {}".format(path, key))
            key_path = "{}[{}]".format(path, repr(key))
            _validate_value(val, key_path)
        return

    # Unsupported type
    raise UnsupportedValueError(
        "Unsupported type at {}: {} (got {})".format(path, value, type(value).__name__)
    )


def _serialize_to_bytes(message):
    """Serialize a validated message to UTF-8 bytes; raises SerializationError on failure."""
    try:
        serialized = json.dumps(message)
        return serialized.encode("utf-8")
    except MemoryError:
        raise
    except Exception as err:
        raise SerializationError("JSON serialization failed: {}".format(err))


def serialize_and_validate_message(message):
    """
    Validate a message and serialize it to UTF-8 bytes for queue admission.

    is_json_safe() runs first as an allocation-light pass; the path-producing validator runs only on failure. Raises UnsupportedValueError, NonStringKeyError, NonFiniteFloatError, MessageTooLargeError, or SerializationError.
    """
    # Validate structure and values. is_json_safe() enforces the same rules
    # as _validate_value() but without building per-node diagnostic paths, so
    # the common valid-message case stays allocation-light. On failure the
    # path-producing validator re-walks the message and raises the precise
    # error (with the offending path) the caller reports.
    if not is_json_safe(message):
        _validate_value(message)

    # Serialize to bytes
    payload_bytes = _serialize_to_bytes(message)

    # Check size
    if len(payload_bytes) > MAX_OUTBOUND_MESSAGE_BYTES:
        raise MessageTooLargeError(
            "Message size {} exceeds maximum {}".format(
                len(payload_bytes), MAX_OUTBOUND_MESSAGE_BYTES
            )
        )

    return payload_bytes


def get_max_message_bytes():
    """Return the maximum outbound message payload size in bytes."""
    return MAX_OUTBOUND_MESSAGE_BYTES

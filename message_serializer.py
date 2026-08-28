# message_serializer.py - Outbound message validation and serialization
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""
Outbound message validation and serialization for the inter-core bus.

This module provides pre-admission validation of messages that will be
serialized, UTF-8 encoded, and stored as immutable bytes in the outbound
queue.

Validation rules:
- Only JSON-safe values are permitted (dict, list, tuple, str, int, float, bool, None)
- Dictionary keys must be strings
- Float values must be finite (no NaN, Infinity, -Infinity)
- Messages must not exceed MAX_OUTBOUND_MESSAGE_BYTES when serialized
"""

import json


# Maximum outbound MQTT payload bytes (128KB)
MAX_OUTBOUND_MESSAGE_BYTES = 128 * 1024


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


def _is_finite_float(value):
    """Check if a float value is finite (not NaN or Infinity)."""
    if not isinstance(value, float):
        return True
    # Check for NaN (NaN is not equal to itself)
    if value != value:
        return False
    # Check for Infinity (comparison with large float works on Pico)
    if value == float("inf") or value == -float("inf"):
        return False
    return True


def _validate_value(value, path="root"):
    """
    Recursively validate a value for JSON serialization.

    Args:
        value: The value to validate
        path: Current path in the data structure (for error messages)

    Raises:
        UnsupportedValueError: If the value type is not supported
        NonStringKeyError: If a dict key is not a string
        NonFiniteFloatError: If a float is NaN or Infinity
    """
    if value is None:
        return

    if isinstance(value, bool):
        # Must check bool before int since bool is a subclass of int
        return

    if isinstance(value, int):
        return

    if isinstance(value, float):
        if not _is_finite_float(value):
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
    """
    Serialize a validated message to UTF-8 bytes.

    Args:
        message: A validated message dictionary

    Returns:
        UTF-8 encoded JSON bytes

    Raises:
        SerializationError: If serialization fails
    """
    try:
        serialized = json.dumps(message)
        return serialized.encode("utf-8")
    except MemoryError:
        raise
    except Exception as err:
        raise SerializationError("JSON serialization failed: {}".format(err))


def serialize_and_validate_message(message):
    """
    Validate a message and serialize it to bytes for queue admission.

    This function performs all required validations before serialization:
    1. Recursively validate all value types
    2. Ensure all dict keys are strings
    3. Ensure all floats are finite
    4. Serialize to JSON
    5. Encode to UTF-8
    6. Check size against MAX_OUTBOUND_MESSAGE_BYTES

    Args:
        message: A message dictionary to validate and serialize

    Returns:
        UTF-8 encoded JSON bytes ready for queue admission

    Raises:
        UnsupportedValueError: If the message contains unsupported value types
        NonStringKeyError: If the message contains non-string dict keys
        NonFiniteFloatError: If the message contains NaN or Infinity floats
        MessageTooLargeError: If the serialized message exceeds the max size
        SerializationError: If JSON serialization fails
    """
    # Validate structure and values
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

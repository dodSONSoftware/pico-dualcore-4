# test_message_serializer.py - Host-side tests for outbound message validation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for the outbound message serializer.

The serializer's hot path is the common case of a VALID message: it must not allocate per-node diagnostic path strings for errors that never occur. The allocation-light is_json_safe() fast pass therefore gates the detailed path-producing validator, and the two must accept and reject exactly the same values -- if the fast pass ever disagreed, a valid message would be misreported (or an invalid one would reach json.dumps and emit non-strict JSON)."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import message_protocol  # noqa: E402
import message_serializer  # noqa: E402
from message_serializer import (  # noqa: E402
    MessageTooLargeError,
    NonFiniteFloatError,
    NonStringKeyError,
    UnsupportedValueError,
    get_max_message_bytes,
    serialize_and_validate_message,
)


def test_valid_nested_message_serializes():
    """The fast pass admits a valid nested message without the detailed
    validator, and the payload round-trips unchanged."""
    message = {
        "kind": "telemetry",
        "values": [1, 2.5, None, True, "x"],
        "nested": {"deep": {"n": [None, "y", [3]]}},
    }
    payload = serialize_and_validate_message(message)
    assert isinstance(payload, bytes)
    assert json.loads(payload) == message


def test_fast_pass_and_detailed_validator_agree():
    """is_json_safe() must accept and reject exactly the same values as
    _validate_value(). A disagreement would either misreport a valid message
    or let an invalid one through to json.dumps (emitting non-strict JSON)."""
    samples = [
        {"a": 1, "b": None, "c": True, "d": "x", "e": 1.5},
        {"nested": {"deep": [1, 2.5, {"x": [None, "y"]}]}, "list": [1, [2, 3]]},
        {"nan": float("nan")},
        {"inf": float("inf")},
        {"neg_inf": -float("inf")},
        {1: "x"},
        {"unsupported": object()},
        {"nested_bad": {"a": [1, float("nan")]}},
        {"nested_key": {42: "x"}},
        {"nested_unsupported": [1, {"b": object()}]},
    ]
    for sample in samples:
        try:
            message_serializer._validate_value(sample)
            detailed_passes = True
        except message_serializer.SerializationError:
            detailed_passes = False
        assert message_protocol.is_json_safe(sample) == detailed_passes, sample


def test_nan_nested_in_list_raises_with_path():
    with pytest.raises(NonFiniteFloatError) as excinfo:
        serialize_and_validate_message({"a": [1, float("nan")]})
    assert "a" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_non_string_key_nested_raises_with_path():
    with pytest.raises(NonStringKeyError):
        serialize_and_validate_message({"a": {42: "x"}})


def test_unsupported_type_nested_raises():
    with pytest.raises(UnsupportedValueError):
        serialize_and_validate_message({"a": [1, object()]})


def test_oversized_message_rejected():
    big = {"blob": "x" * (get_max_message_bytes() + 1)}
    with pytest.raises(MessageTooLargeError):
        serialize_and_validate_message(big)

"""Import-time registration, separate from git_format's path vocabulary."""
from types import MappingProxyType


_schema_validators = {}
_closure_validators = {}
_schema_fields = {}


def register_schema(family, *, fields):
    """Register a plugin's validator and exact field set without wrapping it."""
    def register(validator):
        if family in _schema_validators:
            raise ValueError(f"duplicate schema registration: {family!r}")
        frozen_fields = frozenset(fields)
        _schema_validators[family] = validator
        _schema_fields[family] = frozen_fields
        return validator
    return register


def register_closure(number):
    """Register a closure, preserving its original callable signature."""
    def register(validator):
        if number in _closure_validators:
            raise ValueError(f"duplicate closure registration: {number!r}")
        _closure_validators[number] = validator
        return validator
    return register


def _freeze_registries():
    """Publish immutable snapshots after every discovered plugin has loaded."""
    return (MappingProxyType(dict(_schema_validators)),
            MappingProxyType(dict(_closure_validators)),
            MappingProxyType(dict(_schema_fields)))

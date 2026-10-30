"""Import-time registration, separate from git_format's path vocabulary."""
from types import MappingProxyType


# Fixed fixture coverage for R1 closures 1/5, independent of future plugins.
R1_CLOSURE_FAMILIES = (
    "input_candidate", "operation", "consumed_candidate", "event",
)


_schema_validators = {}
_closure_validators = {}
_schema_fields = {}
_sample_builders = {}
SCHEMA_BUILDERS = MappingProxyType({})


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


def register_sample(family):
    """Register a minimal payload builder after its schema, without wrapping it.

    Builders return a fresh dict without a path. Optional keyword arguments
    override path identities only; callers supply their own repo/company scope.
    Business fields and defaults belong exclusively to the family plugin.
    """
    def register(builder):
        if family in _sample_builders:
            raise ValueError(f"duplicate sample registration: {family!r}")
        if family not in _schema_fields:
            raise ValueError(f"sample without schema registration: {family!r}")
        if not callable(builder):
            raise TypeError("sample builder must be callable")
        _sample_builders[family] = builder
        return builder
    return register


def get_sample_builder(family, default=None):
    """Look up a builder in the published discovery snapshot."""
    return SCHEMA_BUILDERS.get(family, default)


def _freeze_registries():
    """Publish immutable snapshots after every discovered plugin has loaded."""
    global SCHEMA_BUILDERS
    SCHEMA_BUILDERS = MappingProxyType(dict(_sample_builders))
    return (MappingProxyType(dict(_schema_validators)),
            MappingProxyType(dict(_closure_validators)),
            MappingProxyType(dict(_schema_fields)), SCHEMA_BUILDERS)

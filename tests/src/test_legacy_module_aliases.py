"""A module reachable under two names must be one module, not two.

`backend.api.saved_result_evolution` is kept as an alias for the feature
module. Two separate module objects would mean two sets of module-level state,
and a monkeypatch or a cache in one would not be seen through the other.
"""


def test_legacy_import_is_same_module_state():
    from backend.api import saved_result_evolution as legacy
    from backend.features.evaluation import saved_result_evolution as feature
    assert legacy is feature

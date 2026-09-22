from backend.api import features


def test_core_features_are_explicit_and_always_available():
    result = features.catalog()

    assert {item["name"] for item in result["core"]} == set(features.CORE_FEATURES)
    assert all(item["stability"] == "stable" for item in result["core"])
    assert all(item["available"] for item in result["core"])


def test_experimental_feature_can_report_a_runtime_failure():
    result = features.catalog({"evolve": (False, "Missing optimizer package: dspy")})
    evolve = next(item for item in result["experimental"] if item["name"] == "evolve")

    assert evolve["available"] is False
    assert evolve["install_extra"] == "optimizers"
    assert "dspy" in evolve["unavailable_reason"]

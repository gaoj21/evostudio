from backend.features.execution.token_usage import (attach_usage, release_usage,
                                                    reported_usage, usage_key)


def test_missing_usage_is_unknown_and_reported_usage_normalizes():
    assert reported_usage(None) is None
    assert reported_usage({'prompt_tokens':12,'completion_tokens':3}) == {'input_tokens':12,'output_tokens':3,'total_tokens':15}
    assert reported_usage({'input_tokens':True,'output_tokens':3}) is None


def test_a_state_gets_one_hook_and_the_package_usage_reaches_it():
    from llm import LLMResult, LLMUsage
    from backend.features import model_bridge
    state = {}
    key = usage_key(state)
    assert usage_key(state) == key                       # registered once
    model_bridge._report(key, LLMResult(content='hi', usage=LLMUsage(12, 3, 15)))
    assert state['token_usage']['total_tokens'] == 15
    assert state['token_usage']['reported_calls'] == 1
    # A call that reported nothing is not a zero.
    model_bridge._report(key, LLMResult(content='hi'))
    assert state['token_usage']['reported_calls'] == 1
    release_usage(state)
    model_bridge._report(key, LLMResult(content='hi', usage=LLMUsage(1, 1, 2)))
    assert state['token_usage']['total_tokens'] == 15


def test_a_model_the_framework_built_is_pointed_at_the_run_s_hook():
    from llm import LLMResult, LLMUsage
    from backend.features import model_bridge
    state = {}
    key = usage_key(state)
    model_class, config_class = model_bridge.classes()
    rebuilt = model_class(config=config_class())             # a rebuild with no key
    assert rebuilt.usage_key is None
    attach_usage(rebuilt, key)
    assert rebuilt.usage_key == key and rebuilt.config.usage_key == key
    model_bridge._report(rebuilt.usage_key, LLMResult(content='hi', usage=LLMUsage(2, 1, 3)))
    assert state['token_usage']['total_tokens'] == 3
    assert attach_usage(object(), key) is not None           # a stub is left alone
    release_usage(state)

"""Unit tests for the model-only API (demo/frontend/model_api.py): its settings, the allow-listed request for
vLLM, the SSE pass-through and the single answer put together for callers that do not stream. The API itself, on
loopback with a fake GPU link, is covered by test_demo_model_api_server.py and test_demo_model_api_chat.py."""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
try:
    import app as frontend
    import model_api as api
finally:
    del sys.path[:1]

KEY = "unit-test-key"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
SETTINGS = api.Settings(key_hashes=(api.key_digest(KEY),))
TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Weather of a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


def build(payload, profile="fp8", **settings):
    config = dataclasses.replace(SETTINGS, **settings) if settings else SETTINGS
    return api.build_request(payload, config, profile)


def ask(content="hi", **extra):
    return {"model": "ax-k2-fp8", "messages": [{"role": "user", "content": content}], **extra}


def nested(levels):
    value = {}
    for _ in range(levels):
        value = {"a": value}
    return value


class Checks(unittest.TestCase):
    def refuse(self, payload, status, code, param=None, *, profile="fp8", **settings):
        with self.assertRaises(api.ApiError) as caught:
            build(payload, profile, **settings)
        error = caught.exception
        self.assertEqual((error.status, error.code), (status, code), error.message)
        if param is not None:
            self.assertEqual(error.param, param)
        return error


class SettingsTests(unittest.TestCase):
    def read(self, **env):
        return api.Settings.from_env({"AXK2_MODEL_API_" + name.upper(): value for name, value in env.items()})

    def test_no_valid_key_digest_means_no_api(self):
        self.assertFalse(api.Settings().enabled)
        self.assertFalse(api.Settings.from_env({}).enabled)
        for text in (None, "", "not-a-digest", "abc", "g" * 64, "a" * 63, "a" * 65, ", ;"):
            self.assertFalse(self.read(key_sha256=text).enabled, text)
        self.assertTrue(SETTINGS.enabled)

    def test_key_digests(self):
        other = api.key_digest("other")
        self.assertEqual(api.key_digest("abc"), ABC_SHA256)
        self.assertEqual(api.digests(ABC_SHA256), (ABC_SHA256,))
        self.assertEqual(api.digests(f"{ABC_SHA256},{other}"), (ABC_SHA256, other))
        self.assertEqual(api.digests(f" sha256:{ABC_SHA256.upper()} ;\n{other}\t{ABC_SHA256}"), (ABC_SHA256, other))
        self.assertEqual(api.digests(f"{ABC_SHA256[:-1]},plain-key,{ABC_SHA256}x"), ())

    def test_defaults(self):
        settings = api.Settings()
        self.assertEqual((settings.name_fp8, settings.name_nvfp4), ("ax-k2-fp8", "ax-k2-nvfp4"))
        self.assertEqual((settings.aliases_fp8, settings.aliases_nvfp4), (("skt/A.X-K2",), ("skt/A.X-K2-NVFP4",)))
        self.assertEqual((settings.max_active, settings.max_queue, settings.rate_per_minute), (4, 8, 60))
        self.assertEqual((settings.max_tokens, settings.default_max_tokens), (8192, 4096))
        # the environment reader and the dataclass agree on every default
        self.assertEqual(self.read(key_sha256=ABC_SHA256), api.Settings(key_hashes=(ABC_SHA256,)))

    def test_every_setting_can_be_set(self):
        settings = self.read(
            key_sha256=ABC_SHA256, name_fp8="my-fp8", name_nvfp4="my-nvfp4", aliases_fp8="one/a, two/b",
            aliases_nvfp4="three/c\nfour/d", max_active="2", max_queue="3", rate_per_minute="10", max_tokens="1000",
            default_max_tokens="500", max_body_bytes="5000", max_messages="7", max_tools="2",
            max_schema_bytes="2048", queue_timeout="1.5", timeout="20")
        self.assertEqual(settings, api.Settings(
            key_hashes=(ABC_SHA256,), name_fp8="my-fp8", name_nvfp4="my-nvfp4", aliases_fp8=("one/a", "two/b"),
            aliases_nvfp4=("three/c", "four/d"), max_active=2, max_queue=3, rate_per_minute=10, max_tokens=1000,
            default_max_tokens=500, max_body_bytes=5000, max_messages=7, max_tools=2, max_schema_bytes=2048,
            queue_timeout=1.5, timeout=20.0))

    def test_numbers_are_clamped_and_garbage_falls_back(self):
        self.assertEqual(self.read(max_active="many").max_active, 4)
        self.assertEqual(self.read(max_active="2.5").max_active, 4)
        self.assertEqual(self.read(max_active="0").max_active, 1)
        self.assertEqual(self.read(max_active="-3").max_active, 1)
        self.assertEqual(self.read(max_active="100000").max_active, 256)
        self.assertEqual(self.read(max_queue="0").max_queue, 0)
        self.assertEqual(self.read(max_tokens="0").max_tokens, 1)
        self.assertEqual(self.read(queue_timeout="nan").queue_timeout, 30.0)
        self.assertEqual(self.read(timeout="inf").timeout, 300.0)
        self.assertEqual(self.read(timeout="0").timeout, 1.0)
        self.assertEqual(self.read(timeout="999999").timeout, 3600.0)

    def test_unusable_names_fall_back(self):
        self.assertEqual(self.read(name_fp8="").name_fp8, "ax-k2-fp8")
        self.assertEqual(self.read(aliases_fp8="x" * 201).aliases_fp8, ("skt/A.X-K2",))
        self.assertEqual(self.read(aliases_fp8="a/b,\x07").aliases_fp8, ("skt/A.X-K2",))
        self.assertEqual(self.read(aliases_fp8="x,x, y").aliases_fp8, ("x", "y"))

    def test_names_for_a_profile(self):
        self.assertEqual(SETTINGS.names_for("fp8"), ("ax-k2-fp8", "skt/A.X-K2"))
        self.assertEqual(SETTINGS.names_for("nvfp4"), ("ax-k2-nvfp4", "skt/A.X-K2-NVFP4"))

    def test_the_app_config_reads_them(self):
        config = frontend.Config.from_env({"AXK2_MODEL_API_KEY_SHA256": ABC_SHA256})
        self.assertEqual(config.model_api.key_hashes, (ABC_SHA256,))
        self.assertFalse(frontend.Config.from_env({}).model_api.enabled)
        self.assertFalse(frontend.Config().model_api.enabled)


class ErrorTests(unittest.TestCase):
    def test_openai_error_object(self):
        error = api.ApiError(400, "invalid_value", "Bad.", param="messages")
        self.assertEqual(error.body(), {"error": {"message": "Bad.", "type": "invalid_request_error",
                                                  "param": "messages", "code": "invalid_value"}})
        self.assertIsNone(api.ApiError(404, "x", "m").body()["error"]["param"])

    def test_error_type_follows_the_status(self):
        kinds = {status: api.ApiError(status, "x", "m").kind for status in (400, 401, 404, 413, 429, 500, 502, 503, 504)}
        self.assertEqual(kinds, {400: "invalid_request_error", 401: "invalid_request_error",
                                 404: "invalid_request_error", 413: "invalid_request_error",
                                 429: "rate_limit_error", 500: "server_error", 502: "server_error",
                                 503: "server_error", 504: "server_error"})

    def test_response(self):
        response = api.busy().response()
        self.assertEqual((response.status_code, response.headers["retry-after"]), (429, "5"))
        self.assertEqual(json.loads(response.body)["error"]["code"], "capacity_exceeded")
        response = api.unavailable().response()
        self.assertEqual((response.status_code, response.headers["retry-after"]), (503, "30"))
        self.assertEqual(api.timed_out().status, 504)
        self.assertEqual((api.internal_error().status, api.internal_error().code), (500, "internal_error"))

    def test_only_validation_messages_of_vllm_are_passed_on(self):
        text = json.dumps({"error": {"message": "This model's maximum context length is 262144 tokens."}})
        too_long = api.upstream_failure(400, text)
        self.assertEqual((too_long.status, too_long.code, too_long.param), (400, "context_length_exceeded", "messages"))
        self.assertIn("maximum context length", too_long.message)
        rejected = api.upstream_failure(422, '{"detail": "bad tool schema"}')
        self.assertEqual((rejected.status, rejected.code), (400, "invalid_request"))
        for status in (429, 503):
            busy = api.upstream_failure(status, "overloaded")
            self.assertEqual((busy.status, busy.code, busy.headers), (503, "backend_unavailable", {"Retry-After": "5"}))
        broken = api.upstream_failure(500, "Traceback (most recent call last): File /opt/vllm/secret.py")
        self.assertEqual((broken.status, broken.code), (502, "upstream_error"))
        self.assertNotIn("Traceback", broken.message)
        self.assertNotIn("secret", broken.message)


class BuildRequestTests(Checks):
    def test_the_upstream_body_is_built_from_an_allow_list(self):
        spec = build({"model": "ax-k2-fp8", "messages": [{"role": "system", "content": "Be brief."},
                                                         {"role": "user", "content": "Weather?"}],
                      "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 50,
                      "temperature": 0.2, "top_p": 0.5, "stop": ["END", ""], "seed": 7, "stream": True,
                      "stream_options": {"include_usage": True}, "response_format": {"type": "text"}})
        self.assertEqual(spec.body, {
            "model": "axk2", "messages": [{"role": "system", "content": "Be brief."},
                                          {"role": "user", "content": "Weather?"}],
            "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 50, "temperature": 0.2, "top_p": 0.5,
            "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": False, "stop": ["END"], "seed": 7})
        self.assertEqual((spec.stream, spec.include_usage, spec.dropped, spec.model),
                         (True, True, [], "ax-k2-fp8"))

    def test_defaults_match_the_demo_chat(self):
        spec = build(ask())
        self.assertEqual(spec.body, {
            "model": "axk2", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 4096, "temperature": 0.6, "top_p": 0.95})
        self.assertEqual((spec.stream, spec.include_usage), (False, False))  # upstream always streams

    def test_vllm_extensions_never_reach_the_upstream_body(self):
        hostile = {
            "chat_template_kwargs": {"enable_thinking": True}, "guided_json": {"type": "object"},
            "guided_regex": ".*", "guided_choice": ["a"], "guided_grammar": "x", "priority": -100, "top_k": 1,
            "min_p": 0.5, "repetition_penalty": 2, "use_beam_search": True, "best_of": 8, "echo": True,
            "add_generation_prompt": False, "continue_final_message": True, "skip_special_tokens": False,
            "ignore_eos": True, "min_tokens": 99, "prompt_logprobs": 5, "logits_processors": ["x"],
            "request_id": "x", "kv_transfer_params": {"x": 1}, "documents": [{"x": 1}], "chat_template": "{{ x }}",
            "mm_processor_kwargs": {}, "bad_words": ["x"], "truncate_prompt_tokens": 1, "length_penalty": 1,
            "stop_token_ids": [1], "structured_outputs": {"json": {}}, "thinking": {"type": "enabled"},
            "enable_thinking": True, "reasoning_effort": "high"}
        spec = build(ask(**hostile))
        self.assertEqual(set(spec.body), {"model", "messages", "stream", "stream_options", "chat_template_kwargs",
                                          "max_tokens", "temperature", "top_p"})
        self.assertEqual(spec.body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(spec.body["stream_options"], {"include_usage": True})
        self.assertEqual(spec.dropped, sorted(hostile)[:20])

    def test_stream_options_of_the_caller_only_decide_the_answer(self):
        spec = build(ask(stream=True, stream_options={"include_usage": False, "continuous_usage_stats": True}))
        self.assertEqual(spec.body["stream_options"], {"include_usage": True})
        self.assertEqual((spec.stream, spec.include_usage), (True, False))
        self.assertTrue(build(ask(stream=True, stream_options={"include_usage": True})).include_usage)
        self.assertFalse(build(ask(stream=True, stream_options={"include_usage": "yes"})).include_usage)
        self.assertFalse(build(ask(stream=False, stream_options={"include_usage": True})).include_usage)

    def test_stream_flags_are_validated(self):
        self.refuse(ask(stream="true"), 400, "invalid_value", "stream")
        self.refuse(ask(stream=1), 400, "invalid_value", "stream")
        self.refuse(ask(stream=True, stream_options=True), 400, "invalid_value", "stream_options")
        self.assertFalse(build(ask(stream=None)).stream)

    def test_foundry_style_request_is_accepted(self):
        spec = build(ask(
            tools=[dict(TOOL, function=dict(TOOL["function"], strict=True))], tool_choice="auto",
            parallel_tool_calls=True, reasoning_effort="medium", response_format={"type": "text"},
            max_completion_tokens=256, stream=True, stream_options={"include_usage": True}, store=False,
            metadata={"run": "r1"}, user="agent-7", service_tier="auto", prompt_cache_key="k"))
        self.assertEqual(spec.dropped, ["metadata", "prompt_cache_key", "reasoning_effort", "service_tier",
                                        "store", "user"])
        self.assertEqual((spec.body["max_tokens"], spec.body["tools"], spec.body["parallel_tool_calls"]),
                         (256, [TOOL], True))
        self.assertNotIn("strict", json.dumps(spec.body))

    def test_harmless_fields_are_dropped_and_only_their_names_are_kept(self):
        spec = build(ask(user="alice@example.com", metadata={"k": "secret-value"}, store=True))
        self.assertEqual(spec.dropped, ["metadata", "store", "user"])
        self.assertNotIn("alice", json.dumps([spec.dropped, spec.body]))
        self.assertNotIn("secret-value", json.dumps([spec.dropped, spec.body]))
        spec = build(ask(**{"k" * 100: 1, "a\x00b": 2}))
        self.assertEqual(spec.dropped, ["ab", "k" * 40])
        self.assertEqual(len(build(ask(**{f"extra{i:02d}": i for i in range(30)})).dropped), 20)

    def test_token_limits(self):
        self.assertEqual(build(ask(max_tokens=100_000)).body["max_tokens"], 8192)
        self.assertEqual(build(ask(max_tokens=7)).body["max_tokens"], 7)
        self.assertEqual(build(ask(max_completion_tokens=50, max_tokens=100)).body["max_tokens"], 50)
        self.assertEqual(build(ask(max_completion_tokens=None, max_tokens=9)).body["max_tokens"], 9)
        self.assertEqual(build(ask(max_tokens=16.0)).body["max_tokens"], 16)
        self.assertEqual(build(ask(), max_tokens=100).body["max_tokens"], 100)  # the default is clamped as well
        self.assertEqual(build(ask(max_tokens=500), max_tokens=100).body["max_tokens"], 100)
        for name in ("max_tokens", "max_completion_tokens"):
            for value in (0, -1, "5", True, 1.5, [5]):
                self.refuse(ask(**{name: value}), 400, "invalid_value", name)

    def test_sampling_parameters(self):
        body = build(ask(temperature=0, top_p=1)).body
        self.assertEqual((body["temperature"], body["top_p"]), (0, 1))
        self.assertEqual(build(ask(top_p=0)).body["top_p"], 1e-6)
        self.assertEqual(build(ask(temperature=2)).body["temperature"], 2)
        for name, values in (("temperature", (-0.1, 2.1, "0.5", True, [])), ("top_p", (-0.1, 1.1, "0.5", False))):
            for value in values:
                self.refuse(ask(**{name: value}), 400, "invalid_value", name)

    def test_stop_and_seed(self):
        self.assertEqual(build(ask(stop="END")).body["stop"], ["END"])
        self.assertEqual(build(ask(stop=["a", "b"])).body["stop"], ["a", "b"])
        self.assertNotIn("stop", build(ask(stop=[""])).body)
        self.assertNotIn("stop", build(ask(stop=None)).body)
        for stop in (["a"] * 5, [1], {"a": 1}, 5, "x" * 257):
            self.refuse(ask(stop=stop), 400, "invalid_value", "stop")
        self.assertEqual(build(ask(seed=-3)).body["seed"], -3)
        for seed in ("1", 1.5, True, 2 ** 63):
            self.refuse(ask(seed=seed), 400, "invalid_value", "seed")

    def test_messages_are_cleaned(self):
        spec = build({"model": "ax-k2-fp8", "messages": [
            {"role": "developer", "content": "Be brief."},
            {"role": "user", "name": "ann", "content": [{"type": "text", "text": "Hello"},
                                                         {"type": "input_text", "text": "there"}]},
            {"role": "assistant", "content": None, "refusal": None, "function_call": None,
             "tool_calls": [{"id": "call_1", "type": "function", "index": 0,
                             "function": {"name": "get_weather", "arguments": {"city": "서울"}}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": [{"type": "text", "text": "맑음"}]},
            {"role": "assistant", "content": "Sunny.", "tool_calls": []}]})
        self.assertEqual(spec.body["messages"], [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hello\nthere"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "get_weather", "arguments": '{"city": "서울"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "맑음"},
            {"role": "assistant", "content": "Sunny."}])

    def test_tool_call_arguments(self):
        def calls(arguments):
            call = {"id": "c", "type": "function", "function": {"name": "f", "arguments": arguments}}
            messages = build({"model": "ax-k2-fp8", "messages": [
                {"role": "user", "content": "x"}, {"role": "assistant", "content": "", "tool_calls": [call]}]})
            return messages.body["messages"][1]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(calls('{"a": 1}'), '{"a": 1}')
        self.assertEqual(calls({"a": [1, 2]}), '{"a": [1, 2]}')
        self.assertEqual(calls(""), "{}")
        self.assertEqual(calls(None), "{}")

    def test_messages_are_required(self):
        for payload in ({"model": "ax-k2-fp8"}, {"model": "ax-k2-fp8", "messages": []},
                        {"model": "ax-k2-fp8", "messages": "hi"}, {"model": "ax-k2-fp8", "messages": {"role": "user"}}):
            self.refuse(payload, 400, "missing_required_parameter", "messages")
        three = {"model": "ax-k2-fp8", "messages": [{"role": "user", "content": "a"}] * 3}
        self.refuse(three, 400, "invalid_value", "messages", max_messages=2)
        self.assertEqual(len(build(three, max_messages=3).body["messages"]), 3)

    def test_message_problems_name_the_offending_field(self):
        call = {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        cases = [
            ("messages[0]", ["text"]),
            ("messages[0].role", [{"role": "function", "content": "x"}]),
            ("messages[0].role", [{"content": "x"}]),
            ("messages[0].content", [{"role": "user", "content": 5}]),
            ("messages[0].content", [{"role": "user", "content": {"type": "text", "text": "x"}}]),
            ("messages[0].content", [{"role": "user"}]),
            ("messages[1].tool_call_id", [{"role": "user", "content": "x"}, {"role": "tool", "content": "x"}]),
            ("messages[1].tool_call_id", [{"role": "user", "content": "x"},
                                          {"role": "tool", "tool_call_id": 5, "content": "x"}]),
            ("messages[0].tool_calls", [{"role": "assistant", "tool_calls": "x"}]),
            ("messages[0].tool_calls[0]", [{"role": "assistant", "tool_calls": [dict(call, type="custom")]}]),
            ("messages[0].tool_calls[0]", [{"role": "assistant", "tool_calls": ["x"]}]),
            ("messages[0].tool_calls[0].id", [{"role": "assistant", "tool_calls": [{"type": "function",
                                                                                   "function": {"name": "f"}}]}]),
            ("messages[0].tool_calls[0].function.name",
             [{"role": "assistant", "tool_calls": [dict(call, function={"name": "bad name!"})]}]),
            ("messages[0].tool_calls[0].function.arguments",
             [{"role": "assistant", "tool_calls": [dict(call, function={"name": "f", "arguments": "{nope"})]}]),
            ("messages[0].tool_calls[0].function.arguments",
             [{"role": "assistant", "tool_calls": [dict(call, function={"name": "f", "arguments": 5})]}]),
        ]
        for param, messages in cases:
            with self.subTest(param=param, messages=messages):
                self.refuse({"model": "ax-k2-fp8", "messages": messages}, 400, "invalid_value", param)

    def test_images_audio_and_files_are_refused(self):
        for part in ({"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                     {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
                     {"type": "file", "file": {"file_id": "f"}}, {"type": "text"}, "text"):
            payload = ask([{"type": "text", "text": "look"}, part])
            self.refuse(payload, 400, "unsupported_value", "messages[0].content[1]")

    def test_tools(self):
        spec = build(ask(tools=[{"function": {"name": "f", "strict": True, "extra": 1}},
                                {"type": "function", "function": {"name": "g", "description": "G",
                                                                  "parameters": {"type": "object"}}}]))
        self.assertEqual(spec.body["tools"], [
            {"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "g", "description": "G", "parameters": {"type": "object"}}}])
        self.assertEqual(spec.body["tool_choice"], "auto")
        for tools in (None, []):
            body = build(ask(tools=tools, tool_choice="required", parallel_tool_calls=True)).body
            self.assertTrue(not {"tools", "tool_choice", "parallel_tool_calls"} & set(body))

    def test_tool_problems(self):
        too_deep = {"type": "function", "function": {"name": "f", "parameters": nested(40)}}
        for tools, code, param in [
                ("x", "invalid_value", "tools"),
                ([{"type": "web_search"}], "unsupported_value", "tools[0]"),
                ([{"type": "function"}], "unsupported_value", "tools[0]"),
                (["x"], "unsupported_value", "tools[0]"),
                ([{"type": "function", "function": {"name": "bad name"}}], "invalid_value", "tools[0].function.name"),
                ([{"type": "function", "function": {"name": "f", "description": 5}}], "invalid_value",
                 "tools[0].function.description"),
                ([{"type": "function", "function": {"name": "f", "parameters": []}}], "invalid_value",
                 "tools[0].function.parameters"),
                ([too_deep], "invalid_value", "tools[0].function.parameters")]:
            with self.subTest(tools=tools):
                self.refuse(ask(tools=tools), 400, code, param)
        self.refuse(ask(tools=[TOOL, TOOL]), 400, "invalid_value", "tools", max_tools=1)
        self.refuse(ask(tools=[TOOL]), 400, "invalid_value", "tools", max_schema_bytes=100)
        build(ask(tools=[TOOL, TOOL]), max_tools=2)

    def test_tool_choice(self):
        named = {"type": "function", "function": {"name": "get_weather"}}
        for choice, expected in [(None, "auto"), ("auto", "auto"), ("none", "none"), ("required", "required"),
                                 (named, named), ({"function": {"name": "get_weather"}}, named)]:
            self.assertEqual(build(ask(tools=[TOOL], tool_choice=choice)).body["tool_choice"], expected)
        for choice in ("any", "get_weather", 3, {}, {"type": "function", "function": {"name": "other"}},
                       {"type": "custom", "function": {"name": "get_weather"}}):
            self.refuse(ask(tools=[TOOL], tool_choice=choice), 400, "invalid_value", "tool_choice")

    def test_parallel_tool_calls(self):
        for flag in (True, False):
            self.assertEqual(build(ask(tools=[TOOL], parallel_tool_calls=flag)).body["parallel_tool_calls"], flag)
        self.assertNotIn("parallel_tool_calls", build(ask(tools=[TOOL])).body)
        self.refuse(ask(tools=[TOOL], parallel_tool_calls="yes"), 400, "invalid_value", "parallel_tool_calls")

    def test_unsupported_features_are_a_400(self):
        for field, value in [("n", 2), ("n", "1"), ("n", 0), ("logprobs", True), ("top_logprobs", 3),
                             ("logit_bias", {"1": 5}), ("functions", [{"name": "f"}]), ("modalities", ["audio"]),
                             ("modalities", "text"), ("audio", {"voice": "x"}),
                             ("response_format", {"type": "json_object"}),
                             ("response_format", {"type": "json_schema", "json_schema": {"name": "x"}}),
                             ("response_format", "text")]:
            with self.subTest(field=field, value=value):
                self.refuse(ask(**{field: value}), 400, "unsupported_value", field)
        build(ask(n=1, logprobs=False, top_logprobs=None, logit_bias={}, functions=[], modalities=["text"],
                  response_format={"type": "text"}))
        build(ask(n=None, logprobs=None, audio=None, response_format=None))

    def test_top_logprobs_zero_is_accepted_as_sdks_send_it(self):
        plain = build(ask()).body
        for extra in ({"top_logprobs": 0}, {"logprobs": False, "top_logprobs": 0},
                      {"logprobs": None, "top_logprobs": None}):
            with self.subTest(extra=extra):
                self.assertEqual(build(ask(**extra)).body, plain)  # accepted, and not passed on

    def test_top_logprobs_above_zero_and_logprobs_stay_a_400(self):
        self.refuse(ask(top_logprobs=1), 400, "unsupported_value", "top_logprobs")
        self.refuse(ask(logprobs=False, top_logprobs=1), 400, "unsupported_value", "top_logprobs")
        self.refuse(ask(logprobs=True), 400, "unsupported_value", "logprobs")
        self.refuse(ask(logprobs=True, top_logprobs=0), 400, "unsupported_value", "logprobs")

    def test_model_names(self):
        for name in ("ax-k2-fp8", "skt/A.X-K2", "  AX-K2-FP8 ", "SKT/a.x-k2"):
            spec = build(dict(ask(), model=name))
            self.assertEqual((spec.model, spec.body["model"]), ("ax-k2-fp8", "axk2"), name)
        for name in ("ax-k2-nvfp4", "skt/A.X-K2-NVFP4"):
            self.assertEqual(build(dict(ask(), model=name), "nvfp4").model, "ax-k2-nvfp4")
        self.assertEqual(build(dict(ask(), model="custom"), name_fp8="custom").model, "custom")

    def test_unknown_model_names_are_a_404(self):
        for payload, profile in [(dict(ask(), model="gpt-4o"), "fp8"), (dict(ask(), model="ax-k2-nvfp4"), "fp8"),
                                 (dict(ask(), model="skt/A.X-K2-NVFP4"), "fp8"),
                                 (dict(ask(), model="ax-k2-fp8"), "nvfp4"), (dict(ask(), model="skt/A.X-K2"), "nvfp4"),
                                 (dict(ask(), model=""), "fp8"), ({"model": "nope"}, "fp8")]:
            error = self.refuse(payload, 404, "model_not_found", "model", profile=profile)
            self.assertEqual(error.kind, "invalid_request_error")
        long_name = self.refuse(dict(ask(), model="x" * 300), 404, "model_not_found", "model")
        self.assertLess(len(long_name.message), 200)

    def test_the_model_is_required_and_a_string(self):
        self.refuse({"messages": ask()["messages"]}, 400, "missing_required_parameter", "model")
        self.refuse(dict(ask(), model=None), 400, "missing_required_parameter", "model")
        for value in (5, ["ax-k2-fp8"], {"id": "ax-k2-fp8"}, True):
            self.refuse(dict(ask(), model=value), 400, "invalid_value", "model")


class ParseJsonTests(unittest.TestCase):
    def test_objects_only(self):
        self.assertEqual(api.parse_json(b'{"a": 1}'), {"a": 1})
        self.assertEqual(api.parse_json('{"a": "\\u00e9"}'), {"a": "\u00e9"})

    def test_everything_else_is_invalid_json(self):
        for raw in (b"", b"{", b"not json", b"[1]", b'"text"', b"5", b"null", b'{"a": NaN}', b'{"a": Infinity}',
                    b'{"a": -Infinity}', b"\xff\xfe\x00", b"[" * 100_000 + b"]" * 100_000):
            with self.assertRaises(api.ApiError) as caught:
                api.parse_json(raw)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid_json"), raw[:20])

    def test_depth(self):
        self.assertTrue(api.depth(nested(10)))
        self.assertTrue(api.depth({"a": [1] * 100_000}))
        self.assertFalse(api.depth(nested(40)))
        self.assertFalse(api.depth([[[]]], limit=2))


def chunk(delta=None, finish=None, **extra):
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "axk2",
            "choices": [{"index": 0, "delta": {} if delta is None else delta, "finish_reason": finish}], **extra}


def calls(index, **function):
    return {"tool_calls": [{"index": index, "function": function}]}


USAGE = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "axk2", "choices": [],
         "usage": {"prompt_tokens": 11, "total_tokens": 14, "completion_tokens": 3}}


def sse(*items):
    """vLLM's wire format: one `data: <json>` event per chunk."""
    return "".join("data: " + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n\n"
                   for item in items)


STREAM = sse(chunk({"role": "assistant", "content": ""}), chunk({"content": "Hel"}), chunk({"content": "lo"}),
             chunk({}, "stop"), USAGE, "[DONE]")


def run(text, keep_usage=True, size=None):
    """(bytes sent on, parsed chunks, Events, Context) for `text`, fed whole or in pieces of `size` characters."""
    ctx = api.Context("rid", "fp8")
    events = api.Events(ctx)
    pieces = [text] if size is None else [text[i:i + size] for i in range(0, len(text), size)]
    sent, parsed = [], []
    for piece in pieces:
        for raw, found in events.feed(piece, keep_usage):
            if raw is not None:
                sent.append(raw)
            if found is not None:
                parsed.append(found)
    return b"".join(sent), parsed, events, ctx


class EventsTests(unittest.TestCase):
    def test_chunks_pass_through_byte_for_byte(self):
        sent, parsed, events, _ = run(STREAM)
        self.assertEqual(sent, STREAM.encode("utf-8"))
        self.assertEqual(len(parsed), 5)
        self.assertTrue(events.done)
        self.assertEqual(events.finish, "stop")

    def test_split_anywhere(self):
        for size in (1, 2, 5, 17, 1000):
            sent, _, events, _ = run(STREAM, size=size)
            self.assertEqual(sent, STREAM.encode("utf-8"), size)
            self.assertTrue(events.done)

    def test_the_usage_chunk_is_dropped_unless_asked_for(self):
        sent, parsed, events, ctx = run(STREAM, keep_usage=False)
        without = sse(chunk({"role": "assistant", "content": ""}), chunk({"content": "Hel"}),
                      chunk({"content": "lo"}), chunk({}, "stop"), "[DONE]")
        self.assertEqual(sent, without.encode("utf-8"))
        self.assertEqual(len(parsed), 5)  # the non-stream answer still reads it
        self.assertEqual(events.usage, USAGE["usage"])
        self.assertEqual((ctx.prompt_tokens, ctx.completion_tokens), (11, 3))
        _, _, _, kept = run(STREAM, keep_usage=True)
        self.assertEqual((kept.prompt_tokens, kept.completion_tokens), (11, 3))

    def test_comments_stay_and_event_lines_go(self):
        data = json.dumps(chunk({"content": "x"}))
        text = f": keep-alive\r\n\r\nevent: message\r\nid: 7\r\nretry: 3\r\ndata: {data}\r\n\r\n"
        expected = f": keep-alive\n\ndata: {data}\n\n".encode("utf-8")
        for size in (None, 1, 3):
            sent, parsed, _, _ = run(text, size=size)
            self.assertEqual(sent, expected, size)
            self.assertEqual(len(parsed), 1)
        self.assertNotIn(b"event:", run(": a\n\nevent: x\nid: 1\ndata: [DONE]\n\n")[0])

    def test_data_line_without_a_space(self):
        line = "data:" + json.dumps(chunk({"content": "x"}), separators=(",", ":")) + "\n\n"
        sent, parsed, _, _ = run(line)
        self.assertEqual(sent, line.encode("utf-8"))
        self.assertEqual(parsed[0]["choices"][0]["delta"], {"content": "x"})

    def test_reasoning_is_removed(self):
        text = sse(chunk({"role": "assistant", "content": ""}), chunk({"reasoning_content": "hmm"}),
                   chunk({"reasoning": "hmm"}), chunk({"content": "A", "reasoning_content": "x"}),
                   chunk({"reasoning_content": "y"}, "stop"), "[DONE]")
        sent, parsed, _, _ = run(text)
        self.assertNotIn(b"reasoning", sent)
        events = [block for block in sent.decode("utf-8").split("\n\n") if block]
        self.assertEqual(len(events), 4)  # the role chunk, "A", the finish chunk and [DONE]
        self.assertEqual(json.loads(events[1][6:])["choices"][0]["delta"], {"content": "A"})
        self.assertEqual(json.loads(events[2][6:])["choices"][0],
                         {"index": 0, "delta": {}, "finish_reason": "stop"})
        self.assertEqual(events[3], "data: [DONE]")
        self.assertTrue(all("reasoning_content" not in c["choices"][0]["delta"] for c in parsed))

    def test_bad_upstream_events_are_a_bad_gateway(self):
        for text in ("data: {not json\n\n", "data: [1, 2]\n\n", 'data: {"error": {"message": "secret-internal"}}\n\n',
                     "data: " + "x" * (api.EVENT_LIMIT + 1)):
            with self.assertRaises(api.ApiError) as caught:
                run(text)
            self.assertEqual((caught.exception.status, caught.exception.code), (502, "upstream_error"))
            self.assertNotIn("secret-internal", caught.exception.message)

    def test_nothing_after_done(self):
        sent, parsed, events, _ = run(sse(chunk({"content": "a"}), "[DONE]", chunk({"content": "late"})))
        self.assertNotIn(b"late", sent)
        self.assertEqual(len(parsed), 1)
        self.assertTrue(events.done)

    def test_the_context_notes_the_first_token_and_the_finish(self):
        ctx = api.Context("rid", "fp8")
        events = api.Events(ctx)
        events.feed(sse(chunk({"role": "assistant", "content": ""})), True)
        self.assertIsNone(ctx.ttft)
        events.feed(sse(chunk(calls(0, name="f"))), True)
        self.assertIsNotNone(ctx.ttft)
        events.feed(sse(chunk({}, "length")), True)
        self.assertEqual((events.finish, ctx.finish_reason), ("length", "length"))


def synthesize(*chunks, fallback="ax-k2-fp8"):
    events, synth = api.Events(api.Context("rid", "fp8")), api.Synth()
    for _, parsed in events.feed(sse(*chunks), True):
        if parsed is not None:
            synth.add(parsed)
    return synth.completion(events, fallback)


class SynthTests(unittest.TestCase):
    def test_text_and_usage(self):
        body = synthesize(chunk({"role": "assistant", "content": ""}), chunk({"content": "Hel"}),
                          chunk({"content": "lo"}), chunk({}, "stop"), USAGE)
        self.assertEqual(body, {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "axk2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "logprobs": None,
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})

    def test_tool_calls_are_merged_by_index(self):
        body = synthesize(
            chunk({"role": "assistant", "content": "", "tool_calls": [
                {"index": 0, "id": "call-a", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]}),
            chunk(calls(0, arguments='{"city":')),
            chunk({"tool_calls": [{"index": 1, "id": "call-b", "type": "function",
                                   "function": {"name": "get_time", "arguments": "{}"}}]}),
            chunk(calls(0, arguments=' "Seoul"}')),
            chunk({}, "tool_calls"), USAGE)
        choice = body["choices"][0]
        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(choice["message"]["tool_calls"], [
            {"id": "call-a", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}},
            {"id": "call-b", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}])
        self.assertEqual(choice["finish_reason"], "tool_calls")
        json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])  # a JSON string, as OpenAI has it

    def test_text_before_a_tool_call_is_kept(self):
        body = synthesize(chunk({"content": "Let me check."}), chunk({"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "f", "arguments": "{}"}}]}), chunk({}, "tool_calls"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Let me check.")

    def test_the_finish_reason_follows_the_calls(self):
        call = chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "f", "arguments": "{}"}}]})
        finish = lambda *chunks: synthesize(*chunks)["choices"][0]["finish_reason"]  # noqa: E731
        self.assertEqual(finish(call, chunk({}, "stop")), "tool_calls")
        self.assertEqual(finish(chunk({"content": "x"}), chunk({}, "stop")), "stop")
        self.assertEqual(finish(chunk({"content": "x"}), chunk({}, "length")), "length")
        self.assertEqual(finish(call, chunk({}, "length")), "length")

    def test_missing_ids_and_arguments(self):
        body = synthesize(chunk({"tool_calls": [{"index": 0, "function": {"name": "f"}}]}), chunk({}, "tool_calls"))
        call = body["choices"][0]["message"]["tool_calls"][0]
        self.assertRegex(call["id"], r"^call_[0-9a-f]{24}$")
        self.assertEqual(call["function"], {"name": "f", "arguments": "{}"})

    def test_function_names_arrive_whole_or_in_pieces(self):
        name = lambda *chunks: synthesize(*chunks)["choices"][0]["message"]["tool_calls"][0]["function"]["name"]  # noqa: E731
        self.assertEqual(name(chunk(calls(0, name="get_")), chunk(calls(0, name="weather", arguments="{}")),
                              chunk({}, "tool_calls")), "get_weather")
        self.assertEqual(name(chunk(calls(0, name="get_weather")), chunk(calls(0, name="get_weather", arguments="{}")),
                              chunk({}, "tool_calls")), "get_weather")

    def test_usage_only_when_the_counts_are_integers(self):
        self.assertNotIn("usage", synthesize(chunk({"content": "x"}), chunk({}, "stop")))
        broken = dict(USAGE, usage={"prompt_tokens": "1", "completion_tokens": 2})
        self.assertNotIn("usage", synthesize(chunk({"content": "x"}), chunk({}, "stop"), broken))

    def test_reasoning_and_other_choices_are_left_out(self):
        other = {"id": "chatcmpl-1", "created": 1, "model": "axk2",
                 "choices": [{"index": 1, "delta": {"content": "zzz"}, "finish_reason": None}]}
        body = synthesize(chunk({"reasoning_content": "hmm"}), other, chunk({"content": "x"}), chunk({}, "stop"))
        self.assertEqual(body["choices"][0]["message"], {"role": "assistant", "content": "x"})
        self.assertNotIn("reasoning", json.dumps(body))

    def test_metadata_and_fallbacks(self):
        body = synthesize(chunk({"content": "x"}, system_fingerprint="fp_1"), chunk({}, "stop"))
        self.assertEqual(body["system_fingerprint"], "fp_1")
        bare = {"choices": [{"index": 0, "delta": {"content": "x"}}]}
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        body = synthesize(bare, done)
        self.assertRegex(body["id"], r"^chatcmpl-[0-9a-f]{24}$")
        self.assertEqual((body["model"], body["object"]), ("ax-k2-fp8", "chat.completion"))
        self.assertIsInstance(body["created"], int)


class ContextTests(unittest.TestCase):
    def test_one_json_line_of_numbers_and_codes(self):
        ctx = api.Context("rid-1", "fp8")
        ctx.route, ctx.stream, ctx.status, ctx.finish_reason = "chat", True, 200, "stop"
        ctx.prompt_tokens, ctx.completion_tokens, ctx.dropped = 5, 2, ["metadata"]
        ctx.token()
        with self.assertLogs("axk2.model_api", "INFO") as logs:
            ctx.log()
            ctx.log()
        self.assertEqual(len(logs.records), 1)
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(set(record), {"ts", "event", "request_id", "profile", "route", "stream", "status",
                                       "prompt_tokens", "completion_tokens", "ttft_ms", "queue_ms", "total_ms",
                                       "finish_reason", "error", "dropped_fields"})
        self.assertEqual((record["request_id"], record["profile"], record["route"], record["status"]),
                         ("rid-1", "fp8", "chat", 200))
        self.assertEqual((record["prompt_tokens"], record["completion_tokens"], record["finish_reason"]), (5, 2, "stop"))
        self.assertIsInstance(record["ttft_ms"], int)
        self.assertIsNone(record["queue_ms"])

    def test_no_dropped_fields_no_key(self):
        with self.assertLogs("axk2.model_api", "INFO") as logs:
            api.Context("rid-2", "nvfp4").log()
        self.assertNotIn("dropped_fields", json.loads(logs.records[0].getMessage()))


if __name__ == "__main__":
    unittest.main()

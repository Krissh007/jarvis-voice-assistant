"""Long tool results are shortened before the Live model sees them (models faked)."""

import asyncio

from core.condense import Condenser, cut
from core.llm import LLM, USAGE_LIMIT_MESSAGE, LLMUnavailable
from core.registry import Tier, Tool
from tests.fakes import fake_live as fl

LONG = "File Edit View " * 40 + "Claude: A monad is a design pattern for chaining steps. " + "Copy Retry " * 40


class Model:
    def __init__(self, reply="Claude says a monad is a pattern for chaining steps.", fail=False):
        self.calls, self.reply, self.fail = [], reply, fail

    async def generate(self, prompt, system="", max_tokens=800, json=False, images=None, thinking=None,
                       start_at=None):
        self.calls.append({"prompt": prompt, "start_at": start_at})
        if self.fail:
            raise LLMUnavailable(USAGE_LIMIT_MESSAGE)
        return self.reply


def condenser(model, **opts):
    return Condenser(model, {"condense": {"over_chars": 500, "max_words": 80, "model": "lite", **opts}})


def test_long_text_is_condensed_for_the_users_request_with_the_small_model_first() -> None:
    model = Model()
    out = asyncio.run(condenser(model).condense("read_app", LONG, "read me Claude's reply"))
    assert out == "Claude says a monad is a pattern for chaining steps."
    call = model.calls[0]
    assert call["start_at"] == "lite" and "The user asked: read me Claude's reply" in call["prompt"]
    assert '<untrusted_content source="read_app">' in call["prompt"] and "at most 80 words" in call["prompt"]


def test_short_text_passes_unchanged_without_a_model_call() -> None:
    model = Model()
    assert asyncio.run(condenser(model).condense("job_search_status", "71 shortlisted.", "q")) == "71 shortlisted."
    assert model.calls == []


def test_without_a_model_the_start_and_end_are_kept() -> None:
    out = asyncio.run(condenser(Model(fail=True), cut_chars=500).condense("read_app", "A" * 400 + "MIDDLE" + "Z" * 400, "q"))
    assert out.startswith("A") and out.endswith("Z") and "(cut short)" in out and "MIDDLE" not in out
    assert cut("short", 100) == "short"


def harness(tmp_path, model, condense, text=LONG):
    calls = []

    def tool_func(what="all"):
        calls.append(what)
        return text

    h = fl.SafetyHarness(tmp_path, extra_tools=[Tool(
        name="read_app", description="read", func=tool_func, tier=Tier.SAFE, read_only=True,
        untrusted_output=True, condense=condense, parameters={"type": "object", "properties": {"what": {}}})])
    h.dispatcher.condenser = condenser(model)
    return h, calls


def test_dispatcher_condenses_opted_in_results_then_wraps_them_as_untrusted(tmp_path) -> None:
    model = Model()
    h, _ = harness(tmp_path, model, condense=lambda a: a.get("what") != "controls")

    async def go():
        await h.dispatcher.on_user_input("read me Claude's reply", "text")
        return await h.model_calls("read_app", what="text")

    resp = asyncio.run(go())
    assert resp["result"].startswith('<untrusted_content source="read_app">')
    assert "a monad is a pattern" in resp["result"] and "File Edit View" not in resp["result"]
    assert h.dispatcher.tainted and "read me Claude's reply" in model.calls[0]["prompt"]


def test_controls_lists_and_tools_that_dont_opt_in_are_never_condensed(tmp_path) -> None:
    model = Model()
    h, _ = harness(tmp_path, model, condense=lambda a: a.get("what") != "controls")
    resp = asyncio.run(h.model_calls("read_app", what="controls"))
    assert "File Edit View" in resp["result"] and model.calls == []
    h2, _ = harness(tmp_path, model, condense=False)
    assert "File Edit View" in asyncio.run(h2.model_calls("read_app", what="text"))["result"]


def test_plan_steps_get_the_full_text(tmp_path) -> None:
    model = Model()
    h, _ = harness(tmp_path, model, condense=True)
    out = asyncio.run(h.dispatcher.run_step(h.registry.get("read_app"), {"what": "text"}, "plan_approved"))
    assert "File Edit View" in out and model.calls == []


def test_llm_start_at_tries_that_model_first() -> None:
    tried = []

    class Client:
        class aio:
            class models:
                @staticmethod
                async def generate_content(*, model, contents, config):
                    tried.append(model)
                    return type("R", (), {"text": "ok", "candidates": []})()

    llm = LLM(Client(), ["flash", "flash-b", "lite"])
    asyncio.run(llm.generate("q", start_at="lite"))
    asyncio.run(llm.generate("q", start_at="not-in-chain"))
    assert tried == ["lite", "flash"]


def test_a_slow_model_falls_back_to_cutting() -> None:
    class Slow(Model):
        async def generate(self, prompt, **kw):
            await asyncio.sleep(5)
            return "late"

    c = condenser(Slow(), timeout_s=0.05, cut_chars=500)
    out = asyncio.run(c.condense("read_app", "A" * 400 + "Z" * 400, "q"))
    assert "(cut short)" in out and "late" not in out
    assert Condenser(Slow(), {}).timeout_s == 1.5                      # seen live: 5 s made replies wait for nothing


def test_an_overloaded_model_isnt_asked_and_a_cut_keeps_more() -> None:
    class Busy(Model):
        asked = 0

        def available(self, model):
            return False

        async def generate(self, prompt, **kw):
            Busy.asked += 1
            return "summary"

    text = "A" * 3000 + "MIDDLE" + "Z" * 3000
    out = asyncio.run(condenser(Busy()).condense("read_app", text, "q"))
    assert Busy.asked == 0 and "(cut short)" in out and len(out) > 3000   # straight to the cut, no waiting

"""Agent engines built on agent frameworks: LangGraph and Google's Agent Development Kit (ADK).

Both drive the same `agent.Session`: the same sandbox tools, prompt, step limit, stop button and progress
events as the built-in loop, so a run looks the same in the app whichever engine did it. Each framework talks
to the model through a small adapter over `llm.tool_step` (a LangChain chat model for LangGraph, a `BaseLlm`
for ADK), so every provider set up in Settings works with both, and each call counts in the daily budget.

    pip install "codewright[langgraph]"   # langgraph + langchain-core
    pip install "codewright[adk]"         # google-adk
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import llm
from .agent import Session, _trim

# --------------------------------------------------------------------------- LangGraph


def _lc_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content or [])


def lc_to_turns(messages: list) -> tuple[str, list[dict]]:
    """LangChain messages -> (system prompt, Codewright's neutral turns)."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    system, turns = [], []
    for m in messages:
        if isinstance(m, SystemMessage):
            system.append(_lc_text(m.content))
        elif isinstance(m, HumanMessage):
            turns.append({"role": "user", "content": _lc_text(m.content)})
        elif isinstance(m, AIMessage):
            turns.append(
                {
                    "role": "assistant",
                    "text": _lc_text(m.content),
                    "calls": [{"id": c["id"], "name": c["name"], "args": c["args"]} for c in m.tool_calls],
                    "raw": m.additional_kwargs.get("codewright_raw"),
                }
            )
        elif isinstance(m, ToolMessage):
            r = {"id": m.tool_call_id, "name": m.name or "", "content": _lc_text(m.content)}
            if turns and turns[-1]["role"] == "tool":
                turns[-1]["results"].append(r)
            else:
                turns.append({"role": "tool", "results": [r]})
    return "\n\n".join(system), turns


def chat_model(cfg: llm.LLMConfig, max_tokens: int = 8000, purpose: str = "agent"):
    """A LangChain chat model that answers through Codewright's configured provider (with tool calling)."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from pydantic import ConfigDict

    class CodewrightChatModel(BaseChatModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        cfg: Any
        max_tokens: int = 8000
        purpose: str = "agent"

        @property
        def _llm_type(self) -> str:
            return "codewright"

        def bind_tools(self, tools, **kwargs):
            return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            system, turns = lc_to_turns(messages)
            _trim(turns)
            tools = [t["function"] for t in kwargs.get("tools") or []]
            turn = llm.tool_step(self.cfg, system, turns, tools, max_tokens=self.max_tokens, purpose=self.purpose)
            msg = AIMessage(
                content=turn["text"],
                tool_calls=[
                    {"name": c["name"], "args": c["args"], "id": c["id"], "type": "tool_call"} for c in turn["calls"]
                ],
                additional_kwargs={"codewright_raw": turn["raw"]},
                response_metadata={"truncated": turn["truncated"], "model_name": self.cfg.model},
            )
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return CodewrightChatModel(cfg=cfg, max_tokens=max_tokens, purpose=purpose)


def build_graph(s: Session, model=None):
    """The LangGraph state graph: model -> tools -> model ... until the agent calls finish, answers without
    a tool call, is stopped, or reaches the step limit."""
    from typing import Annotated, TypedDict

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages

    # The functional form: with `from __future__ import annotations` a class body's annotations would be strings
    # that LangGraph resolves in this module's globals, where these locally imported names are not.
    State = TypedDict("State", {"messages": Annotated[list, add_messages]})  # noqa: UP013

    bound = (model or chat_model(s.cfg)).bind_tools(s.tools)

    def call_model(state: dict) -> dict:
        if not s.thinking():
            return {"messages": []}
        ai = bound.invoke([SystemMessage(s.system), *state["messages"]])
        s.say(_lc_text(ai.content))
        if not ai.tool_calls and ai.response_metadata.get("truncated"):
            return {"messages": [ai, HumanMessage("Your answer was cut off. Continue, in smaller steps.")]}
        return {"messages": [ai]}

    def call_tools(state: dict) -> dict:
        ai = state["messages"][-1]
        return {
            "messages": [
                ToolMessage(content=s.tool(c["name"], c["args"]), tool_call_id=c["id"], name=c["name"])
                for c in ai.tool_calls
            ]
        }

    def after_model(state: dict) -> str:
        last = state["messages"][-1]
        if s.ended:  # stopped, or the step limit
            return END
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        if isinstance(last, HumanMessage):  # asked to continue after a cut-off answer
            return "model"
        s.finished, s.ended = _lc_text(last.content), "answered"
        return END

    def after_tools(state: dict) -> str:
        if s.finished is not None:
            s.ended = "finished"
            return END
        return END if s.stopped() else "model"

    g = StateGraph(State)
    g.add_node("model", call_model)
    g.add_node("tools", call_tools)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, ["tools", "model", END])
    g.add_conditional_edges("tools", after_tools, ["model", END])
    return g.compile()


def run_langgraph(s: Session, model=None) -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.errors import GraphRecursionError

    graph = build_graph(s, model)
    try:
        graph.invoke({"messages": [HumanMessage(s.intro)]}, {"recursion_limit": s.max_steps * 3 + 10})
    except GraphRecursionError:
        s.ended = s.ended or "step limit"


# --------------------------------------------------------------------------- Google ADK


def adk_to_turns(contents: list, raws: list) -> list[dict]:
    """ADK (google.genai) contents -> neutral turns. `raws[i]` is the provider's own message for the i-th
    model turn this adapter produced (Claude's blocks, Gemini's parts with thought signatures)."""
    turns, model_turn = [], 0
    for c in contents or []:
        parts = c.parts or []
        if c.role == "model":
            calls = [
                {"id": p.function_call.id or "", "name": p.function_call.name, "args": dict(p.function_call.args or {})}
                for p in parts
                if p.function_call
            ]
            raw = raws[model_turn] if model_turn < len(raws) else None
            if raw is not None and raw[0] != [x["name"] for x in calls]:
                raw = None  # not the turn we recorded
            turns.append(
                {
                    "role": "assistant",
                    "text": "".join(p.text or "" for p in parts if p.text and not p.thought),
                    "calls": calls,
                    "raw": raw[1] if raw else None,
                }
            )
            model_turn += 1
            continue
        results = []
        for p in parts:
            if p.function_response:
                resp = p.function_response.response or {}
                content = resp.get("result", resp) if isinstance(resp, dict) else resp
                results.append(
                    {
                        "id": p.function_response.id or "",
                        "name": p.function_response.name,
                        "content": content if isinstance(content, str) else str(content),
                    }
                )
        if results:
            turns.append({"role": "tool", "results": results})
        text = "".join(p.text or "" for p in parts if p.text)
        if text:
            turns.append({"role": "user", "content": text})
    return turns


def _system_text(cfg) -> str:
    si = getattr(cfg, "system_instruction", None) if cfg is not None else None
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    parts = getattr(si, "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _declared_tools(cfg) -> list[dict]:
    out = []
    for t in (getattr(cfg, "tools", None) or []) if cfg is not None else []:
        for d in getattr(t, "function_declarations", None) or []:
            params = d.parameters_json_schema or (
                d.parameters.model_dump(exclude_none=True, mode="json") if d.parameters else None
            )
            out.append(
                {
                    "name": d.name,
                    "description": d.description or "",
                    "parameters": params or {"type": "object", "properties": {}},
                }
            )
    return out


def adk_model(s: Session):
    """An ADK model (BaseLlm) that answers through Codewright's configured provider."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from pydantic import ConfigDict, PrivateAttr

    class CodewrightLlm(BaseLlm):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        _session: Any = PrivateAttr(default=None)
        _raws: list = PrivateAttr(default_factory=list)

        async def generate_content_async(self, llm_request, stream: bool = False):
            sess = self._session
            if not sess.thinking():  # an answer with no tool call ends the run
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="")]), turn_complete=True)
                return
            turns = adk_to_turns(llm_request.contents, self._raws)
            _trim(turns)
            turn = await asyncio.to_thread(
                llm.tool_step,
                sess.cfg,
                _system_text(llm_request.config),
                turns,
                _declared_tools(llm_request.config),
                max_tokens=8000,
                purpose="agent",
            )
            self._raws.append(([c["name"] for c in turn["calls"]], turn["raw"]))
            sess.say(turn["text"])
            parts = [types.Part(text=turn["text"])] if turn["text"] else []
            parts += [
                types.Part(function_call=types.FunctionCall(id=c["id"], name=c["name"], args=c["args"]))
                for c in turn["calls"]
            ]
            yield LlmResponse(
                content=types.Content(role="model", parts=parts or [types.Part(text="")]), turn_complete=True
            )

    m = CodewrightLlm(model=f"codewright/{s.cfg.provider}/{s.cfg.model}")
    m._session = s
    return m


def adk_tools(s: Session) -> list:
    """The sandbox tools as ADK tools, declared with their JSON schemas."""
    from google.adk.tools.base_tool import BaseTool
    from google.genai import types

    class SandboxTool(BaseTool):
        def __init__(self, spec: dict):
            super().__init__(name=spec["name"], description=spec["description"])
            self._spec = spec

        def _get_declaration(self):
            return types.FunctionDeclaration(
                name=self._spec["name"],
                description=self._spec["description"],
                parameters_json_schema=self._spec["parameters"],
            )

        async def run_async(self, *, args, tool_context):
            res = await asyncio.to_thread(s.tool, self.name, dict(args or {}))
            if self.name == "finish":
                tool_context.actions.skip_summarization = True  # the summary is the answer: no more model calls
            return {"result": res}

    return [SandboxTool(t) for t in s.tools]


def build_adk_agent(s: Session, model=None):
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="codewright_agent",
        description="Makes code changes in a sandbox copy of the repository and tests them.",
        model=model or adk_model(s),
        instruction=lambda _ctx: s.system,  # a callable: ADK leaves it as written (no {state} templating)
        tools=adk_tools(s),
    )


def run_adk(s: Session, model=None) -> None:
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    async def go() -> None:
        sessions = InMemorySessionService()
        runner = Runner(app_name="codewright", agent=build_adk_agent(s, model), session_service=sessions)
        sess = await sessions.create_session(app_name="codewright", user_id="codewright")
        last_text = ""
        async for event in runner.run_async(
            user_id="codewright",
            session_id=sess.id,
            new_message=types.Content(role="user", parts=[types.Part(text=s.intro)]),
            run_config=RunConfig(max_llm_calls=s.max_steps + 5),
        ):
            content = getattr(event, "content", None)
            if content and content.role == "model":
                text = "".join(p.text or "" for p in content.parts or [] if p.text)
                if text and not any(p.function_call for p in content.parts or []):
                    last_text = text
            if s.stopped():
                s.ended = "stopped"
                break
        if s.finished is not None:
            s.ended = s.ended or "finished"
        elif not s.ended:
            s.finished, s.ended = last_text, "answered"

    try:
        asyncio.run(go())
    except Exception as exc:
        if type(exc).__name__ == "LlmCallsLimitExceededError":
            s.ended = "step limit"
        else:
            raise

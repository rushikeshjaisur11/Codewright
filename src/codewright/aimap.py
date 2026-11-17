"""The AI page: which files need a model, and how the repo's agents are put together.

Read from the code graph only (no model is called). It finds:

- files that import an LLM SDK or agent framework, and what they do with it: define agents,
  call a model directly, or only import it;
- agents (Google ADK, OpenAI Agents SDK, Pydantic AI, CrewAI, smolagents, LangChain, AutoGen,
  LlamaIndex), with their model, instruction, tools and sub-agents (or handoffs);
- LangGraph ``StateGraph`` workflows, with their steps and edges;
- direct model calls (``client.messages.create``, ``chat.completions.create``,
  ``generate_content``, ``litellm.completion``...), with the model name when it is a literal;
- the functions that run an agent, the files that provide its tools, and the code that calls
  into all of this.

From these it draws the agent architecture: who runs which agent, which agents delegate to
which, which tools and models each uses, every box linked to its file and line.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from tree_sitter import Node as TSNode

from .archmap import module_name
from .graph import CodeGraph, ParsedFile, is_test_file, text
from .models import Finding

# import prefix -> what it is (provider or framework)
AI_IMPORTS = [
    ("google.adk", "Google ADK"),
    ("google.genai", "Gemini API"),
    ("google.generativeai", "Gemini API"),
    ("vertexai", "Vertex AI"),
    ("anthropic", "Claude API"),
    ("openai", "OpenAI API"),
    ("agents", "OpenAI Agents SDK"),
    ("litellm", "LiteLLM"),
    ("langgraph", "LangGraph"),
    ("langchain", "LangChain"),
    ("langchain_core", "LangChain"),
    ("langchain_openai", "LangChain"),
    ("langchain_anthropic", "LangChain"),
    ("langchain_google_genai", "LangChain"),
    ("langchain_google_vertexai", "LangChain"),
    ("langchain_community", "LangChain"),
    ("llama_index", "LlamaIndex"),
    ("crewai", "CrewAI"),
    ("pydantic_ai", "Pydantic AI"),
    ("smolagents", "smolagents"),
    ("autogen", "AutoGen"),
    ("autogen_agentchat", "AutoGen"),
    ("autogen_ext", "AutoGen"),
    ("dspy", "DSPy"),
    ("instructor", "Instructor"),
    ("mistralai", "Mistral API"),
    ("cohere", "Cohere API"),
    ("groq", "Groq API"),
    ("ollama", "Ollama"),
    ("transformers", "Hugging Face Transformers"),
    ("huggingface_hub", "Hugging Face Hub"),
    ("boto3.bedrock", "Amazon Bedrock"),
    ("mcp", "MCP"),
]

# Calls that send a request to a model, by how the call ends; the provider when it is unambiguous.
MODEL_CALL_SUFFIXES = [
    (".messages.create", "Claude API"),
    (".messages.stream", "Claude API"),
    (".messages.parse", "Claude API"),
    (".chat.completions.create", "OpenAI API"),
    (".chat.completions.parse", "OpenAI API"),
    (".beta.chat.completions.parse", "OpenAI API"),
    (".responses.create", "OpenAI API"),
    (".responses.parse", "OpenAI API"),
    (".completions.create", "OpenAI API"),
    (".embeddings.create", "OpenAI API"),
    (".models.generate_content", "Gemini API"),
    (".models.generate_content_stream", "Gemini API"),
    (".models.embed_content", "Gemini API"),
    (".generate_content", "Gemini API"),
    (".generate_content_async", "Gemini API"),
    (".send_message", "Gemini API"),
    (".invoke_model", "Amazon Bedrock"),
    (".converse", "Amazon Bedrock"),
]
# Module-level functions that call a model, by their resolved dotted name.
MODEL_CALL_FUNCTIONS = {
    "litellm.completion": "LiteLLM",
    "litellm.acompletion": "LiteLLM",
    "litellm.embedding": "LiteLLM",
    "litellm.aembedding": "LiteLLM",
    "ollama.chat": "Ollama",
    "ollama.generate": "Ollama",
    "ollama.embed": "Ollama",
    "openai.ChatCompletion.create": "OpenAI API",
    "openai.Completion.create": "OpenAI API",
}
# LangChain-style model objects are called with .invoke() and friends.
LANGCHAIN_MODEL_CLASSES = (
    "ChatOpenAI",
    "ChatAnthropic",
    "ChatGoogleGenerativeAI",
    "ChatVertexAI",
    "ChatOllama",
    "AzureChatOpenAI",
    "ChatLiteLLM",
    "ChatBedrock",
    "ChatGroq",
    "ChatMistralAI",
)
LANGCHAIN_CALLS = (".invoke", ".ainvoke", ".stream", ".astream", ".batch", ".abatch")

AGENT_KWARGS = ("sub_agents", "handoffs", "agents", "members", "participants", "managed_agents")
TOOL_KWARGS = ("tools", "toolsets")
INSTRUCTION_KWARGS = (
    "instruction",
    "instructions",
    "system_prompt",
    "system_message",
    "prompt",
    "goal",
    "backstory",
    "description",
    "role",
)
RUN_METHODS = (
    "run",
    "run_async",
    "run_sync",
    "run_stream",
    "run_live",
    "invoke",
    "ainvoke",
    "stream",
    "astream",
    "kickoff",
    "kickoff_async",
    "chat",
    "achat",
    "initiate_chat",
)
AGENT_RUNNERS = ("Runner", "InMemoryRunner", "AgentExecutor")  # constructors that take the agent to run


@dataclass
class Agent:
    id: str  # "<file>::<variable>" or "<file>:<line>"
    name: str
    var: str
    file: str
    line: int
    framework: str
    kind: str  # llm | sequential | parallel | loop | remote | crew | graph
    model: str = ""
    instruction: str = ""
    tools: list[dict] = field(default_factory=list)  # {label, file, line, agent} (agent: id of an agent used as a tool)
    children: list[str] = field(default_factory=list)  # agent ids it delegates to (sub-agents, handoffs, crew members)
    child_names: list[str] = field(default_factory=list)  # as written, before resolving
    steps: list[dict] = field(default_factory=list)  # LangGraph: {name, file, line}
    step_edges: list[list[str]] = field(default_factory=list)  # LangGraph: [source, target, label]
    end: int = 0  # last line of the definition
    instruction_full: str = ""  # the whole literal, for the inspector (instruction is shortened)
    options: dict = field(default_factory=dict)  # other keyword arguments as written, shortened


@dataclass
class ModelCall:
    file: str
    function: str
    line: int
    api: str  # the call as written
    provider: str
    model: str = ""


@dataclass
class Runs:
    file: str
    function: str
    line: int
    agent: str  # agent id
    how: str  # the call as written


@dataclass
class AiFile:
    file: str
    sdks: list[str]
    roles: list[str]  # "agents", "model calls", "tools", "runs agents", "imports", "calls AI code"
    agents: list[str] = field(default_factory=list)
    model_calls: int = 0
    tools: list[str] = field(default_factory=list)
    via: list[str] = field(default_factory=list)  # for "calls AI code": the AI functions it calls
    findings: int = 0


# --------------------------------------------------------------------------- tree helpers


def _walk(node: TSNode):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _string(node: TSNode | None) -> str | None:
    """The value of a plain string literal (not an f-string), else None."""
    if node is None:
        return None
    if node.type == "concatenated_string":
        parts = [_string(c) for c in node.named_children]
        return "".join(parts) if all(p is not None for p in parts) else None
    if node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(text(c) for c in node.named_children if c.type == "string_content")


def _first_string(node: TSNode | None) -> str | None:
    if node is None:
        return None
    for n in _walk(node):
        if (s := _string(n)) is not None and s.strip():
            return s
    return None


def _args(call: TSNode) -> tuple[list[TSNode], dict[str, TSNode]]:
    args = call.child_by_field_name("arguments")
    pos, kw = [], {}
    if args is None:
        return pos, kw
    for a in args.named_children:
        if a.type == "keyword_argument":
            name, value = a.child_by_field_name("name"), a.child_by_field_name("value")
            if name is not None and value is not None:
                kw[text(name)] = value
        elif a.type not in ("comment", "list_splat", "dictionary_splat"):
            pos.append(a)
    return pos, kw


def _resolve(pf: ParsedFile, dotted: str) -> str | None:
    """`dotted` as the fully qualified name it refers to through the file's imports, or None."""
    head, _, rest = dotted.partition(".")
    target = pf.module.imports.get(head)
    if target is None:
        return None
    return f"{target}.{rest}" if rest else target


def _ai_label(target: str) -> str | None:
    for prefix, label in AI_IMPORTS:
        if target == prefix or target.startswith(prefix + "."):
            return label
    return None


def _agent_ctor(resolved: str) -> tuple[str, str] | None:
    """(framework, kind) when `resolved` constructs an agent."""
    last = resolved.rsplit(".", 1)[-1]
    if resolved.startswith("google.adk"):
        kinds = {
            "Agent": "llm",
            "LlmAgent": "llm",
            "SequentialAgent": "sequential",
            "ParallelAgent": "parallel",
            "LoopAgent": "loop",
            "RemoteA2aAgent": "remote",
        }
        return ("Google ADK", kinds[last]) if last in kinds else None
    if resolved in ("agents.Agent", "agents.agent.Agent"):
        return "OpenAI Agents SDK", "llm"
    if resolved in ("pydantic_ai.Agent", "pydantic_ai.agent.Agent"):
        return "Pydantic AI", "llm"
    if resolved.startswith("crewai"):
        return {"Agent": ("CrewAI", "llm"), "Crew": ("CrewAI", "crew")}.get(last)
    if resolved.startswith(("langgraph.prebuilt", "langchain.agents")) and (
        (last.startswith("create_") and last.endswith("agent")) or last == "AgentExecutor"
    ):
        return "LangChain", "llm"
    if resolved.startswith(("smolagents", "autogen", "llama_index")) and last.endswith("Agent"):
        return _ai_label(resolved) or "agents", "llm"
    if (label := _ai_label(resolved)) and last.endswith("Agent") and label not in ("MCP",):
        return label, "llm"
    return None


def _provider_of_model(model: str) -> str:
    m = model.lower()
    if ":" in m and not m.startswith("http"):
        m = m.split(":", 1)[1]
    if "/" in m:
        prefix = m.split("/", 1)[0]
        return {
            "anthropic": "Claude",
            "openai": "OpenAI",
            "gemini": "Gemini",
            "vertex_ai": "Vertex AI",
            "ollama": "Ollama",
            "bedrock": "Bedrock",
            "groq": "Groq",
            "mistral": "Mistral",
        }.get(prefix, "LiteLLM")
    for start, name in (
        ("claude", "Claude"),
        ("gpt", "OpenAI"),
        ("o1", "OpenAI"),
        ("o3", "OpenAI"),
        ("o4", "OpenAI"),
        ("gemini", "Gemini"),
        ("llama", "Llama"),
        ("mistral", "Mistral"),
        ("command", "Cohere"),
    ):
        if m.startswith(start):
            return name
    return ""


def _model_text(node: TSNode | None) -> str:
    """A model argument as a short label: the literal, the literal inside a wrapper call, or the code."""
    if node is None:
        return ""
    if (s := _string(node)) is not None:
        return s
    if node.type == "call" and (inner := _first_string(node)):
        callee = node.child_by_field_name("function")
        return f"{inner} (via {text(callee).rsplit('.', 1)[-1]})" if callee is not None else inner
    return text(node)[:60]


def _short(s: str, n: int = 220) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _enclosing_function(pf: ParsedFile, line: int) -> tuple[str, int]:
    inside = [f for f in pf.module.functions if f.start_line <= line <= f.end_line]
    if inside:
        f = max(inside, key=lambda f: f.start_line)
        return f.qualname, f.def_line
    return "<module>", line


# --------------------------------------------------------------------------- the scan


class AiMap:
    def __init__(self, graph: CodeGraph, findings: list[Finding] | None = None):
        self.g = graph
        self.findings = findings or []
        self.agents: dict[str, Agent] = {}
        self.calls: list[ModelCall] = []
        self.runs: list[Runs] = []
        self._list_vars: dict[tuple[str, str], TSNode] = {}  # (file, var) -> list literal assigned at module level
        self._pending: list[tuple[Agent, ParsedFile, dict[str, TSNode], list[TSNode]]] = []
        self.sdk_files: dict[str, list[str]] = {}
        for path, pf in graph.files.items():
            if is_test_file(path):
                continue
            labels = sorted({lbl for t in pf.module.imports.values() if (lbl := _ai_label(t))})
            if labels:
                self.sdk_files[path] = labels
        for path in self.sdk_files:
            self._scan_file(graph.files[path])
        self._by_var: dict[str, list[Agent]] = defaultdict(list)
        for a in self.agents.values():
            self._by_var[a.var].append(a)
        for agent, pf, kw, pos in self._pending:
            self._link(agent, pf, kw, pos)
        for path, pf in graph.files.items():
            if not is_test_file(path):
                self._scan_runs(pf)

    # ----------------------------------------------------------------- agents and model calls
    def _scan_file(self, pf: ParsedFile) -> None:
        for n in _walk(pf.root):
            if n.type == "assignment" and (left := n.child_by_field_name("left")) is not None:
                right = n.child_by_field_name("right")
                if right is not None and right.type == "list" and left.type == "identifier":
                    self._list_vars.setdefault((pf.path, text(left)), right)
        graphs: dict[str, Agent] = {}
        langchain = any(t.startswith("langchain") for t in pf.module.imports.values())
        lc_models: set[str] = set()
        for n in _walk(pf.root):
            if n.type != "call" or (callee := n.child_by_field_name("function")) is None:
                continue
            written = text(callee)
            resolved = _resolve(pf, written) or ""
            line = n.start_point[0] + 1
            pos, kw = _args(n)
            var = self._assigned_to(n)
            if ctor := _agent_ctor(resolved):
                self._add_agent(pf, n, var, ctor, pos, kw)
                continue
            if resolved.endswith(".StateGraph") and resolved.startswith("langgraph"):
                gid = f"{pf.path}::{var or line}"
                graphs[var or str(line)] = self.agents[gid] = Agent(
                    gid, var or "graph", var or "graph", pf.path, line, "LangGraph", "graph"
                )
                continue
            if langchain and resolved.rsplit(".", 1)[-1] in LANGCHAIN_MODEL_CLASSES and var:
                lc_models.add(var)
            obj, _, method = written.rpartition(".")
            if obj in graphs and method in (
                "add_node",
                "add_edge",
                "add_conditional_edges",
                "set_entry_point",
                "set_finish_point",
            ):
                self._graph_step(pf, graphs[obj], method, pos, kw)
                continue
            if provider := self._model_call(pf, written, resolved, langchain, lc_models):
                model = _model_text(kw.get("model") or kw.get("model_id") or kw.get("modelId"))
                if not model and obj in lc_models:
                    model = ""
                fn, _ = _enclosing_function(pf, line)
                self.calls.append(ModelCall(pf.path, fn, line, written, provider, model))

    @staticmethod
    def _assigned_to(call: TSNode) -> str:
        p = call.parent
        while p is not None and p.type in ("await", "parenthesized_expression"):
            p = p.parent
        if p is not None and p.type == "assignment" and (left := p.child_by_field_name("left")) is not None:
            return text(left).rsplit(".", 1)[-1] if left.type in ("identifier", "attribute") else ""
        return ""

    def _model_call(self, pf: ParsedFile, written: str, resolved: str, langchain: bool, lc_models: set[str]) -> str:
        if resolved in MODEL_CALL_FUNCTIONS:
            return MODEL_CALL_FUNCTIONS[resolved]
        for suffix, provider in MODEL_CALL_SUFFIXES:
            if written.endswith(suffix) or ("." + written).endswith(suffix):
                labels = self.sdk_files.get(pf.path, [])
                if suffix in (".generate_content", ".send_message") and not any(
                    lbl in ("Gemini API", "Vertex AI") for lbl in labels
                ):
                    return ""
                if suffix in (".invoke_model", ".converse") and "boto3" not in " ".join(pf.module.imports.values()):
                    return ""
                return provider
        if langchain and written.endswith(LANGCHAIN_CALLS) and written.rsplit(".", 1)[0] in lc_models:
            return "LangChain"
        return ""

    def _add_agent(self, pf: ParsedFile, call: TSNode, var: str, ctor: tuple[str, str], pos, kw) -> None:
        framework, kind = ctor
        line = call.start_point[0] + 1
        name = _string(kw.get("name")) or _string(kw.get("role")) or ""
        if not name and framework == "OpenAI Agents SDK" and pos:
            name = _string(pos[0]) or ""
        model_node = kw.get("model") or kw.get("llm") or kw.get("model_client")
        if model_node is None and framework in ("Pydantic AI", "LangChain") and pos:
            model_node = pos[0]
        instruction = full = ""
        for k in INSTRUCTION_KWARGS:
            if k in kw and k not in ("role",):
                lit = _string(kw[k])
                instruction = _short(lit) if lit else f"built at runtime ({_short(text(kw[k]), 60)})"
                full = lit[:4000] if lit else instruction
                break
        skip = {"name", "model", "llm", "model_client", "tools", "toolsets", *AGENT_KWARGS, *INSTRUCTION_KWARGS}
        options = {k: _short(text(v), 80) for k, v in kw.items() if k not in skip}
        aid = f"{pf.path}::{var}" if var else f"{pf.path}:{line}"
        agent = Agent(
            aid,
            name or var or f"agent at line {line}",
            var or name,
            pf.path,
            line,
            framework,
            kind,
            _model_text(model_node),
            instruction,
        )
        agent.end = call.end_point[0] + 1
        agent.instruction_full = full
        agent.options = options
        self.agents[aid] = agent
        self._pending.append((agent, pf, kw, pos))

    def _graph_step(self, pf: ParsedFile, g: Agent, method: str, pos: list[TSNode], kw: dict[str, TSNode]) -> None:
        def name_of(n: TSNode | None) -> str:
            if n is None:
                return ""
            s = _string(n)
            return s if s is not None else text(n).rsplit(".", 1)[-1]

        if method == "add_node" and pos:
            name = name_of(pos[0])
            target = pos[1] if len(pos) > 1 else pos[0]
            fn = self._function_ref(pf, text(target)) if target.type in ("identifier", "attribute") else None
            g.steps.append(
                {"name": name, "file": fn[0] if fn else pf.path, "line": fn[1] if fn else target.start_point[0] + 1}
            )
        elif method == "add_edge" and len(pos) >= 2:
            g.step_edges.append([name_of(pos[0]), name_of(pos[1]), ""])
        elif method == "set_entry_point" and pos:
            g.step_edges.append(["START", name_of(pos[0]), ""])
        elif method == "set_finish_point" and pos:
            g.step_edges.append([name_of(pos[0]), "END", ""])
        elif method == "add_conditional_edges" and pos:
            src = name_of(pos[0])
            mapping = pos[2] if len(pos) > 2 else kw.get("path_map")
            router = name_of(pos[1]) if len(pos) > 1 else ""
            targets = []
            if mapping is not None and mapping.type == "dictionary":
                targets = [name_of(p.child_by_field_name("value")) for p in mapping.named_children if p.type == "pair"]
            elif mapping is not None and mapping.type == "list":
                targets = [name_of(x) for x in mapping.named_children]
            for t in dict.fromkeys(targets) or ["?"]:
                g.step_edges.append([src, t, f"if {router}" if router else "if"])

    def _function_ref(self, pf: ParsedFile, name: str) -> tuple[str, int] | None:
        """(file, line) of the repo function `name` refers to from `pf`, if any."""
        name = name.rsplit(".", 1)[-1] if "." in name and name.split(".")[0] in ("self", "cls") else name
        for f in pf.module.functions:
            if f.qualname == name:
                return pf.path, f.def_line
        target = pf.module.imports.get(name.split(".")[0])
        if target is None:
            return None
        if "." in name:
            target = target + "." + name.split(".", 1)[1]
        mod, _, fname = target.rpartition(".")
        for path, other in self.g.files.items():
            mn = module_name(path)
            if mn == mod or mn.endswith("." + mod) or mod.endswith("." + mn):
                for f in other.module.functions:
                    if f.qualname == fname:
                        return path, f.def_line
        return None

    def _agent_ref(self, pf: ParsedFile, name: str) -> Agent | None:
        name = name.strip()
        if f"{pf.path}::{name}" in self.agents:
            return self.agents[f"{pf.path}::{name}"]
        target = pf.module.imports.get(name)
        real = target.rsplit(".", 1)[-1] if target else name
        cands = self._by_var.get(real, [])
        if target and len(cands) > 1:
            mod = target.rpartition(".")[0]
            narrowed = [
                a
                for a in cands
                if module_name(a.file).endswith(mod.split(".")[-1]) or mod.endswith(module_name(a.file))
            ]
            cands = narrowed or cands
        return cands[0] if cands and (target or len(cands) == 1) else None

    def _items(self, pf: ParsedFile, node: TSNode) -> list[TSNode]:
        if node.type in ("list", "tuple"):
            return [c for c in node.named_children if c.type != "comment"]
        if node.type == "identifier" and (pf.path, text(node)) in self._list_vars:
            return self._items(pf, self._list_vars[(pf.path, text(node))])
        return [node]

    def _link(self, agent: Agent, pf: ParsedFile, kw: dict[str, TSNode], pos: list[TSNode]) -> None:
        for k in AGENT_KWARGS:
            if k not in kw:
                continue
            for item in self._items(pf, kw[k]):
                ref = self._agent_ref(pf, text(item)) if item.type in ("identifier", "attribute") else None
                if ref is None and item.type == "call":
                    _, ikw = _args(item)
                    inner = ikw.get("agent")
                    ref = self._agent_ref(pf, text(inner)) if inner is not None else None
                if ref is not None and ref.id != agent.id:
                    agent.children.append(ref.id)
                else:
                    agent.child_names.append(_short(text(item), 50))
        for k in TOOL_KWARGS:
            if k not in kw:
                continue
            for item in self._items(pf, kw[k]):
                agent.tools.append(self._tool(pf, item))

    def _tool(self, pf: ParsedFile, item: TSNode) -> dict:
        raw = text(item)
        if item.type in ("identifier", "attribute"):
            if (ref := self._agent_ref(pf, raw)) is not None:
                return {"label": ref.name, "agent": ref.id, "file": ref.file, "line": ref.line}
            if (fn := self._function_ref(pf, raw)) is not None:
                return {"label": raw.rsplit(".", 1)[-1], "file": fn[0], "line": fn[1]}
            return {"label": raw.rsplit(".", 1)[-1], "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}
        if item.type == "call" and (callee := item.child_by_field_name("function")) is not None:
            cpos, ckw = _args(item)
            wrapped = ckw.get("agent") or ckw.get("func") or (cpos[0] if cpos else None)
            cname = text(callee).rsplit(".", 1)[-1]
            if wrapped is not None and wrapped.type in ("identifier", "attribute"):
                if (ref := self._agent_ref(pf, text(wrapped))) is not None:
                    return {"label": ref.name, "agent": ref.id, "file": ref.file, "line": ref.line}
                if (fn := self._function_ref(pf, text(wrapped))) is not None:
                    return {"label": text(wrapped).rsplit(".", 1)[-1], "file": fn[0], "line": fn[1]}
            server = _first_string(item)
            label = cname + (f": {_short(server, 40)}" if (server and "Toolset" in cname) or "MCP" in cname else "")
            return {"label": label, "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}
        return {"label": _short(raw, 40), "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}

    # ----------------------------------------------------------------- who runs the agents
    def _scan_runs(self, pf: ParsedFile) -> None:
        if not self.agents:
            return
        names = {a.var for a in self.agents.values() if a.var}
        src = pf.source
        if not any(n.encode() in src for n in names):
            return
        for n in _walk(pf.root):
            if n.type != "call" or (callee := n.child_by_field_name("function")) is None:
                continue
            written = text(callee)
            obj, _, method = written.rpartition(".")
            pos, kw = _args(n)
            target = None
            if obj and method in RUN_METHODS and obj in names:
                target = self._agent_ref(pf, obj)
            if target is None:
                cand = kw.get("agent") or kw.get("starting_agent") or kw.get("root_agent")
                if (
                    cand is None
                    and pos
                    and written.rsplit(".", 1)[-1] in (*AGENT_RUNNERS, "run", "run_sync", "run_streamed")
                ):
                    cand = pos[0]
                if cand is not None and cand.type == "identifier" and text(cand) in names:
                    target = self._agent_ref(pf, text(cand))
            if target is None or (pf.path == target.file and n.start_point[0] + 1 == target.line):
                continue
            line = n.start_point[0] + 1
            fn, _ = _enclosing_function(pf, line)
            if fn == "<module>" and pf.path == target.file:
                continue  # the agent module wiring itself up, not something running it
            self.runs.append(Runs(pf.path, fn, line, target.id, _short(written, 50)))

    # ----------------------------------------------------------------- outputs
    def files(self) -> list[AiFile]:
        out: dict[str, AiFile] = {}
        findings = defaultdict(int)
        for f in self.findings:
            findings[f.file] += 1

        def get(path: str) -> AiFile:
            if path not in out:
                out[path] = AiFile(path, self.sdk_files.get(path, []), [], findings=findings[path])
            return out[path]

        for a in self.agents.values():
            af = get(a.file)
            af.agents.append(a.name)
            for t in a.tools:
                if not t.get("builtin") and not t.get("agent") and t["file"] != a.file:
                    tf = get(t["file"])
                    tf.tools.append(t["label"])
        for c in self.calls:
            get(c.file).model_calls += 1
        for r in self.runs:
            get(r.file)
        for path in self.sdk_files:
            get(path)

        # Code that calls into a file that needs a model, one hop through the call graph.
        direct = {p for p, af in out.items() if af.agents or af.model_calls or af.tools}
        via: dict[str, set[str]] = defaultdict(set)
        for caller, callees in self.g.edges.items():
            cfile = caller.partition("::")[0]
            if cfile in direct or is_test_file(cfile):
                continue
            for callee in callees:
                if callee.partition("::")[0] in direct:
                    via[cfile].add(callee.replace("::", " · "))
        for path, targets in via.items():
            af = get(path)
            af.via = sorted(targets)[:6]

        for path, af in out.items():
            roles = []
            if af.agents:
                roles.append("agents")
            if af.model_calls:
                roles.append("model calls")
            if af.tools:
                roles.append("tools")
            if any(r.file == path for r in self.runs):
                roles.append("runs agents")
            if af.via:
                roles.append("calls AI code")
            if not roles and af.sdks:
                roles.append("imports")
            af.roles = roles
            af.tools = sorted(set(af.tools))
        order = ["agents", "model calls", "runs agents", "tools", "calls AI code", "imports"]
        return sorted(out.values(), key=lambda f: (order.index(f.roles[0]) if f.roles else 9, f.file))

    def architecture(self) -> dict:
        """Nodes and edges for the agent architecture (same shape as a Map view)."""
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        fcount = defaultdict(int)
        for f in self.findings:
            fcount[f.file] += 1

        def node(nid: str, label: str, kind: str, file: str = "", line: int = 1, detail: str = "", name: str = ""):
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid,
                    "label": label,
                    "kind": kind,
                    "detail": detail,
                    "findings": 0,
                    "hotspot": 0,
                    "untested": 0,
                    "files": [{"file": file, "line": line, **({"name": name} if name else {})}] if file else [],
                }
            return nodes[nid]

        def edge(a: str, b: str, label: str) -> None:
            if a != b and not any(e["source"] == a and e["target"] == b and e["label"] == label for e in edges):
                edges.append({"source": a, "target": b, "label": label, "weight": 1})

        def model_node(model: str, provider: str = "") -> str:
            label = model or provider or "model"
            prov = provider or _provider_of_model(model)
            nid = f"model:{label}"
            node(nid, label, "llm", detail=prov or "model")
            return nid

        for a in self.agents.values():
            kind_label = {
                "llm": "agent",
                "sequential": "runs sub-agents in order",
                "parallel": "runs sub-agents in parallel",
                "loop": "runs sub-agents in a loop",
                "remote": "remote agent (A2A)",
                "crew": "crew",
                "graph": "workflow graph",
            }[a.kind]
            n = node(
                a.id,
                a.name,
                "workflow" if a.kind in ("sequential", "parallel", "loop", "crew", "graph") else "agent",
                a.file,
                a.line,
                f"{a.framework} · {kind_label}",
                a.var,
            )
            n["findings"] = fcount[a.file]
            n["instruction"] = a.instruction
            n["model"] = a.model
            if a.model:
                edge(a.id, model_node(a.model), "model")
            for child in a.children:
                edge(a.id, child, "delegates")
            for i, cname in enumerate(a.child_names):
                cid = f"{a.id}#missing{i}"
                node(cid, cname, "agent", a.file, a.line, "sub-agent defined outside the scanned code")
                edge(a.id, cid, "delegates")
            for t in a.tools:
                if t.get("agent"):
                    edge(a.id, t["agent"], "uses as tool")
                    continue
                tid = f"tool:{t['file']}:{t['label']}" if not t.get("builtin") else f"tool:{t['label']}"
                node(
                    tid,
                    t["label"],
                    "tool",
                    t["file"],
                    t["line"],
                    "built-in or library tool" if t.get("builtin") else f"{t['file']}:{t['line']}",
                )
                edge(a.id, tid, "tool")
            if a.kind == "graph":
                for s in a.steps:
                    sid = f"{a.id}#{s['name']}"
                    node(sid, s["name"], "function", s["file"], s["line"], f"step in {a.name}")
                    edge(a.id, sid, "step")
                names = {s["name"] for s in a.steps}
                for src, dst, label in a.step_edges:
                    ends = {"START": "function", "END": "function"}
                    ids = []
                    for x in (src, dst):
                        if x in names:
                            ids.append(f"{a.id}#{x}")
                        else:
                            xid = f"{a.id}#{x}"
                            node(
                                xid, x.lower() if x in ends else x, ends.get(x, "function"), a.file, a.line, f"{a.name}"
                            )
                            ids.append(xid)
                    edge(ids[0], ids[1], label or "then")
        for c in self.calls:
            fid = f"fn:{c.file}::{c.function}"
            node(fid, c.function, "function", c.file, c.line, c.file, c.function)
            nodes[fid]["findings"] = fcount[c.file]
            edge(
                fid,
                model_node(c.model, c.provider if not c.model else _provider_of_model(c.model) or c.provider),
                "prompts",
            )
        for r in self.runs:
            rid = f"run:{r.file}::{r.function}"
            node(
                rid,
                r.function if r.function != "<module>" else module_name(r.file),
                "entry",
                r.file,
                r.line,
                f"{r.file}:{r.line} · {r.how}",
                r.function,
            )
            edge(rid, r.agent, "runs")
        return {"nodes": list(nodes.values()), "edges": edges}

    def to_dict(self) -> dict:
        files = self.files()
        arch = self.architecture()
        models = sorted({n["label"] for n in arch["nodes"] if n["kind"] == "llm"})
        from .archmap import Edge, Node, View

        keep = {"id", "label", "kind", "files", "detail"}
        mermaid = View(
            "ai",
            "Agents",
            [Node(**{k: v for k, v in n.items() if k in keep}) for n in arch["nodes"]],
            [Edge(e["source"], e["target"], e["label"]) for e in arch["edges"]],
        ).mermaid()
        return {
            "mermaid": mermaid,
            "files": [asdict(f) for f in files],
            "agents": [asdict(a) for a in self.agents.values()],
            "model_calls": [asdict(c) for c in self.calls],
            "runs": [asdict(r) for r in self.runs],
            "architecture": arch,
            "summary": {
                "files": len(files),
                "need_model": sum(1 for f in files if {"agents", "model calls"} & set(f.roles)),
                "agents": sum(1 for a in self.agents.values() if a.kind != "graph"),
                "workflows": sum(1 for a in self.agents.values() if a.kind == "graph"),
                "tools": len(
                    {(t["label"], t["file"]) for a in self.agents.values() for t in a.tools if not t.get("agent")}
                ),
                "model_calls": len(self.calls),
                "models": models,
                "frameworks": sorted({a.framework for a in self.agents.values()} | {s for f in files for s in f.sdks}),
            },
        }


def build(graph: CodeGraph, findings: list[Finding] | None = None) -> dict:
    return AiMap(graph, findings).to_dict()

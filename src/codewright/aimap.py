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

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tree_sitter import Node as TSNode
from tree_sitter import Query, QueryCursor

from .archmap import module_name
from .graph import PY, CodeGraph, ParsedFile, is_test_file, text
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
# A file can only run an agent with one of these calls (see `_scan_runs`).
_RUNS = re.compile(
    rb"\.(?:"
    + b"|".join(re.escape(m.encode()) for m in RUN_METHODS)
    + rb")\s*\(|\b(?:"
    + b"|".join(re.escape(r.encode()) for r in (*AGENT_RUNNERS, "run", "run_sync", "run_streamed"))
    + rb")\s*\("
)


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
    scope: str = "<module>"  # the function it is defined in, for agents built inside a factory
    instruction_full: str = ""  # the whole literal, for the inspector (instruction is shortened)
    options: dict = field(default_factory=dict)  # other keyword arguments as written, shortened
    ctor: str = ""  # the constructor as written (`LlmAgent`, `FanOutDesk`): ties a custom agent to its class


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
    kind: str = "runs"  # "runs" (a runner or .run()) or "builds" (calls a function that returns the agent)
    parallel: bool = False  # started alongside others (asyncio.gather, a TaskGroup, an executor): a fan-out
    via: str = ""  # the registry it was picked from (`WORKERS[name].ainvoke`), when it was


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


_CALLS_AND_ASSIGNMENTS = Query(PY, "[(call) (assignment)] @n")


def _calls_and_assignments(pf: ParsedFile) -> list[TSNode]:
    """Every call and assignment in the file, in the order a depth-first walk meets them. A native
    tree-sitter query is several times faster than walking every node in Python. Kept on the file
    while the AI map is built (it is read by several passes), then dropped."""
    cache = pf.tree_cache
    hit = cache.get("ai_nodes")
    if hit is None:
        caps = QueryCursor(_CALLS_AND_ASSIGNMENTS).captures(pf.root)
        hit = cache["ai_nodes"] = sorted(caps.get("n", []), key=lambda n: (n.start_byte, -n.end_byte))
    return hit


_MODEL_CTOR = re.compile(
    rb"\b(?:"
    + b"|".join(
        re.escape(c.encode())
        for c in (*LANGCHAIN_MODEL_CLASSES, "init_chat_model", "LiteLlm", "LiteLLM", "Gemini", "Claude")
    )
    + rb")\s*\("
)
PARALLEL_CALLS = ("gather", "as_completed", "wait", "create_task", "start_soon", "TaskGroup", "map", "submit")


def walk_returns(fnode: TSNode):
    """Return statements of a function, not those of functions nested in it."""
    stack = [fnode.child_by_field_name("body") or fnode]
    while stack:
        n = stack.pop()
        if n.type == "return_statement":
            yield n
            continue
        for c in n.children:
            if c.type not in ("function_definition", "class_definition", "decorated_definition", "lambda"):
                stack.append(c)


def _branch_values(ret: TSNode) -> list[TSNode]:
    """The values a return can give: both arms of `a if cond else b`, every item of a list of branches."""
    out, todo = [], list(ret.named_children[:1])
    while todo:
        v = todo.pop()
        if v.type == "conditional_expression":
            kids = v.named_children
            todo.extend([kids[0], kids[-1]] if len(kids) >= 3 else kids)
        elif v.type in ("list", "tuple"):
            todo.extend(v.named_children)
        else:
            out.append(v)
    return out


def _inside_parallel(call: TSNode) -> bool:
    """The call is built inside a list/generator or passed to gather/create_task: one of several started together."""
    p = call.parent
    while p is not None and p.type not in ("function_definition", "module", "block"):
        if p.type in ("list_comprehension", "generator_expression", "set_comprehension", "dictionary_comprehension"):
            return True
        if p.type == "call" and re.search(
            r"(gather|create_task|start_soon|submit|as_completed|wait)$", text(p.child_by_field_name("function"))
        ):
            return True
        p = p.parent
    return False


def _route_label(fn) -> str:
    """`POST /chat` for a function decorated `@app.post("/chat")`, else ""."""
    for d in fn.decorators:
        m = re.match(r"[\w.]*\.(get|post|put|patch|delete|websocket|api_route|route)\(\s*[\"']([^\"']*)", d)
        if m:
            verb = m.group(1).upper() if m.group(1) not in ("api_route", "route") else "ROUTE"
            return f"{verb} {m.group(2) or '/'}"
    return ""


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


def _has_args(call: TSNode) -> bool:
    callee, args = call.child_by_field_name("function"), call.child_by_field_name("arguments")
    return callee is not None and callee.type in ("identifier", "attribute") and bool(args and args.named_children)


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
            "BaseAgent": "custom",
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
        # Positions, not syntax nodes: a node keeps its whole file's tree in memory, and on a big repo the trees
        # do not all fit (see graph.TREE_BUDGET). `_at` finds the node again when it is needed.
        self._list_vars: dict[tuple[str, str], tuple[int, int]] = {}  # (file, var) -> span of a module-level list
        self._pending: list[tuple[Agent, ParsedFile, dict[str, TSNode], list[TSNode]]] = []
        self._by_node: dict[tuple[str, int], str] = {}  # (file, start byte of the constructor call) -> agent id
        self._factories: dict[str, str] = {}  # "file::function" -> id of the agent it returns
        self._assigns: list[tuple[str, str, str, str]] = []  # (file, scope, var, callee as written)
        self._factory_calls: list[tuple[str, int, int, str]] = []  # (file, start, end, callee) of calls with args
        self._aliases: dict[tuple[str, str], str] = {}  # (file, var) -> agent id, for `x = make_agent()`
        self._graph_vars: dict[tuple[str, str], str] = {}  # (file, var) -> graph id, for StateGraph and .compile()
        self.sdk_files: dict[str, list[str]] = {}
        self._run_names: tuple[set[str], re.Pattern] | None = None
        self._model_vars: dict[tuple[str, str], str] = {}  # (file, var) -> model literal, `llm = ChatOpenAI(model=...)`
        self._registry_nodes: dict[tuple[str, str], list[tuple[str, str, str]]] = {}  # (file, var) -> items written
        self.registries: dict[tuple[str, str], list[str]] = {}  # (file, var) -> agent ids it holds
        self.errors: list[dict] = []  # files a step failed on: skipped, so one odd file can't blank the page
        self._ref_memo: dict[tuple[str, str], tuple[str, int] | None] = {}
        self._mod_index: tuple[dict, dict] | None = None
        self._paths: list[str] = []
        for path, pf in graph.files.items():
            if is_test_file(path):
                continue
            labels = sorted({lbl for t in pf.module.imports.values() if (lbl := _ai_label(t))})
            if labels:
                self.sdk_files[path] = labels
        self._classes = self._agent_classes()
        # Files that build agents without importing an SDK themselves: they import a repo module that does
        # (an agent factory, a custom agent class, an agent defined elsewhere).
        core = frozenset(self.sdk_files) | {p for p, _ in self._classes}
        scan = set(core)
        if graph.resolver is not None:
            # One hop: files that import an AI file and call something from it (`build_agent()`,
            # `agents.root_agent.run(...)`). Growing `scan` while iterating chained this through the whole repo.
            for path, pf in graph.files.items():
                if path in scan or is_test_file(path):
                    continue
                if graph.resolver.imported_files(pf) & core:
                    scan.add(path)  # read only if it imports something AI-related (see `_exported` below)
        for path in sorted(scan):  # model variables first: a file may use one defined in a file scanned later
            self._guard(path, "reading model variables", self._scan_model_vars, graph.files[path])
        worth = self._worth_scanning_pattern()
        for path in sorted(p for p in scan if p in core):
            pf = graph.files[path]
            if worth.search(pf.source) or self._uses_imported_model(pf):
                self._guard(path, "reading agents and model calls", self._scan_file, pf)
        self._graph_factories()
        # Then the files that only import AI code: read one only if it imports something the AI files define
        # (an agent, a registry, a model, a factory, an agent class, a workflow).
        exported = self._exported()
        for path in sorted(p for p in scan if p not in core):
            pf = graph.files[path]
            if any(self._imported_var(pf, alias) in exported for alias in pf.module.imports):
                self._guard(path, "reading agents and model calls", self._scan_file, pf)
        self._graph_factories()
        self._resolve_aliases()
        self._factory_arguments()
        self._index()
        self._resolve_registries()
        for agent, pf, kw, pos in self._pending:
            self._guard(pf.path, f"linking agent {agent.name}", self._link, agent, pf, kw, pos)
        for a in self.agents.values():
            for st in a.steps:
                if st.get("ref") and (ref := self._agent_ref(graph.files[a.file], st["ref"], a.scope)) is not None:
                    st["agent"] = ref.id
        self._runner_vars: dict[tuple[str, str], str] = {}
        for path, pf in graph.files.items():
            if not is_test_file(path):
                self._guard(path, "finding code that runs agents", self._scan_runs, pf)
        if self._runner_vars:  # `runner.run_async(...)` in an endpoint runs the runner's agent
            for (path, var), aid in self._runner_vars.items():
                self._aliases.setdefault((path, var), aid)
                self._vars[var].append((path, aid))
            self._run_names = None
            rnames = {v for (_, v) in self._runner_vars}
            for path, pf in graph.files.items():
                if not is_test_file(path):
                    self._guard(path, "finding code that uses runners", self._scan_runs, pf, rnames)
        self._scan_builds()
        self._resolve_routers()
        self._name_graphs()
        for a in self.agents.values():  # `create_react_agent(llm, ...)`: show the model `llm` is
            a.model = self._model_label(graph.files[a.file], a.model) if a.model else a.model
        for path in scan:
            if graph.files[path].has_tree:
                graph.files[path].tree_cache.pop("ai_nodes", None)
        # Syntax nodes held past the build would keep every scanned file's tree alive for as long as the page.
        self._pending, self._factory_calls, self._list_vars, self._registry_nodes = [], [], {}, {}

    def _insights(self) -> list[dict]:
        from .agentreview import review

        try:
            return review(self)
        except Exception as exc:  # the review is advice: never take the page down with it
            self.errors.append(
                {"file": "", "step": "reviewing the architecture", "error": f"{type(exc).__name__}: {exc}", "at": ""}
            )
            return []

    def _exported(self) -> set[tuple[str, str]]:
        """(file, name) of everything AI-related the scanned files define at module level."""
        out = {(a.file, a.var) for a in self.agents.values() if a.scope == "<module>" and a.var}
        out |= set(self._registry_nodes) | set(self._model_vars) | set(self._graph_vars)
        out |= {(p, q.rsplit(".", 1)[-1]) for (p, q) in self._classes}
        for key in self._factories:
            path, _, fn = key.partition("::")
            if "#" not in fn and "." not in fn:
                out.add((path, fn))
        return out

    def _worth_scanning_pattern(self) -> re.Pattern:
        """Text a file must contain to define, run or prompt an agent or a model. Most files that import an AI
        library (a model's own code inside Transformers, say) contain none of it and are not parsed again."""
        names = [re.escape(q.rsplit(".", 1)[-1]) for (_, q) in self._classes]
        parts = [
            r"\w*(?:Agent|agent|Crew|AgentExecutor|Runner)\s*\(",
            r"\bStateGraph\b",
            r"\b(?:a?completion|embedding|init_chat_model)\s*\(",
            r"\b(?:" + "|".join(re.escape(c) for c in LANGCHAIN_MODEL_CLASSES) + r")\b",
            *(re.escape(suffix) + r"\s*\(" for suffix, _ in MODEL_CALL_SUFFIXES),
        ]
        if names:
            parts.append(r"\b(?:" + "|".join(sorted(set(names))) + r")\s*\(")
        return re.compile("|".join(parts).encode())

    def _uses_imported_model(self, pf: ParsedFile) -> bool:
        """`from .agents import fast` and then `fast.ainvoke(...)`: a chat model defined in another file."""
        return (
            any(self._imported_var(pf, a) in self._model_vars for a in pf.module.imports) if self._model_vars else False
        )

    def _guard(self, path: str, step: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # one file the scanner can't handle must not fail the whole AI page
            import logging
            import traceback

            where = traceback.extract_tb(exc.__traceback__)[-1]
            self.errors.append(
                {
                    "file": path,
                    "step": step,
                    "error": f"{type(exc).__name__}: {exc}",
                    "at": f"{Path(where.filename).name}:{where.lineno}",
                }
            )
            logging.getLogger("codewright").warning(
                "AI map: skipped %s while %s: %s\n%s", path, step, exc, traceback.format_exc()
            )

    # ----------------------------------------------------------------- models, registries, graph factories
    def _module_file(self, mod: str) -> str | None:
        """The repo file a dotted module name (as imported) refers to."""
        mod = mod.lstrip(".")
        if self._mod_index is None:
            self._find_function("", "")  # builds the index
        by_suffix, exact = self._mod_index
        cands = sorted(set(by_suffix.get(mod, ())) | set(exact.get(mod, ())))
        return self._paths[cands[0]] if cands else None

    def _imported_var(self, pf: ParsedFile, name: str) -> tuple[str, str] | None:
        """(file, variable) a name imported into `pf` refers to: `from .agents import fast` -> (agents.py, fast)."""
        head = name.split(".", 1)[0]
        target = pf.module.imports.get(head)
        if not target or "." not in target:
            return None
        if target.startswith("."):
            from .archmap import resolve_relative

            target = resolve_relative(target, pf.path)
        mod, _, var = target.rpartition(".")
        path = self._module_file(mod)
        return (path, var) if path else None

    @staticmethod
    def _at(pf: ParsedFile, span: tuple[int, int], kind: str | None = None) -> TSNode | None:
        """The node at a recorded byte span (the tree may have been parsed again since)."""
        n = pf.root.descendant_for_byte_range(span[0], span[1])
        while n is not None and (n.start_byte, n.end_byte) == span and kind and n.type != kind:
            n = n.parent
        while (
            n is not None
            and n.parent is not None
            and (n.parent.start_byte, n.parent.end_byte) == span
            and (kind is None or n.type != kind)
        ):
            n = n.parent  # the outermost node with exactly this span (`[a, b]`, not its first token)
        return n if n is not None and (n.start_byte, n.end_byte) == span else None

    def _scan_model_vars(self, pf: ParsedFile) -> None:
        """`llm = ChatOpenAI(model="gpt-4o")` at module level: `llm` is that model."""
        if not _MODEL_CTOR.search(pf.source):  # most files build no chat model: skip without the tree
            return
        for n in _calls_and_assignments(pf):
            if n.type != "assignment":
                continue
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier" or right.type != "call":
                continue
            callee = right.child_by_field_name("function")
            resolved = _resolve(pf, text(callee)) or text(callee)
            short = resolved.rsplit(".", 1)[-1]
            if short not in (*LANGCHAIN_MODEL_CLASSES, "init_chat_model", "LiteLlm", "LiteLLM", "Gemini", "Claude"):
                continue
            pos, kw = _args(right)
            node = kw.get("model") or kw.get("model_name") or kw.get("model_id") or (pos[0] if pos else None)
            self._model_vars[(pf.path, text(left))] = _model_text(node) or short

    def _model_label(self, pf: ParsedFile, written: str) -> str:
        """The model a variable stands for (`llm` -> `gpt-4o`), or what was written."""
        name = written.strip()
        if (pf.path, name) in self._model_vars:
            return self._model_vars[(pf.path, name)]
        ref = self._imported_var(pf, name)
        if ref in self._model_vars:
            return self._model_vars[ref]
        return written

    def _graph_factories(self) -> None:
        """`def build_graph(): g = StateGraph(...); ...; return g.compile()`: calling build_graph() gives that graph."""
        for a in list(self.agents.values()):
            if a.kind != "graph" or a.scope == "<module>":
                continue
            pf = self.g.files[a.file]
            fnode = pf.nodes.get(a.scope)
            if fnode is None:
                continue
            compiled = {a.var}
            for n in walk_returns(fnode):
                val = n.named_children[0] if n.named_children else None
                if val is None:
                    continue
                t = text(val)
                if t in compiled or re.match(rf"{re.escape(a.var)}\s*\.\s*compile\s*\(", t):
                    self._factories[f"{a.file}::{a.scope}"] = a.id
            for path, scope, var, callee in self._assigns:  # `app = g.compile()` then `return app`
                if path == a.file and scope == a.scope and callee == f"{a.var}.compile":
                    self._aliases[(path, var)] = a.id

    def _resolve_registries(self) -> None:
        """`WORKERS = {"research": researcher, "data": analyst}`: a dict or list of agents picked at run time."""
        for (path, var), items in self._registry_nodes.items():
            pf = self.g.files[path]
            ids = []
            for kind, written, callee in items:
                ref = self._agent_ref(pf, written) if kind != "call" else None
                if ref is None and kind == "call":
                    key = self._function_key(pf, callee)
                    ref = self.agents.get(self._factories.get(key or "", ""))
                if ref is not None and ref.id not in ids:
                    ids.append(ref.id)
            if ids:
                self.registries[(path, var)] = ids

    def _registry_of(self, pf: ParsedFile, name: str) -> tuple[str, list[str]] | None:
        """(registry name, agent ids) for a name that is a registry here or imported from where it is one."""
        if (pf.path, name) in self.registries:
            return name, self.registries[(pf.path, name)]
        ref = self._imported_var(pf, name)
        if ref in self.registries:
            return name, self.registries[ref]
        return None

    def _resolve_routers(self) -> None:
        """`add_conditional_edges("aggregate", needs_more)` without a mapping: branches are what the router returns."""
        for a in self.agents.values():
            if a.kind != "graph":
                continue
            pf = self.g.files[a.file]
            fixed = []
            for src, dst, label in a.step_edges:
                if dst != "?" or not label.startswith("if "):
                    fixed.append([src, dst, label])
                    continue
                router = label[3:]
                ref = self._function_ref(pf, router)
                targets: list[str] = []
                if ref is not None:
                    rpf = self.g.files[ref[0]]
                    fn = next((f for f in rpf.module.functions if f.def_line == ref[1]), None)
                    fnode = rpf.nodes.get(fn.qualname) if fn else None
                    for r in walk_returns(fnode) if fnode is not None else []:
                        for v in _branch_values(r):
                            s_ = _string(v)
                            name = s_ if s_ is not None else text(v).rsplit(".", 1)[-1]
                            if s_ is not None or name in ("END", "START") or name in {st["name"] for st in a.steps}:
                                targets.append(name)
                for t in dict.fromkeys(targets) or ["?"]:
                    fixed.append([src, t, label])
            a.step_edges = fixed

    def _name_graphs(self) -> None:
        """A graph named after what the code calls it: `workflow = build_graph()` -> "workflow", not "g"."""
        for a in self.agents.values():
            if a.kind != "graph":
                continue
            names = [var for (path, var), aid in self._aliases.items() if aid == a.id]
            if names:
                a.name = sorted(names, key=lambda v: (v in ("app", "graph", "g"), len(v)))[0]
            elif a.scope != "<module>":
                a.name = a.scope

    # ----------------------------------------------------------------- repo agent classes and references
    def _agent_classes(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Repo classes that are agents: they subclass an SDK agent class, or another such repo class."""
        found: dict[tuple[str, str], tuple[str, str]] = {}
        for _ in range(4):  # subclasses of subclasses
            grew = False
            for path, pf in self.g.files.items():
                if is_test_file(path):
                    continue
                for qual, bases in pf.classes.items():
                    if (path, qual) in found:
                        continue
                    for b in bases:
                        ctor = _agent_ctor(_resolve(pf, b) or "") or self._repo_class(pf, b, found)
                        if ctor:
                            found[(path, qual)] = ctor
                            grew = True
                            break
            if not grew:
                break
        return found

    def _repo_class(self, pf: ParsedFile, written: str, classes: dict | None = None) -> tuple[str, str] | None:
        """(framework, kind) when `written` names a repo agent class, here or through an import."""
        classes = self._classes if classes is None else classes
        if (pf.path, written) in classes:
            return classes[(pf.path, written)]
        target = _resolve(pf, written)
        if not target:
            return None
        mod, _, cls = target.rpartition(".")
        for (path, qual), ctor in classes.items():
            mn = module_name(path)
            if qual == cls and (
                mn == mod
                or mn.endswith("." + mod.lstrip("."))
                or mod.endswith("." + mn)
                or mod.lstrip(".") == mn.rsplit(".", 1)[-1]
            ):
                return ctor
        return None

    def _function_key(self, pf: ParsedFile, name: str) -> str | None:
        ref = self._function_ref(pf, name)
        if ref is None:
            return None
        other = self.g.files[ref[0]]
        fn = next((f for f in other.module.functions if f.def_line == ref[1]), None)
        return f"{ref[0]}::{fn.qualname}" if fn else None

    def _resolve_aliases(self) -> None:
        """`root_agent = build_agent()` and `app = graph.compile()`: the variable is that agent."""
        for _ in range(3):  # an alias of an alias
            for path, _scope, var, callee in self._assigns:
                if (path, var) in self._aliases:
                    continue
                pf = self.g.files[path]
                obj, _, method = callee.rpartition(".")
                if method == "compile" and (path, obj) in self._graph_vars:
                    self._aliases[(path, var)] = self._graph_vars[(path, obj)]
                    continue
                key = self._function_key(pf, callee)
                if key in self._factories:
                    self._aliases[(path, var)] = self._factories[key]

    def _factory_arguments(self) -> None:
        """`build_researcher("gemini-2.5-flash")`: an agent whose model is the factory's parameter gets the value
        the callers pass (all of them, when they differ)."""
        if not self._factories:
            return
        passed: dict[tuple[str, str], set[str]] = defaultdict(set)  # (factory key, param) -> literal values
        for path, start, end, callee in self._factory_calls:
            pf = self.g.files[path]
            key = self._function_key(pf, callee)
            if key not in self._factories:
                continue
            call = self._at(pf, (start, end), "call")
            if call is None:
                continue
            fpath, _, qual = key.partition("::")
            fn = next((f for f in self.g.files[fpath].module.functions if f.qualname == qual), None)
            if fn is None:
                continue
            params = [x for x in fn.params if x not in ("self", "cls")]
            pos, kw = _args(call)
            for i, node in enumerate(pos[: len(params)]):
                if (v := _string(node)) is not None:
                    passed[(key, params[i])].add(v)
            for k, node in kw.items():
                if (v := _string(node)) is not None:
                    passed[(key, k)].add(v)
        for a in self.agents.values():
            key = f"{a.file}::{a.scope}"
            if a.scope != "<module>" and a.model and (key, a.model) in passed:
                a.model = " | ".join(sorted(passed[(key, a.model)]))

    def _index(self) -> None:
        """var name -> [(file it is defined in, agent id)] for module-level agents and aliases."""
        self._vars: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for a in self.agents.values():
            if a.var and a.scope == "<module>":
                self._vars[a.var].append((a.file, a.id))
        for (path, var), aid in self._aliases.items():
            self._vars[var].append((path, aid))

    # ----------------------------------------------------------------- agents and model calls
    def _scan_file(self, pf: ParsedFile) -> None:
        nodes = _calls_and_assignments(pf)
        for n in nodes:
            if n.type == "assignment" and (left := n.child_by_field_name("left")) is not None:
                right = n.child_by_field_name("right")
                if right is not None and right.type == "list" and left.type == "identifier":
                    self._list_vars.setdefault((pf.path, text(left)), (right.start_byte, right.end_byte))
                if (
                    right is not None
                    and right.type in ("list", "dictionary", "tuple")
                    and left.type == "identifier"
                    and _enclosing_function(pf, n.start_point[0] + 1)[0] == "<module>"
                ):
                    items = [p.child_by_field_name("value") for p in right.named_children if p.type == "pair"]
                    items = items if right.type == "dictionary" else list(right.named_children)
                    plain = [
                        (it.type, text(it), text(it.child_by_field_name("function")) if it.type == "call" else "")
                        for it in items[:64]
                        if it is not None and it.type in ("identifier", "attribute", "call")
                    ]
                    if plain:  # names as written: resolved to agents once all files are read, with no tree
                        self._registry_nodes.setdefault((pf.path, text(left)), plain)
                if right is not None and left.type == "identifier":
                    call = right.named_children[0] if right.type == "await" and right.named_children else right
                    if call.type == "call" and (c := call.child_by_field_name("function")) is not None:
                        scope, _ = _enclosing_function(pf, n.start_point[0] + 1)
                        self._assigns.append((pf.path, scope, text(left), text(c)))
            if n.type == "call" and _has_args(n):
                self._factory_calls.append((pf.path, n.start_byte, n.end_byte, text(n.child_by_field_name("function"))))
        graphs: dict[str, Agent] = {}
        langchain = any(t.startswith("langchain") for t in pf.module.imports.values())
        lc_models: set[str] = {v for (p, v) in self._model_vars if p == pf.path}
        for alias in pf.module.imports:  # `from .agents import fast`: a chat model defined elsewhere
            if self._imported_var(pf, alias) in self._model_vars:
                lc_models.add(alias)
                langchain = True
        for n in nodes:
            if n.type != "call" or (callee := n.child_by_field_name("function")) is None:
                continue
            written = text(callee)
            resolved = _resolve(pf, written) or ""
            line = n.start_point[0] + 1
            pos, kw = _args(n)
            var = self._assigned_to(n)
            if ctor := (_agent_ctor(resolved) or self._repo_class(pf, written)):
                self._add_agent(pf, n, var, ctor, pos, kw)
                continue
            if resolved.endswith(".StateGraph") and resolved.startswith("langgraph"):
                gscope, _ = _enclosing_function(pf, line)
                gid = f"{pf.path}::{var or line}" if gscope == "<module>" else f"{pf.path}::{gscope}:{var or line}"
                graphs[var or str(line)] = self.agents[gid] = Agent(
                    gid, var or "graph", var or "graph", pf.path, line, "LangGraph", "graph", scope=gscope
                )
                if var:
                    self._graph_vars[(pf.path, var)] = gid
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
                    model = self._model_label(pf, obj)
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
        written_ctor = text(call.child_by_field_name("function"))
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
        scope, _ = _enclosing_function(pf, line)
        if scope == "<module>":
            aid = f"{pf.path}::{var}" if var else f"{pf.path}:{line}"
        else:  # built inside a function: a factory, or a local helper
            aid = f"{pf.path}::{scope}:{var or line}"
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
        agent.scope = scope
        self._by_node[(pf.path, call.start_byte)] = aid
        if scope != "<module>":
            parent = call.parent
            while parent is not None and parent.type in ("parenthesized_expression", "await"):
                parent = parent.parent
            body = text(pf.nodes.get(scope))
            if (parent is not None and parent.type == "return_statement") or (
                var and re.search(rf"\breturn\s+{re.escape(var)}\b", body)
            ):
                self._factories[f"{pf.path}::{scope}"] = aid
            elif f"{pf.path}::{scope}" not in self._factories:
                self._factories.setdefault(f"{pf.path}::{scope}#last", aid)
        agent.instruction_full = full
        agent.options = options
        agent.ctor = written_ctor
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
                {
                    "name": name,
                    "file": fn[0] if fn else pf.path,
                    "line": fn[1] if fn else target.start_point[0] + 1,
                    "ref": text(target) if target.type in ("identifier", "attribute") else "",
                }
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
        if target.startswith("."):  # `from .tools import search_web`: relative to this file's package
            from .archmap import resolve_relative

            target = resolve_relative(target, pf.path)
        mod, _, fname = target.rpartition(".")
        key = (mod, fname)
        if key not in self._ref_memo:
            self._ref_memo[key] = self._find_function(mod, fname)
        return self._ref_memo[key]

    def _find_function(self, mod: str, fname: str) -> tuple[str, int] | None:
        """The first file (in graph order) whose module name matches `mod` (equal, or one ends with the
        other) and defines `fname`. Uses an index of module names instead of scanning every file per call,
        which made the AI page take minutes on big repos."""
        if self._mod_index is None:
            by_suffix: dict[str, list[int]] = defaultdict(list)  # every dotted suffix of a module -> files
            exact: dict[str, list[int]] = defaultdict(list)
            self._paths = list(self.g.files)
            for i, path in enumerate(self._paths):
                mn = module_name(path)
                exact[mn].append(i)
                parts = mn.split(".")
                for k in range(len(parts)):
                    by_suffix[".".join(parts[k:])].append(i)
            self._mod_index = (by_suffix, exact)
        by_suffix, exact = self._mod_index
        cands = set(by_suffix.get(mod, ()))  # mn == mod or mn ends with "." + mod
        parts = mod.split(".")
        for k in range(1, len(parts)):  # mod ends with "." + mn
            cands.update(exact.get(".".join(parts[k:]), ()))
        for i in sorted(cands):
            path = self._paths[i]
            for f in self.g.files[path].module.functions:
                if f.qualname == fname:
                    return path, f.def_line
        return None

    def _agent_ref(self, pf: ParsedFile, name: str, scope: str = "<module>") -> Agent | None:
        """The agent `name` refers to from `pf`: local to the function, in this module, an alias
        (`x = make_agent()`), imported (`from app.agents import root_agent`, `agents.root_agent`)."""
        name = name.strip()
        if name.startswith(("self.", "cls.")):
            name = name.split(".", 1)[1]
        if scope != "<module>" and f"{pf.path}::{scope}:{name}" in self.agents:
            return self.agents[f"{pf.path}::{scope}:{name}"]
        if f"{pf.path}::{name}" in self.agents:
            return self.agents[f"{pf.path}::{name}"]
        if (pf.path, name) in self._aliases:
            return self.agents.get(self._aliases[(pf.path, name)])
        head, _, rest = name.partition(".")
        target = pf.module.imports.get(head)
        if target and rest:
            target = f"{target}.{rest}"
        real = (target or name).rsplit(".", 1)[-1]
        cands = self._vars.get(real, []) if hasattr(self, "_vars") else []
        if target and len(cands) > 1:
            mod = target.rpartition(".")[0].lstrip(".")
            narrowed = [
                c
                for c in cands
                if module_name(c[0]) == mod or module_name(c[0]).endswith("." + mod) or mod.endswith(module_name(c[0]))
            ]
            cands = narrowed or cands
        if cands and (target or len(cands) == 1):
            return self.agents.get(cands[0][1])
        return None

    def _items(self, pf: ParsedFile, node: TSNode) -> list[TSNode]:
        if node.type in ("list", "tuple"):
            return [c for c in node.named_children if c.type != "comment"]
        if node.type == "identifier" and (pf.path, text(node)) in self._list_vars:
            found = self._at(pf, self._list_vars[(pf.path, text(node))])
            return self._items(pf, found) if found is not None else [node]
        return [node]

    def _link(self, agent: Agent, pf: ParsedFile, kw: dict[str, TSNode], pos: list[TSNode]) -> None:
        for k in AGENT_KWARGS:
            if k not in kw:
                continue
            for item in self._items(pf, kw[k]):
                reg = (  # sub_agents=[*SPECIALISTS, summarizer]
                    self._registry_of(pf, text(item.named_children[0]))
                    if item.type == "list_splat" and item.named_children
                    else None
                )
                if reg is not None:
                    agent.children.extend(i for i in reg[1] if i != agent.id and i not in agent.children)
                    continue
                ref = self._agent_ref(pf, text(item), agent.scope) if item.type in ("identifier", "attribute") else None
                if ref is None and item.type == "call":
                    if (pf.path, item.start_byte) in self._by_node:  # an agent built inline in the list
                        ref = self.agents.get(self._by_node[(pf.path, item.start_byte)])
                    else:
                        _, ikw = _args(item)
                        inner = ikw.get("agent")
                        ref = self._agent_ref(pf, text(inner), agent.scope) if inner is not None else None
                        if ref is None and (key := self._function_key(pf, text(item.child_by_field_name("function")))):
                            ref = self.agents.get(self._factories.get(key, ""))  # sub_agents=[make_researcher()]
                if ref is not None and ref.id != agent.id:
                    agent.children.append(ref.id)
                else:
                    agent.child_names.append(_short(text(item), 50))
        for k in TOOL_KWARGS:
            if k not in kw:
                continue
            for item in self._items(pf, kw[k]):
                agent.tools.append(self._tool(pf, item, agent.scope))

    def _tool(self, pf: ParsedFile, item: TSNode, scope: str = "<module>") -> dict:
        raw = text(item)
        if item.type in ("identifier", "attribute"):
            if (ref := self._agent_ref(pf, raw, scope)) is not None:
                return {"label": ref.name, "agent": ref.id, "file": ref.file, "line": ref.line}
            if (fn := self._function_ref(pf, raw)) is not None:
                return {"label": raw.rsplit(".", 1)[-1], "file": fn[0], "line": fn[1]}
            return {"label": raw.rsplit(".", 1)[-1], "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}
        if item.type == "call" and (callee := item.child_by_field_name("function")) is not None:
            cpos, ckw = _args(item)
            wrapped = ckw.get("agent") or ckw.get("func") or (cpos[0] if cpos else None)
            cname = text(callee).rsplit(".", 1)[-1]
            if wrapped is not None and (pf.path, wrapped.start_byte) in self._by_node:  # AgentTool(agent=Agent(...))
                ref = self.agents[self._by_node[(pf.path, wrapped.start_byte)]]
                return {"label": ref.name, "agent": ref.id, "file": ref.file, "line": ref.line}
            if wrapped is not None and wrapped.type in ("identifier", "attribute"):
                if (ref := self._agent_ref(pf, text(wrapped), scope)) is not None:
                    return {"label": ref.name, "agent": ref.id, "file": ref.file, "line": ref.line}
                if (fn := self._function_ref(pf, text(wrapped))) is not None:
                    return {"label": text(wrapped).rsplit(".", 1)[-1], "file": fn[0], "line": fn[1]}
            server = _first_string(item)
            label = cname + (f": {_short(server, 40)}" if (server and "Toolset" in cname) or "MCP" in cname else "")
            return {"label": label, "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}
        return {"label": _short(raw, 40), "file": pf.path, "line": item.start_point[0] + 1, "builtin": True}

    # ----------------------------------------------------------------- who runs the agents
    def _scan_runs(self, pf: ParsedFile, only: set[str] | None = None) -> None:
        """Code that runs agents. With `only`, just calls on those variable names (a second pass for runners)."""
        if not self.agents:
            return
        names = set(self._vars) | {v for (_, v) in self.registries}
        if only is not None:
            names = set(only)
        names |= {a for a in pf.module.imports if self._imported_var(pf, a) in self.registries}
        if self._run_names is None or self._run_names[0] != names:
            self._run_names = (
                names,
                re.compile(rb"\b(?:" + b"|".join(re.escape(n).encode() for n in sorted(names)) + rb")\b"),
            )
        if self._run_names[1].search(pf.source) is None:  # one regex pass, not a substring scan per name
            return
        if only is None and _RUNS.search(pf.source) is None:  # names it, but never runs anything
            return
        parallel_fns: dict[str, bool] = {}
        for n in _calls_and_assignments(pf):
            if n.type != "call" or (callee := n.child_by_field_name("function")) is None:
                continue
            written = text(callee)
            obj, _, method = written.rpartition(".")
            pos, kw = _args(n)
            targets: list[Agent] = []
            via = ""
            line = n.start_point[0] + 1
            scope, _ = _enclosing_function(pf, line)
            if only is not None and obj.rsplit(".", 1)[-1] not in only:
                continue
            if obj and method in RUN_METHODS:
                if obj.rsplit(".", 1)[-1] in names and (t := self._agent_ref(pf, obj, scope)) is not None:
                    targets = [t]
                elif (picked := self._picked_from_registry(pf, n, callee.child_by_field_name("object"))) is not None:
                    via, ids = picked  # `WORKERS[name].ainvoke(...)` or `for a in AGENTS: a.ainvoke(...)`
                    targets = [self.agents[i] for i in ids if i in self.agents]
            if not targets:
                cand = kw.get("agent") or kw.get("starting_agent") or kw.get("root_agent")
                if (
                    cand is None
                    and pos
                    and written.rsplit(".", 1)[-1] in (*AGENT_RUNNERS, "run", "run_sync", "run_streamed")
                ):
                    cand = pos[0]
                if (
                    cand is not None
                    and cand.type in ("identifier", "attribute")
                    and text(cand).rsplit(".", 1)[-1] in names
                    and (t := self._agent_ref(pf, text(cand), scope)) is not None
                ):
                    targets = [t]
                    if (var := self._assigned_to(n)) and written.rsplit(".", 1)[-1] in AGENT_RUNNERS:
                        self._runner_vars[(pf.path, var)] = t.id  # `runner = Runner(agent=...)`
            if scope not in parallel_fns:
                parallel_fns[scope] = self._starts_in_parallel(pf, scope)
            for target in targets:
                if pf.path == target.file and line == target.line:
                    continue
                if scope == "<module>" and pf.path == target.file:
                    continue  # the agent module wiring itself up, not something running it
                parallel = parallel_fns[scope] and (bool(via) or _inside_parallel(n))
                how = _short(written, 50)
                self.runs.append(Runs(pf.path, scope, line, target.id, how, "runs", parallel, via))

    def _picked_from_registry(self, pf: ParsedFile, call: TSNode, obj: TSNode | None) -> tuple[str, list[str]] | None:
        if obj is None:
            return None
        if obj.type == "subscript" and (base := obj.child_by_field_name("value")) is not None:
            return self._registry_of(pf, text(base))
        if obj.type != "identifier":
            return None
        var = text(obj)
        p = call.parent
        while p is not None and p.type not in ("function_definition", "module"):
            clauses = [p] if p.type == "for_statement" else []
            if p.type.endswith("comprehension") or p.type == "generator_expression":
                clauses = [c for c in p.named_children if c.type == "for_in_clause"]  # siblings of the call
            for p_ in clauses:
                left, right = p_.child_by_field_name("left"), p_.child_by_field_name("right")
                if left is not None and right is not None and var in re.findall(r"\w+", text(left)):
                    src = re.sub(r"\.(values|items)\(\)$", "", text(right).strip())
                    if (hit := self._registry_of(pf, src)) is not None:
                        return hit
            p = p.parent
        return None

    def _starts_in_parallel(self, pf: ParsedFile, scope: str) -> bool:
        if scope == "<module>":
            return False
        fnode = pf.nodes.get(scope)
        body = text(fnode) if fnode is not None else ""
        return bool(
            re.search(
                r"\b(?:asyncio\.)?(?:gather|as_completed|wait|create_task|TaskGroup)\(|"
                r"\.(?:submit|map)\(|ThreadPoolExecutor|start_soon\(",
                body,
            )
        )

    def _scan_builds(self) -> None:
        """Code that calls an agent factory (`root = build_agent()`) builds that agent."""
        if not self._factories:
            return
        callers: dict[str, set[str]] = defaultdict(set)
        for caller, callees in self.g.edges.items():
            for c in callees:
                callers[c].add(caller)
        for key, aid in self._factories.items():
            if "#" in key:
                continue
            fname = key.rsplit("::", 1)[-1]
            for caller in sorted(callers.get(key, ())):
                path, _, fn = caller.partition("::")
                if is_test_file(path):
                    continue
                pfn = next((f for f in self.g.files[path].module.functions if f.qualname == fn), None)
                self.runs.append(Runs(path, fn, pfn.def_line if pfn else 1, aid, f"{fname}()", "builds"))
            for path, scope, var, callee in self._assigns:  # module-level `root_agent = build_agent()`
                if scope == "<module>" and self._aliases.get((path, var)) == aid and not is_test_file(path):
                    line = next(
                        (i for i, ln in enumerate(self.g.files[path].lines, 1) if callee in ln and var in ln), 1
                    )
                    self.runs.append(Runs(path, "<module>", line, aid, f"{var} = {callee}()", "builds"))

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

        runner_files = {r.file for r in self.runs}
        for path, af in out.items():
            roles = []
            if af.agents:
                roles.append("agents")
            if af.model_calls:
                roles.append("model calls")
            if af.tools:
                roles.append("tools")
            if path in runner_files:
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
                "custom": "custom agent class",
            }.get(a.kind, a.kind)
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
                    if s.get("agent"):  # a step that is itself an agent (create_react_agent, a subgraph)
                        edge(sid, s["agent"], "runs agent")
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
        # A graph step's own work (the agents it runs, the model it prompts) hangs off the step itself.
        step_of: dict[tuple[str, str], str] = {}
        for a in self.agents.values():
            for st in a.steps if a.kind == "graph" else []:
                spf = self.g.files.get(st["file"])
                fn = next((f for f in spf.module.functions if f.def_line == st["line"]), None) if spf else None
                if fn is not None:
                    step_of[(st["file"], fn.qualname)] = f"{a.id}#{st['name']}"
        self._step_of = dict(step_of)
        # A custom agent class's own methods (`_run_async_impl`) are that agent's work: hang it off the agent.
        owner_of: dict[tuple[str, str], str] = {}
        for a in self.agents.values():
            if a.kind not in ("custom", "llm") or not a.ctor:
                continue
            cls = a.ctor.rsplit(".", 1)[-1]
            for path, qual in self._classes:
                if qual.rsplit(".", 1)[-1] == cls:
                    for f in self.g.files[path].module.functions:
                        if f.qualname.startswith(qual + "."):
                            owner_of.setdefault((path, f.qualname), a.id)
        self._owner_of = owner_of
        step_of.update({k: v for k, v in owner_of.items() if k not in step_of})
        for c in self.calls:
            fid = step_of.get((c.file, c.function)) or f"fn:{c.file}::{c.function}"
            node(fid, c.function, "function", c.file, c.line, c.file, c.function)
            nodes[fid]["findings"] = fcount[c.file]
            edge(
                fid,
                model_node(c.model, c.provider if not c.model else _provider_of_model(c.model) or c.provider),
                "prompts",
            )
        apps = self.g.app_entries
        reach = self._app_reach()

        def app_node(path: str) -> str:
            e = apps[path]
            return node(f"app:{path}", e.trigger, "entry", path, e.line, f"{e.framework} · starts the app")["id"]

        agent_driven = {e["target"] for e in edges if e["label"] in ("delegates", "uses as tool", "runs agent")}
        for r in self.runs:
            if r.kind == "builds" and r.function == "<module>" and r.agent in agent_driven:
                continue  # `researcher = build_researcher()` wiring another agent's sub-agent: the delegation shows it
            if r.function == "<module>" and r.file in apps:
                rid = app_node(r.file)
            elif (r.file, r.function) in step_of:
                rid = step_of[(r.file, r.function)]
            else:
                rid = f"run:{r.file}::{r.function}"
                fn = next((f for f in self.g.files[r.file].module.functions if f.qualname == r.function), None)
                route = _route_label(fn) if fn is not None else ""
                node(
                    rid,
                    route or (r.function if r.function != "<module>" else module_name(r.file)),
                    "entry" if r.kind == "runs" or route else "function",
                    r.file,
                    r.line,
                    f"{r.file}:{r.line} · {r.function}() · {r.how}" if route else f"{r.file}:{r.line} · {r.how}",
                    r.function,
                )
                for path in reach.get(f"{r.file}::{r.function}", ()):
                    if f"app:{path}" != rid:
                        edge(app_node(path), rid, "starts")
            edge(rid, r.agent, ("runs in parallel" + (f" via {r.via}" if r.via else "")) if r.parallel else r.kind)
        for c in self.calls:  # apps that end up calling a model directly
            if (c.file, c.function) in step_of:
                continue
            for path in reach.get(f"{c.file}::{c.function}", ()):
                edge(app_node(path), f"fn:{c.file}::{c.function}", "starts")

        # Agents a framework loads by convention, with nothing in the repo running them.
        driven = {
            e["target"]
            for e in edges
            if e["label"] in ("runs", "builds", "delegates", "uses as tool", "runs agent")
            or e["label"].startswith("runs in parallel")
        }
        for a in self.agents.values():
            if a.id in driven or a.scope != "<module>":
                continue
            if a.framework == "Google ADK" and a.var == "root_agent":
                nid = node(
                    f"adk:{a.file}", "adk web / adk run", "entry", a.file, a.line, "Google ADK loads `root_agent`"
                )["id"]
                edge(nid, a.id, "runs")
        for name, (path, var) in self._langgraph_json().items():
            target = (
                self._aliases.get((path, var))
                or self._graph_vars.get((path, var))
                or (f"{path}::{var}" if f"{path}::{var}" in self.agents else None)
            )
            if target:
                nid = node(f"lg:{name}", f"langgraph dev · {name}", "entry", path, 1, "listed in langgraph.json")["id"]
                edge(nid, target, "runs")
        return {"nodes": list(nodes.values()), "edges": edges}

    def _app_reach(self, depth: int = 12) -> dict[str, list[str]]:
        """Function key -> the app entry files whose start-up reaches it through the call graph."""
        out: dict[str, list[str]] = defaultdict(list)
        for path, e in self.g.app_entries.items():
            if is_test_file(path):
                continue
            seen: set[str] = set()
            frontier = [*e.calls, f"{path}::<module>"]
            for _ in range(depth):
                nxt = []
                for key in frontier:
                    if key in seen:
                        continue
                    seen.add(key)
                    out[key].append(path)
                    nxt.extend(self.g.edges.get(key, ()))
                frontier = nxt
                if not frontier:
                    break
        return out

    def _langgraph_json(self) -> dict[str, tuple[str, str]]:
        """Graphs named in langgraph.json: name -> (file, variable)."""
        import json

        root = Path(self.g.root)
        out: dict[str, tuple[str, str]] = {}
        for cfg in [root / "langgraph.json", *root.glob("*/langgraph.json")]:
            try:
                graphs = json.loads(cfg.read_text()).get("graphs", {})
            except (OSError, ValueError, AttributeError):
                continue
            for name, spec in graphs.items():
                if isinstance(spec, str) and ":" in spec:
                    file, var = spec.rsplit(":", 1)
                    rel = (cfg.parent / file).resolve()
                    try:
                        out[name] = (rel.relative_to(root.resolve()).as_posix(), var)
                    except ValueError:
                        continue
        return out

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
            "insights": self._insights(),
            "errors": self.errors,
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

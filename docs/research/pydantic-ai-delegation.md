# pydantic-ai: multi-agent delegation, typed outputs, audit trail, and the Vocareum proxy

Research for issue [#2](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/2) (map: [#1](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/1)).

**Version this answer is correct for: `pydantic-ai` v2.42.0** — the latest release on PyPI at the time of writing
(`https://pypi.org/pypi/pydantic-ai/json` → `2.42.0`; GitHub tag `v2.42.0`, requires Python >= 3.10).

**pydantic-ai is NOT installed in this repo's virtualenv.** `.venv/lib/python3.14/site-packages` contains
`openai 1.76.0`, `pydantic 2.13.5`, `pandas`, `sqlalchemy`, `python-dotenv` and friends — no `pydantic_ai`.
The venv's Python is 3.14.6, which satisfies the `>=3.10` floor. So every API statement below comes from the
published docs at tag `v2.42.0`, not from introspecting an installed package.

**Docs URL note.** `ai.pydantic.dev/...` now 301-redirects to `pydantic.dev/docs/ai/...`. Citations below give
the site URL plus the versioned source file
(`https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/<file>.md`), which is the primary artifact.

---

## 1. Delegation mechanism

Source: <https://pydantic.dev/docs/ai/guides/multi-agent-applications/> ·
[`docs/multi-agent-applications.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/multi-agent-applications.md)

pydantic-ai names five levels of multi-agent complexity: single agent; **agent delegation**; **programmatic
agent hand-off**; graph-based control flow; and "deep agents".

- **Agent delegation** is the idiomatic mechanism for our topology. The docs define it as: *"the scenario where
  an agent delegates work to another agent, then takes back control when the delegate agent (the agent called
  from within a tool) finishes."* There is **no dedicated handoff primitive** in the core library — delegation is
  literally "the parent agent has a tool, and that tool body calls `await sub_agent.run(...)`".
- **Complete hand-off without return** is a different feature: *"If you want to hand off control to another agent
  completely, without coming back to the first agent, you can use an [output function](output.md#output-functions)."*
- **Agents are stateless and designed to be global**, so *"you do not need to include the agent itself in agent
  dependencies"* — module-level agent objects are the documented style.
- **A convenience wrapper exists but is not in the core package.** Pydantic AI Harness ships a `SubAgents`
  capability that exposes a single `delegate_task(agent_name, task)` tool, forwards dependencies, threads usage
  limits, and lists delegates in the system prompt. The docs say to *"write the delegation tool by hand … when you
  need control `SubAgents` doesn't give you — a bespoke tool schema, per-delegate argument validation, or passing
  the parent's message history through."* Harness is documented at `https://pydantic.dev/docs/ai/harness/` —
  **I did not verify which pip package provides it or whether it is installable in a Udacity-workspace context.
  Treat Harness as unconfirmed for this project; hand-written delegation tools are the safe path.**
- **The delegating tool must be `async def`.** *"That's required, not stylistic: `run_sync()` and
  `run_stream_sync()` cannot be used inside a tool, output function, or other function called during an agent run,
  and raise `UserError` there."* The **parent** may still be started with `run_sync()`. Blocking work inside the
  tool should go through `asyncio.to_thread()`.

### What comes back

`await sub_agent.run(...)` returns an `AgentRunResult` (`pydantic_ai.agent.AgentRunResult`), generic in the
output type. Relevant attributes/methods: `.output` (the validated `output_type` instance), `.usage()` →
`RunUsage`, `.all_messages()` / `.new_messages()` (+ `_json` variants), `.run_id`, `.conversation_id`
(<https://pydantic.dev/docs/ai/guides/output/>, <https://pydantic.dev/docs/ai/guides/message-history/>).

The tool returns a plain value from `r.output` — the tool's own return annotation is what the *parent's* model
sees. In the canonical example both are `list[str]`, so the tool just does `return r.output`.

### Minimal idiomatic example (verbatim from the docs, v2.42.0)

```python
from pydantic_ai import Agent, RunContext, UsageLimits

joke_selection_agent = Agent(
    'openai:gpt-5.2',
    name='joke_selection_agent',
    instructions=(
        'Use the `joke_factory` to generate some jokes, then choose the best. '
        'You must return just a single joke.'
    ),
)
joke_generation_agent = Agent(
    'google:gemini-3-flash-preview', name='joke_generation_agent', output_type=list[str]
)


@joke_selection_agent.tool
async def joke_factory(ctx: RunContext, count: int) -> list[str]:
    r = await joke_generation_agent.run(
        f'Please generate {count} jokes.',
        usage=ctx.usage,
    )
    return r.output


result = joke_selection_agent.run_sync(
    'Tell me a joke.',
    usage_limits=UsageLimits(request_limit=5, total_tokens_limit=500),
)
print(result.output)
print(result.usage)
#> RunUsage(cost=Decimal('0.00051200'), input_tokens=165, output_tokens=24, requests=3, tool_calls=1)
```

`name=` on each `Agent` is optional but recommended once more than one agent exists: it labels each run span in
Logfire, and otherwise the name is inferred from the assigned variable, falling back to `'agent'` for agents held
in a list or dict.

**Dependencies across the boundary:** *"Generally the delegate agent needs to either have the same dependencies as
the calling agent, or dependencies which are a subset."* Pass them explicitly: `await sub.run(..., deps=ctx.deps,
usage=ctx.usage)`.

**Cancellation** is run-scoped: *"a delegate agent cancelling itself surfaces to the parent as a failed tool return
rather than cancelling the parent, and a shared `CancellationToken` cancels a whole tree of runs at once."*

**Relevance to the orchestrator-never-acts principle (map #1):** delegation tools are still tools *on the
orchestrator*. Under the letter of "the orchestrator owns no tools", hand-written delegation tools are the only
thing it would own — they perform no database work, they only call domain agents. The design tickets should
decide explicitly whether "owns no tools" means "owns no *domain* tools" (which delegation satisfies) or something
stricter; pydantic-ai offers no way to delegate without either a tool or application-code hand-off.

---

## 2. Typed outputs

Source: <https://pydantic.dev/docs/ai/guides/output/> ·
[`docs/output.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/output.md);
retries: <https://pydantic.dev/docs/ai/guides/retries/> ·
[`docs/retries.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/retries.md)

- `Agent(..., output_type=X)`. `X` may be *"simple scalar types, list and dict types (including `TypedDict`s and
  `StructuredDict`s), dataclasses and Pydantic models, as well as type unions — generally everything supported as
  type hints in a Pydantic model. You can also pass a list of multiple choices."*
- Result reaches you as `result.output`, typed: *"Both `AgentRunResult` and `StreamedRunResult` are generic in the
  data they wrap, so typing information about the data returned by the agent is preserved."*
- **Unions register one output tool per member** — *"each member is registered with the model as a separate output
  tool in order to reduce the complexity of the schema and maximise the chances a model will respond correctly."*
  This is exactly the shape a blocker-signal protocol wants: `output_type=[QuoteApproved, QuoteBlocked]`, or
  `Result | Failed` as in the docs' flight example.
- Type-checker caveat: for unions you must write the generic explicitly and add `# type: ignore`, e.g.
  `Agent[object, FlightDetails | Failed](..., output_type=FlightDetails | Failed,  # type: ignore)`. Using a
  **list** instead of a union avoids the `# type: ignore` under pyright (mypy still needs the generic).
- Three output modes: **Tool Output** (default, wrap in `ToolOutput(...)` to rename/describe/set strict),
  **Native Output** (`NativeOutput(...)`, provider JSON-schema mode), **Prompted Output** (`PromptedOutput(...)`,
  schema injected into instructions and the text parsed). Mode markers are imported from `pydantic_ai`
  (`from pydantic_ai import Agent, ToolOutput` / `NativeOutput`).
- Also available: **output functions** (model supplies args, your function runs and its return is the output — the
  documented way to hand off completely), and **output validators** (`@agent.output_validator`) which may raise
  `ModelRetry`.

### Validation failure and retries

- A validation failure does **not** raise immediately: it becomes a retry prompt back to the model. Triggers for
  the output budget are *"validation failures, … an output function or output validator raising `ModelRetry`, and
  … a model response with nothing actionable in it."*
- Budgets are configured with one argument: `Agent('openai:gpt-5.2', retries=3)` sets both budgets; an
  `AgentRetries` dict sets them separately: `Agent('openai:gpt-5.2', retries={'tools': 5, 'output': 1})`.
  Unnamed keys keep **the default of `1`**. The same argument is accepted **per run** (`agent.run(..., retries=...)`)
  and via `agent.override()`.
- `max_retries=N` allows N retries, i.e. **N+1 attempts**; `max_retries=0` raises on the first failure.
- Per-output-tool override: `ToolOutput(Fruit, max_retries=2)`.
- **Exhausting a budget raises `UnexpectedModelBehavior`** (both the tool and output paths).
- Tool-side retries are triggered by *"a Pydantic `ValidationError` on the tool's arguments, by the tool (or its
  `args_validator`, or a tool hook) raising `ModelRetry`, by a tool timeout, and by the model calling a tool that
  doesn't exist."* The tool-retry counter is **keyed by tool name and resets on success** — there is no run-wide
  tool-retry budget, so a tool that alternates failure and success can fail many times in one run.
- `ToolFailed` is the deliberate opposite of `ModelRetry`: it records a `ToolReturnPart` with `outcome='failed'`
  and does **not** consume the retry budget, so repeated failures are bounded by `UsageLimits` instead.
- Note for the blocker protocol: a *business* blocker (out of stock, insufficient cash) should be a **typed output
  variant**, not a `ModelRetry` — `ModelRetry` means "you did it wrong, try again" and burns budget.

---

## 3. Usage and message history (audit trail)

Source: <https://pydantic.dev/docs/ai/guides/message-history/> ·
[`docs/message-history.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/message-history.md)

### Accessors

Both `AgentRunResult` (from `run`/`run_sync`) and `StreamedRunResult` (from `run_stream`) expose:

- `all_messages()` — *"all messages, including messages from prior runs"*; `all_messages_json()` → JSON bytes.
- `new_messages()` — *"only the messages from the current run"*; `new_messages_json()`.

For a streamed run, the final result message only appears once the stream has finished (and **never** with
`stream_text(delta=True)`).

### What the message objects contain

A history is a `list[ModelMessage]`, alternating `ModelRequest` and `ModelResponse`. Verbatim example output:

```python
[
    ModelRequest(
        parts=[UserPromptPart(content='Tell me a joke.', timestamp=datetime.datetime(...))],
        timestamp=datetime.datetime(...),
        instructions='Be a helpful assistant.',
        run_id='...',
        conversation_id='...',
    ),
    ModelResponse(
        parts=[TextPart(content='Did you hear about the toothpaste scandal? They called it Colgate.')],
        usage=RequestUsage(cost=Decimal('0.00026425'), input_tokens=55, output_tokens=12),
        model_name='gpt-5.2',
        timestamp=datetime.datetime(...),
        run_id='...',
        conversation_id='...',
    ),
]
```

So per message you get: `parts`, `timestamp`, `run_id`, `conversation_id`, plus `instructions` on requests and
`usage` + `model_name` on responses. **Per-response usage is on the message itself**, which is what makes a
per-agent cost/token breakdown possible from the transcript alone.

Part types seen in the docs: `SystemPromptPart`, `UserPromptPart`, `TextPart`, `ToolCallPart`, `ToolReturnPart`,
`RetryPromptPart`, `CompactionPart`, and multi-modal content parts. Retry semantics in history:
*"A retried tool call has no `ToolReturnPart` — the `RetryPromptPart` takes its place, carrying the same
`tool_call_id`. There is never both."* A `RetryPromptPart` carries either a string (from `ModelRetry`) or a list of
Pydantic error details (from a `ValidationError`); its `tool_name` is `None` when the retry belongs to the run's
output rather than a tool. Documented part sequence for one tool retry:

```
['UserPromptPart', 'ToolCallPart', 'RetryPromptPart', 'ToolCallPart', 'ToolReturnPart', 'TextPart']
```

That sequence is directly usable as an audit assertion.

### Correlation IDs

- `run_id` — unique per agent run, also on `RunContext.run_id` and `AgentRunResult.run_id`, emitted as OTel
  `gen_ai.agent.call.id`. **Never inherited from `message_history`.** You may mint your own:
  `agent.run_sync('...', run_id='run-from-api-42')`. Passing `run_id=''` or a `run_id` already in the history
  raises `UserError` (it breaks `new_messages()` boundary detection).
- `conversation_id` — *"shared across all runs that build on the same `message_history`"*, inherited when history
  round-trips, emitted as `gen_ai.conversation.id`. Override with `conversation_id='<id>'`; fork with
  `conversation_id='new'`.

For the Beaver's Choice audit trail these map cleanly: one customer request → one `conversation_id`; each agent
run (orchestrator and each delegate) → its own `run_id`; the structured `agent_steps` row keys on both.

### Serialisation

*"The intended way to do this is using a `TypeAdapter`. We export `ModelMessagesTypeAdapter`."*

```python
from pydantic_core import to_jsonable_python
from pydantic_ai import Agent, ModelMessagesTypeAdapter

history = result.all_messages()
as_python_objects = to_jsonable_python(history)
restored = ModelMessagesTypeAdapter.validate_python(as_python_objects)
```

`to_json(history)` + `ModelMessagesTypeAdapter.validate_json(...)` works directly for bytes — that is the JSONL
sidecar the map already decided on. Round-trip caveats the docs call out: `TextContent.metadata` is typed `Any`,
so a JSON round-trip normalises tuples to lists and datetimes to ISO strings (a `dump_python` round-trip preserves
them exactly); tool returns keyed by non-string keys only survive a `dump_python` round-trip.

The docs also recommend appending `new_messages()` per run rather than rewriting `all_messages()` each time,
*"so each write is proportional to the turn instead of to the conversation"*.

### Threading usage across a delegated sub-run — yes

- Pass `usage=ctx.usage` into the delegate's `run()`. Docs: *"Pass the usage from the parent agent to the delegate
  agent so the final `result.usage` includes the usage from both agents."* The documented dependency example shows
  `RunUsage(..., requests=4)` — *"2 from the calling agent and 2 from the delegate agent."*
- `UsageLimits` (`pydantic_ai.usage.UsageLimits`, re-exported from `pydantic_ai`) supports `cost_limit`,
  `request_limit`, `total_tokens_limit`, and `tool_calls_limit`, and is passed per run
  (`agent.run_sync(..., usage_limits=UsageLimits(request_limit=5, total_tokens_limit=500))`). In the programmatic
  hand-off example the same `usage_limits` object is passed to *each* agent run, and a shared `RunUsage()` object
  is threaded through the app.
- **Mixed models**: `result.usage` still accumulates per-response cost where it could be calculated, but
  *"monetary cost cannot be reconstructed from its aggregate token counts because models may have different pricing."*
- **Caveat**: inside a Temporal workflow a tool gets a *copy* of the run context, so `usage=ctx.usage` does not
  carry the delegate's usage back. Not relevant to this project (no Temporal), noted for completeness.

### Message history across the delegation boundary — read this carefully

Message history is **not** threaded automatically. The `SubAgents` note states: *"Each delegation runs in its own
run with its own message history, so a delegate never sees the parent conversation"*, and lists *"passing the
parent's message history through"* as one of the reasons to hand-write the tool. The parent's `all_messages()`
therefore records the delegation as a `ToolCallPart` + `ToolReturnPart` pair; the delegate's own transcript lives
on the `AgentRunResult` the tool holds locally, and **is discarded unless the tool captures it**.

*Partly inferred:* the docs state the runs are separate and that history must be passed explicitly, and separately
state that `all_messages()` returns "all messages, including messages from prior runs" (i.e. those passed in via
`message_history`) — I did not find a sentence that says in so many words "the parent's history does not contain
the delegate's messages". I could not confirm it by running code, since pydantic-ai is not installed. **Design
implication either way: the delegation tool should write the sub-run's `new_messages()` to the audit sidecar
itself**, keyed by the sub-run's `run_id`, before returning. That is also the natural place to write the
structured `agent_steps` row. Worth a 10-line empirical check once pydantic-ai is installed.

---

## 4. Custom base URL — the Vocareum proxy

Source: <https://pydantic.dev/docs/ai/models/openai/> ·
[`docs/models/openai.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/models/openai.md)

### Exact construction (v2.42.0)

```python
import os

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

model = OpenAIChatModel(
    'gpt-4o-mini',
    provider=OpenAIProvider(
        base_url='https://openai.vocareum.com/v1',
        api_key=os.environ['UDACITY_OPENAI_API_KEY'],
    ),
)
agent = Agent(model)
```

Docs, verbatim: *"To use another OpenAI-compatible API, you can set the `OPENAI_BASE_URL` and `OPENAI_API_KEY`
environment variables, or make use of the `base_url` and `api_key` arguments from `OpenAIProvider`."* Passing the
key explicitly from `UDACITY_OPENAI_API_KEY` is the clean way to honour the custom env var name without shadowing
`OPENAI_API_KEY`; `python-dotenv` is already in the venv to load `.env`.

### Version-sensitive naming — read this, it has moved

- `OpenAIChatModel` — Chat Completions wire format. *"`OpenAIChatModel` is also what backs every OpenAI-compatible
  provider … they all speak the Chat Completions wire format, so the same model class applies."* **This is the
  class to use for Vocareum.**
- `OpenAIResponsesModel` — the Responses API. In v2.42.0 the **bare `'openai:'` prefix resolves to
  `OpenAIResponsesModel`**, not to chat completions; `'openai-chat:'` resolves to `OpenAIChatModel`. So
  `Agent('openai:gpt-4o-mini')` with `OPENAI_BASE_URL` set would hit `/v1/responses`, not `/v1/chat/completions`.
- The older name `OpenAIModel` appears in pre-2.x material found on the web; in v2.42.0 the documented names are
  `OpenAIChatModel` and `OpenAIResponsesModel`. **Unconfirmed:** whether `OpenAIModel` still exists as a
  deprecated alias in v2.42.0 — I did not find it in the v2.42.0 docs and could not check the installed package.
  Do not rely on it.
- Import paths are stable and both are needed: `pydantic_ai.models.openai` for the model,
  `pydantic_ai.providers.openai` for the provider.

### Empirical probe of the proxy (run 2026-09-11 with the key in `.env`)

I exercised `https://openai.vocareum.com/v1` directly with `curl`:

| Endpoint | Result |
| --- | --- |
| `GET /v1/models` | **400** — `{"error":{"message":"Invalid Service /v1/models. Available Services Include: Completions, Embeddings, Images, Responses"}}` |
| `POST /v1/chat/completions` with `tools` + `strict: true` | **200**, model echoed `gpt-4o-mini-2024-07-18`, returned a well-formed `tool_calls` array and a `usage` block |
| `POST /v1/responses` | **200**, well-formed Responses payload |

Conclusions:

- **Tool calling works through the proxy, including strict function schemas.** That is the single capability
  pydantic-ai's default Tool Output mode and its function tools depend on, so the default configuration should
  work — no need for `NativeOutput` or `PromptedOutput` fallbacks, and no need to set
  `openai_supports_strict_tool_definition=False` in an `OpenAIModelProfile`.
- `usage` is returned, so token accounting will populate. Cost figures depend on pydantic-ai's own price table for
  the model name; **unconfirmed** whether `RunUsage.cost` is populated for a non-OpenAI `base_url`.
- **`/v1/models` is blocked.** pydantic-ai does not list models during a normal run, so this should not matter —
  but it does mean the available model catalogue cannot be enumerated. `gpt-4o-mini` is confirmed working.
  **Unconfirmed:** which other model names Vocareum accepts.
- **End-to-end through pydantic-ai itself is unverified** — the package is not installed, so this is a wire-level
  compatibility result, not a library-level one. The remaining risk is small and concentrated in model-profile
  selection: pydantic-ai picks a `ModelProfile` from the model name, and with an unknown-to-it base URL it falls
  back to the OpenAI profile, which matches what the proxy actually serves. If anything misbehaves, the documented
  escape hatch is `profile=OpenAIModelProfile(...)` with knobs including `json_schema_transformer`,
  `openai_supports_strict_tool_definition`, `openai_chat_supports_multiple_system_messages`, and
  `openai_chat_supports_max_completion_tokens`.

Note also: `OpenAIProvider` accepts an `openai_client=AsyncOpenAI(...)` if the proxy ever needs custom headers.
The OpenAI SDK client does its own retries (`max_retries=2` by default, so one model request can reach the network
up to three times), stacking on top of any agent-level retry budget.

---

## 5. Tool definition conventions

Source: <https://pydantic.dev/docs/ai/guides/tools/> ·
[`docs/tools.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/tools.md);
deps: <https://pydantic.dev/docs/ai/guides/dependencies/> ·
[`docs/dependencies.md@v2.42.0`](https://github.com/pydantic/pydantic-ai/blob/v2.42.0/docs/dependencies.md)

Three registration routes, verbatim:

- `@agent.tool` — *"for tools that need access to the agent context"*. **This is the default**: *"`@agent.tool` is
  considered the default decorator since in the majority of cases tools will need access to the agent context."*
  First parameter is `ctx: RunContext[DepsT]`.
- `@agent.tool_plain` — *"for tools that do not need access to the agent context"*.
- `tools=[...]` on the `Agent` constructor — plain functions (signature inspected to decide whether they take
  `RunContext`) or `Tool(fn, takes_ctx=True/False)` instances for finer control (custom name, description,
  `prepare` callback). `Tool` is importable from `pydantic_ai`.

Plus `toolsets=[...]` for collections (MCP servers, third-party bundles); internally *"all `tools` and `toolsets`
are gathered into a single combined toolset"*.

### Schema derivation

*"Function parameters are extracted from the function signature, and all parameters except `RunContext` are used
to build the schema for that tool call."* Docstrings are parsed with **griffe** for the description and per-parameter
descriptions; `google`, `numpy` and `sphinx` styles are supported, auto-detected, and settable via
`docstring_format=`. `require_parameter_descriptions=True` raises `UserError` on a missing one.

Precisely three parts of the docstring reach the model: *"the leading description, the parameter descriptions, and
the first entry of the returns section. Other sections Griffe can parse, such as `Raises`, `Examples`, `Notes`,
`Warnings` and `Yields`, are dropped"*. Worth baking into the tool-writing convention for this project.

Tool return values may be *"anything that Pydantic can serialize to JSON."*

### Dependency injection

`Agent(..., deps_type=MyDeps)`; pass the instance per run as `agent.run(..., deps=deps)`; read it inside tools as
`ctx.deps`. *"Dependencies can be any python type … dataclasses are generally a convenient container when your
dependencies included multiple objects."* Deps also reach dynamic system prompts and output validators. For this
project the obvious deps payload is the SQLAlchemy engine plus the current request id / conversation id and the
audit-sidecar writer — which is also what the delegation tool forwards with `deps=ctx.deps`.

Example of the canonical shape:

```python
@agent.tool
def get_player_name(ctx: RunContext[str]) -> str:
    """Get the player's name."""
    return ctx.deps
```

---

## 6. Comparison: smolagents and npcsh

### smolagents (Hugging Face)

Primary sources: <https://huggingface.co/docs/smolagents/en/conceptual_guides/intro_agents> and
<https://huggingface.co/docs/smolagents/en/examples/multiagents>

smolagents' organising idea is the **code agent**: at each step the LLM writes *Python code* as its action rather
than a JSON tool call. The docs argue this from cited papers — code gives better *"Composability … Object
management … Generality … Representation in LLM training data"* — and place "Code Agents" at the top of their
agency ladder (*"LLM acts in code, can define its own tools / start other agents"*). `ToolCallingAgent` is the
JSON-tool-calling sibling, recommended for *"a single-timeline task that does not require parallel tool calls"*.
The loop is an explicit ReAct memory loop: `while llm_should_continue(memory): action = llm_get_next_action(memory)`.

Multi-agent in smolagents is **hierarchical management via `managed_agents`**. A sub-agent is given `name` and
`description` — *"mandatory attributes to make this agent callable by its manager agent"* — and handed to the
manager at construction: `CodeAgent(tools=[], model=model, managed_agents=[web_agent])`. The manager then invokes
the sub-agent from inside the Python it writes, as if it were a callable.

**How it differs from pydantic-ai:** the sub-agent call is generated *as code by the model* and executed in a
(sandboxable, import-allowlisted) interpreter, rather than being a typed Python function you wrote. The contract
between manager and delegate is a **natural-language task string in and a natural-language report out** — there is
no `output_type`, no Pydantic validation of the hand-back, and hence no validation-triggered retry of a structured
result. Delegation is declarative (`managed_agents=[...]`) rather than hand-wired, which is more convenient but
gives you less control over the interface. For a project graded on typed inter-agent contracts and an auditable
trail, that's the crux of the difference: smolagents optimises for *expressive autonomy*, pydantic-ai for
*checkable contracts*. smolagents' code-execution surface is also an added safety and reproducibility concern that
this project does not need.

### npcsh (NPC Worldwide)

Primary source: <https://github.com/NPC-Worldwide/npcsh> (README)

npcsh is primarily a **shell** — an agentic command-line environment — rather than a library you compose agents in.
Agents are **NPCs**, defined declaratively in `.npc` YAML files (or `agents.md` / an `agents/` directory) with
fields like `name`, `primary_directive`, `model`, `provider`, and `jinxes`. NPCs are grouped into **teams**, and
a team's `team.ctx` supplies default model/provider that individual NPCs inherit. Tools are **jinxs** — *"custom
Jinja Execution templates"* — i.e. templated executions rather than typed Python functions. Routing is by
**addressing an NPC by name in the shell**, e.g. `npcsh> @corca refactor the auth module and add tests`; the README
also describes an orchestrating role over a team. Model access goes through LiteLLM, so *"any model provider that
LiteLLM supports"*, configured via `~/.npcshrc` env vars (`NPCSH_CHAT_MODEL`, `NPCSH_CHAT_PROVIDER`) and standard
provider keys.

**How it differs from pydantic-ai:** the unit of composition is a **declarative NPC file plus a shell session**,
not typed Python objects; the tool abstraction is a **Jinja template**, not a function whose JSON schema is derived
from its signature and docstring; and inter-agent hand-off is **name addressing inside the shell**, with no typed
result object, no `deps_type` injection, and no framework-level usage threading or serialisable message-history
API. It is the most operator-facing of the three and the least suited to a graded, programmatically-driven batch
harness like `run_test_scenarios()`.

*Scope note:* I read the npcsh README as the primary source and did not exhaust its docs site. Statements above
about what npcsh *lacks* (typed results, usage threading) are "not documented in the README", not "proven absent".

---

## Open items / unconfirmed

1. **pydantic-ai is not installed.** Everything here is docs-derived at v2.42.0. First implementation step should
   be `uv pip install pydantic-ai` (or `pydantic-ai-slim[openai]`) and a smoke test against Vocareum.
2. **Whether the parent's `all_messages()` includes a delegate's messages** — reasoned to be "no", not proven.
   Design around capturing the sub-run's `new_messages()` in the delegation tool regardless.
3. **`RunUsage.cost` population against a non-OpenAI `base_url`** — unknown.
4. **Vocareum's accepted model catalogue** — `/v1/models` returns 400; only `gpt-4o-mini` is confirmed working.
5. **Pydantic AI Harness (`SubAgents`, `StepPersistence`)** — documented, but packaging/availability unverified.
   Do not design a dependency on it.
6. **Whether `OpenAIModel` survives as a deprecated alias in v2.42.0** — not found in the v2.42.0 docs; use
   `OpenAIChatModel`.

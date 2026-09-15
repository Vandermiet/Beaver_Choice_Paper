"""The Beaver's Choice multi-agent system.

One orchestrator and four domain agents — inventory, quoting, sales,
replenishment — built against the design locked on the wayfinder map
(issue #1). `project_starter.py` imports and wires this package; it does not
house the system.

Module boundaries:

- `contract` — the shared kernel: the canonical envelope, the blocker
  protocol, the carried-catalogue validator.
- `audit`    — the audit trail the orchestrator owns.
- `llm`      — model construction against the Vocareum proxy.
- `starter`  — the single import point for the provided harness helpers.
- `orchestrator` — the delegation sequence and the customer-facing reply.
- one package per domain agent, each holding its `models`, its `tools` and
  its `agent` construction.
"""

# The workflow at a glance

Five agents and the flows between them. No tools, no helpers, no tables — for
those, [`workflow-diagram-detailed.md`](workflow-diagram-detailed.md) draws the same system in
full.

One **orchestrator** owns the plan and every customer-facing word. It never
touches a system of record itself. Around it sit **four domain agents**, each
owning exactly one question and answering nothing else; none of them delegates,
so every arrow below starts or ends at the orchestrator. The sequence is fixed —
resolve, price, commit — with one bounded retry: if sales declines a line for
want of stock, the orchestrator sends replenishment to buy it in and offers the
line to sales once more. There is no third pass.

```mermaid
flowchart TB
    CUSTOMER(["Customer — one prose enquiry + the date it arrived"])

    ORCHESTRATOR["ORCHESTRATOR<br/>reads the enquiry, drives the sequence,<br/>writes the one reply"]

    INVENTORY["INVENTORY<br/>do we carry this, and how short are we?"]
    QUOTING["QUOTING<br/>what does each resolved line cost?"]
    SALES["SALES<br/>can we fill this line right now?"]
    REPLENISHMENT["REPLENISHMENT<br/>can we buy in what was refused,<br/>in time and within our means?"]

    CUSTOMER --> ORCHESTRATOR

    ORCHESTRATOR -->|"1 · resolve these requested lines"| INVENTORY
    INVENTORY -->|"resolved lines, shortfalls, revision questions"| ORCHESTRATOR

    ORCHESTRATOR -->|"2 · price the resolved lines"| QUOTING
    QUOTING -->|"priced lines, order total"| ORCHESTRATOR

    ORCHESTRATOR -->|"3 · commit, pass 1"| SALES
    SALES -->|"committed lines + declines"| ORCHESTRATOR

    ORCHESTRATOR -->|"4 · buy in the stock declines"| REPLENISHMENT
    REPLENISHMENT -->|"what is on its way, and from when"| ORCHESTRATOR

    ORCHESTRATOR -.->|"5 · commit once more — only the lines we bought for"| SALES

    ORCHESTRATOR ==>|"one letter: prices, dates, and every refusal in the customer's own terms"| CUSTOMER
```

Read the numbers as the order the orchestrator drives them in. Steps 4 and 5 run
only when pass 1 declined a line for stock the customer can still be sold; the
dashed arrow is the retry, and it is the only arrow that can be skipped or
taken twice.

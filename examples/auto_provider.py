"""Auto-provider mode: "use whatever provider is available right now".

Set a fallback order once (globally or per-agent). caw selects the first
*installed* provider and, on the first send, transparently moves to the next
one if that provider fails (CLI missing, auth expired) or is rate-limited —
your code never has to catch an exception or pick a provider by hand.

The order can come from (highest priority first):

* an explicit list on the Agent: ``Agent(provider=["claude", "codex"])``
* the global setting: ``caw.set_provider_order([...])`` with ``provider="auto"``
* the ``CAW_PROVIDER`` env var as a comma list: ``CAW_PROVIDER="claude,codex"``

Tip: in auto mode use a ``ModelTier`` (or no model) rather than a concrete model
string — tiers are re-resolved per provider, so model selection stays portable
across the fallback. A bare concrete model string is dropped when falling back to
a different provider (it would be meaningless to the others). If you want a
specific model *per provider* that survives fallback, attach it to the order
itself with ``set_provider_order([(name, model), ...])`` (see below).
"""

import caw
from caw import Agent, ModelTier, check_providers


def main():
    # --- Global order, used by provider="auto" (or by Agent() with no provider). ---
    caw.set_provider_order(["opencode", "codex", "claude"])

    agent = Agent(provider="auto", model=ModelTier.STRONGEST)

    # Selection is a fast, no-network check — the first installed provider wins.
    print("Installed providers, in order:")
    for h in check_providers(["claude", "codex", "opencode"]):
        mark = "✓" if h.installed else "✗"
        print(f"  [{mark}] {h.provider}")
    print(f"\nAuto-selected provider: {agent.provider.name}\n")

    # Just use it. If the selected provider errors or is rate-limited on the
    # first send, caw silently falls back to the next installed one in the order.
    traj = agent.completion("Reply with a one-line hello and include your model name and agent harness name.")
    print(f"Provider used: {traj.agent}")
    print(f"Agent reply: {traj.result}")

    # --- Per-agent order (overrides the global setting). ---
    other = Agent(provider=["codex", "claude"])
    print(f"\nPer-agent order selected: {other.provider.name}")

    # --- Per-provider models in the order. ---
    # Attach a model to each provider: a ModelTier (re-resolved per provider) or a
    # concrete string. Unlike a bare Agent-level model string, these are bound to
    # their provider, so each is used even when reached as a fallback. Applied only
    # when the Agent sets no model of its own.
    caw.set_provider_order([("opencode", "openai/gpt-5.5"), ("codex", ModelTier.STRONGEST), ("claude", "opus")])
    pinned = Agent(provider="auto")
    print(f"Per-provider models: {caw.get_provider_models()}")
    print(f"Pinned-order selected: {pinned.provider.name}")


if __name__ == "__main__":
    main()

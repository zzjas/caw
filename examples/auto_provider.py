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
across the fallback. A concrete model string is dropped when falling back to a
different provider (it would be meaningless to the others).
"""

import caw
from caw import Agent, ModelTier, check_providers


def main():
    # --- Global order, used by provider="auto" (or by Agent() with no provider). ---
    caw.set_provider_order(["claude", "codex", "opencode"])

    agent = Agent(provider="auto", model=ModelTier.STRONGEST)

    # Selection is a fast, no-network check — the first installed provider wins.
    print("Installed providers, in order:")
    for h in check_providers(["claude", "codex", "opencode"]):
        mark = "✓" if h.installed else "✗"
        print(f"  [{mark}] {h.provider}")
    print(f"\nAuto-selected provider: {agent.provider.name}\n")

    # Just use it. If the selected provider errors or is rate-limited on the
    # first send, caw silently falls back to the next installed one in the order.
    traj = agent.completion("Reply with a one-line hello and include your provider name.")
    print(f"Provider used: {traj.agent}")
    print(f"Agent reply: {traj.result}")

    # --- Per-agent order (overrides the global setting). ---
    other = Agent(provider=["codex", "claude"])
    print(f"\nPer-agent order selected: {other.provider.name}")


if __name__ == "__main__":
    main()

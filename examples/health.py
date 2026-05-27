"""Checking provider health / availability at runtime.

caw reports *raw signals* about each provider — it forms no verdict on what
counts as "available", so you compose your own predicate from the fields.

Two depths of check:

* **fast** (default): is the CLI installed, and what can we cheaply learn about
  its credentials?  No network, no token cost — safe to call at startup.
* **live** (``live=True``): also round-trips a tiny probe to confirm the
  provider responds and isn't rate-limited.  Costs one request per provider.

See also the ``caw doctor`` CLI command, which prints this as a table.
"""

from caw import Agent, check_providers


def main():
    # --- Fast sweep over every registered provider (no token cost). ---
    print("Provider health (fast check):\n")
    for h in check_providers():
        auth = h.auth.detail if h.auth else "unknown"
        print(f"  {h.provider:12} installed={h.installed!s:5} auth={auth}")

    # Compose your own "available" predicate from the raw signals. Here:
    # installed, and (if we can tell) the credential token isn't expired.
    def is_usable(h) -> bool:
        return h.installed and not (h.auth and h.auth.token_expired)

    usable = [h.provider for h in check_providers() if is_usable(h)]
    print(f"\nUsable right now (installed + non-expired creds): {usable}")

    # --- Health of a specific agent's provider. ---
    health = Agent(provider="claude_code").check_health()
    print(f"\nclaude_code → installed={health.installed} binary={health.binary_path}")

    # --- Live probe (uncomment to actually round-trip; costs a request each). ---
    # for h in check_providers(["claude", "codex"], live=True):
    #     if h.rate_limited:
    #         print(f"  {h.provider}: rate-limited, ~{h.wait_minutes}m until reset")
    #     elif h.error:
    #         print(f"  {h.provider}: probe failed: {h.error}")
    #     else:
    #         print(f"  {h.provider}: responds OK")


if __name__ == "__main__":
    main()

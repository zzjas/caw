"""Abstract base classes for provider implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from caw.logger import AgentLogger
from caw.models import InteractiveResult, MCPServer, ModelTier, ToolGroup, Trajectory, Turn

if TYPE_CHECKING:
    from caw.health import AuthSignal, ProviderHealth


class ProviderSession(ABC):
    """ABC that each provider implements to manage a live session."""

    @abstractmethod
    def send(self, message: str) -> Turn:
        """Send a message and return the agent's response turn."""
        ...

    @abstractmethod
    def end(self) -> Trajectory:
        """Finalize the session and return the complete trajectory."""
        ...

    @property
    @abstractmethod
    def trajectory(self) -> Trajectory:
        """The accumulated trajectory so far."""
        ...

    def detect_usage_limit(self, turn: Turn) -> int | None:
        """Check whether *turn* indicates the provider's usage limit was hit.

        Returns the number of minutes to wait before retrying, or ``None``
        if no limit was detected.  Override in provider subclasses to
        implement provider-specific detection logic.
        """
        return None

    @property
    def session_id(self) -> str | None:
        """Provider-assigned session ID (if any)."""
        return None

    @property
    def resume_key(self) -> str | None:
        """The provider-side key needed to resume this conversation.

        This is whatever the backend CLI accepts to continue an existing
        session (claude's session id, codex's thread id, opencode's session
        id).  ``None`` until the backend has assigned one — typically after the
        first :meth:`send`.
        """
        return None

    def _restore_from_trajectory(self, trajectory: Trajectory) -> None:
        """Re-seed a freshly built session with a prior trajectory's state.

        Used by :meth:`Provider.resume_session` so a session reconstructed in a
        new process carries the original history, usage totals, and creation
        time forward.  All concrete provider sessions share these attribute
        names; resume-specific keys (thread id, etc.) are restored by the
        provider's own ``resume_session``.
        """
        self._turns = list(trajectory.turns)
        self._total_usage = trajectory.usage
        self._total_duration_ms = trajectory.duration_ms
        if trajectory.created_at:
            self._created_at = trajectory.created_at
        self._has_sent = True

    @property
    def last_raw_output(self) -> str | None:
        """Raw CLI stdout from the most recent send() call (if available)."""
        return None

    def set_step_callback(self, callback: Callable[[list], None] | None) -> None:
        """Set callback invoked after each step within send()."""
        pass  # default no-op; concrete providers override

    def set_logger(self, logger: AgentLogger | None) -> None:
        """Attach a generic logger that receives a one-line summary per event."""
        pass  # default no-op; concrete providers override


class Provider(ABC):
    """ABC that each coding agent backend implements."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'claude_code', 'codex')."""
        ...

    def resolve_model(self, tier: ModelTier) -> str:
        """Translate a :class:`ModelTier` into a concrete model identifier.

        Each provider must override this to map abstract tiers (e.g.
        ``ModelTier.STRONGEST``) to the actual model string it supports.
        """
        raise NotImplementedError(
            f"{self.name} provider does not implement resolve_model(); "
            f"pass an explicit model string instead of ModelTier.{tier.name}"
        )

    def resolve_tool_restrictions(self, tools: ToolGroup) -> dict[str, Any]:
        """Translate ToolGroup into provider-specific session kwargs.

        Receives a concrete ToolGroup value (never None — the Agent layer
        applies the default before calling this).
        """
        return {}

    def check_limit(self, model: str | None = None) -> int | None:
        """Probe whether the provider's usage limit is currently active.

        Sends a minimal test prompt and checks if the response indicates a
        usage-limit.  Returns the estimated number of minutes to wait before
        the limit resets, or ``None`` if no limit is detected.

        This incurs a small token cost for the probe request.
        """
        from caw.display import get_global_display, set_global_display

        old_display = get_global_display()
        set_global_display(None)
        try:
            session = self.start_session(
                mcp_servers=[],
                model=model,
                system_prompt="Reply with the single word 'ok'.",
                **self._limit_probe_kwargs(),
            )
            try:
                turn = session.send("hi")
                return session.detect_usage_limit(turn)
            finally:
                session.end()
        finally:
            set_global_display(old_display)

    def _limit_probe_kwargs(self) -> dict[str, Any]:
        """Extra session kwargs for the limit-check probe.

        Override in subclasses to disable tools and minimise side-effects.
        """
        return {}

    # ------------------------------------------------------------------
    # Health / availability
    # ------------------------------------------------------------------

    @property
    def binary_name(self) -> str:
        """Name of the backing CLI executable (as looked up on ``PATH``).

        Override in subclasses.  Used by the default :meth:`find_binary`.
        """
        raise NotImplementedError(f"{self.name} provider does not declare a binary_name.")

    def find_binary(self) -> str | None:
        """Resolve the CLI executable's path, or ``None`` if not installed.

        Default looks up :attr:`binary_name` on ``PATH``.  Override to add
        provider-specific fallback locations.
        """
        import shutil

        return shutil.which(self.binary_name)

    def check_auth(self) -> "AuthSignal | None":  # noqa: F821 (forward ref)
        """Best-effort, non-authoritative introspection of credentials.

        Returns an :class:`~caw.health.AuthSignal`, or ``None`` when the
        provider cannot introspect its credentials at all.  Override in
        subclasses; the default returns ``None`` (unknown).
        """
        return None

    def check_health(self, *, live: bool = False, model: str | None = None) -> "ProviderHealth":  # noqa: F821
        """Report raw health signals for this provider.

        Fast by default: checks whether the CLI is installed and introspects
        credentials.  When ``live`` is True, additionally runs the
        :meth:`check_limit` probe (one request) to confirm the provider
        responds and whether it is currently rate-limited.

        Forms no verdict on "availability" — see :class:`caw.health.ProviderHealth`.
        """
        from caw.health import ProviderHealth

        binary = self.find_binary()
        health = ProviderHealth(
            provider=self.name,
            installed=binary is not None,
            binary_path=binary,
            auth=self.check_auth(),
        )
        if live and health.installed:
            health.probed = True
            try:
                wait = self.check_limit(model=model)
                health.rate_limited = wait is not None
                health.wait_minutes = wait
            except Exception as exc:  # noqa: BLE001 — surface as a signal, not a raise
                health.error = str(exc)
        return health

    def start_interactive(
        self, initial_prompt: str, mcp_servers: list[MCPServer], capture_bytes: int = 0, **kwargs: Any
    ) -> InteractiveResult:
        """Launch the provider binary interactively with an initial prompt.

        Hands control to the user's terminal — stdin/stdout/stderr are
        inherited so the user interacts with the agent directly.
        A copy of stdout is captured via a pty.

        Returns an :class:`InteractiveResult` with the exit code and
        captured output.
        """
        raise NotImplementedError(f"{self.name} provider does not support interactive mode.")

    @abstractmethod
    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> ProviderSession:
        """Create and return a new provider session."""
        ...

    def resume_key_from_trajectory(self, trajectory: Trajectory) -> str | None:
        """Extract the resume key from a persisted *trajectory*.

        Mirrors :attr:`ProviderSession.resume_key` but reads from a stored
        trajectory rather than a live session.  Returns ``None`` if the
        trajectory predates resume support or was never sent to.
        """
        return None

    def resume_session(
        self,
        mcp_servers: list[MCPServer],
        *,
        session_id: str,
        resume_key: str,
        trajectory: Trajectory | None = None,
        **kwargs: Any,
    ) -> ProviderSession:
        """Rebuild a live session ready to continue an existing conversation.

        ``resume_key`` is the provider-side key the backend CLI needs to resume
        (see :attr:`ProviderSession.resume_key`); ``session_id`` is caw's own
        bookkeeping id (the on-disk directory name).  The next
        :meth:`ProviderSession.send` must resume rather than start fresh.

        When ``trajectory`` is given, the prior history/usage is carried forward
        via :meth:`ProviderSession._restore_from_trajectory`.  When it is
        ``None`` (e.g. resuming without a ``data_dir``), the backend session is
        still resumed but caw's trajectory starts empty.
        """
        raise NotImplementedError(f"{self.name} provider does not support resuming sessions.")

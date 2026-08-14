"""Abstract base classes for provider implementations."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from caw.logger import AgentLogger
from caw.models import InteractiveResult, MCPServer, ModelTier, ToolGroup, Trajectory, Turn

if TYPE_CHECKING:
    from caw.health import AuthSignal, ProviderHealth

logger = logging.getLogger(__name__)


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
        first `send`.
        """
        return None

    def _restore_from_trajectory(self, trajectory: Trajectory) -> None:
        """Re-seed a freshly built session with a prior trajectory's state.

        Used by `Provider.resume_session` so a session reconstructed in a
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

    #: Options every provider accepts. ``session_id`` and the tool-restriction
    #: keys are injected by the Agent layer rather than passed by users.
    SESSION_OPTIONS: ClassVar[frozenset[str]] = frozenset(
        {"model", "system_prompt", "session_id", "reasoning", "cwd"}
    )

    #: Options this provider understands on top of `SESSION_OPTIONS`. Anything
    #: an Agent is given that appears in neither set is reported by
    #: `warn_unknown_options` instead of being dropped in silence.
    EXTRA_SESSION_OPTIONS: ClassVar[frozenset[str]] = frozenset()

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'claude_code', 'codex')."""
        ...

    def warn_unknown_options(self, kwargs: dict[str, Any]) -> None:
        """Log a warning for session options this provider will ignore.

        Provider sessions read the options they know by name out of
        ``**kwargs``, so anything misspelled — or supported by one backend and
        not another — used to vanish without a trace. That is a bad failure
        mode for a library whose pitch is swapping providers: the call keeps
        working and quietly stops doing what it says. Now it is visible.
        """
        known = self.SESSION_OPTIONS | self.EXTRA_SESSION_OPTIONS
        unknown = sorted(k for k in kwargs if k not in known)
        if unknown:
            logger.warning(
                "%s: ignoring unsupported session option(s): %s — this backend "
                "does not implement them, so they will have no effect.",
                self.name,
                ", ".join(unknown),
            )

    def resolve_cwd(self, cwd: Any) -> str | None:
        """Validate a working directory before it reaches ``subprocess``.

        Checked here rather than left to `subprocess.Popen`, because a missing
        directory makes Popen raise `FileNotFoundError` — which every provider
        catches and reports as "the CLI is not installed". A typo in a path
        would send you off reinstalling a CLI you already have.
        """
        if cwd is None:
            return None
        path = os.fspath(cwd)
        if not os.path.isdir(path):
            raise NotADirectoryError(f"cwd is not a directory: {path}")
        return path

    def resolve_model(self, tier: ModelTier) -> str | None:
        """Translate a `ModelTier` into a concrete model identifier, if needed.

        Each provider must override this to map abstract tiers (e.g.
        ``ModelTier.STRONGEST``) to the actual model string it supports. Return
        ``None`` when the provider's own default/config should select the tier.
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

        Defaults to the provider `name`; override when the CLI binary is
        named differently (e.g. ``claude_code`` → ``claude``).  Used by the
        default `find_binary`.
        """
        return self.name

    def find_binary(self) -> str | None:
        """Resolve the CLI executable's path, or ``None`` if not installed.

        Default looks up `binary_name` on ``PATH``.  Override to add
        provider-specific fallback locations.
        """
        import shutil

        return shutil.which(self.binary_name)

    def check_auth(self) -> "AuthSignal | None":  # noqa: F821 (forward ref)
        """Best-effort, non-authoritative introspection of credentials.

        Returns an `AuthSignal`, or ``None`` when the
        provider cannot introspect its credentials at all.  Override in
        subclasses; the default returns ``None`` (unknown).
        """
        return None

    def check_health(self, *, live: bool = False, model: str | None = None) -> "ProviderHealth":  # noqa: F821
        """Report raw health signals for this provider.

        Fast by default: checks whether the CLI is installed and introspects
        credentials.  When ``live`` is True, additionally runs the
        `check_limit` probe (one request) to confirm the provider
        responds and whether it is currently rate-limited.

        Forms no verdict on "availability" — see `caw.health.ProviderHealth`.
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

        Returns an `InteractiveResult` with the exit code and
        captured output.
        """
        raise NotImplementedError(f"{self.name} provider does not support interactive mode.")

    @abstractmethod
    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> ProviderSession:
        """Create and return a new provider session."""
        ...

    def resume_key_from_trajectory(self, trajectory: Trajectory) -> str | None:
        """Extract the resume key from a persisted *trajectory*.

        Mirrors `ProviderSession.resume_key` but reads from a stored
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
        (see `ProviderSession.resume_key`); ``session_id`` is caw's own
        bookkeeping id (the on-disk directory name).  The next
        `ProviderSession.send` must resume rather than start fresh.

        When ``trajectory`` is given, the prior history/usage is carried forward
        via `ProviderSession._restore_from_trajectory`.  When it is
        ``None`` (e.g. resuming without a ``data_dir``), the backend session is
        still resumed but caw's trajectory starts empty.
        """
        raise NotImplementedError(f"{self.name} provider does not support resuming sessions.")

"""Multi-account channel support — typed shape for ``accounts:`` map.

OpenClaw lets a single channel adapter (e.g. Telegram) carry multiple
authenticated accounts side-by-side, each with its own credentials and
its own routing target. OC's historical model is one account per
channel plugin instance; this module ships the typed shape +
resolution helper that channel adapters subclass into to opt in.

Two concrete classes are exposed:

* :class:`ChannelAccountsConfig` — frozen dataclass, the loaded shape.
* :class:`MultiAccountChannel` — ABC that adapters subclass to
  declare they support multi-account routing. The base
  :class:`BaseChannelAdapter` keeps the single-account contract;
  multi-account is opt-in to preserve backwards compatibility for
  every existing bundled plugin.

The router/gateway side reads ``ChannelAccountsConfig.accounts`` and
routes inbound events to the matching ``account_id`` before consulting
the bindings table. ``account_id`` becomes part of the
:class:`opencomputer.agent.bindings_config.BindingMatch` so users can
write ``account_id: work-bot -> work-profile`` rules.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

#: Schema field name (matches openclaw's JSON config + parity-doctor spec).
ACCOUNTS_FIELD: str = "accounts"


@dataclass(frozen=True, slots=True)
class ChannelAccount:
    """One configured account for a channel.

    Keep the surface minimal — channel-specific credential shapes
    (botToken / webhookUrl / etc.) live under ``params`` rather than
    bloating the SDK type. Each adapter parses ``params`` itself.
    """

    id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelAccountsConfig:
    """Typed shape for the ``accounts:`` map of a channel.

    Loaded from ``config.yaml`` like::

        channels:
          telegram:
            accounts:
              personal-bot:
                bot_token: ${TELEGRAM_PERSONAL_TOKEN}
              work-bot:
                bot_token: ${TELEGRAM_WORK_TOKEN}

    Empty / missing → ``ChannelAccountsConfig(accounts={})`` (no multi-
    account routing for that channel — adapter falls back to its
    historical single-account auth).
    """

    accounts: dict[str, ChannelAccount] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensive: account ids must be non-empty strings. Block at
        # construction time so a malformed config can't poison routing.
        for acc_id, acc in self.accounts.items():
            if not isinstance(acc_id, str) or not acc_id.strip():
                raise ValueError(
                    f"channel account id must be a non-empty string, got {acc_id!r}"
                )
            if not isinstance(acc, ChannelAccount):
                raise ValueError(
                    f"account {acc_id!r}: value must be a ChannelAccount, "
                    f"got {type(acc).__name__}"
                )
            if acc.id != acc_id:
                raise ValueError(
                    f"account id mismatch: map key {acc_id!r} but "
                    f"ChannelAccount.id is {acc.id!r}"
                )

    @classmethod
    def from_raw(cls, raw: Any) -> ChannelAccountsConfig:
        """Parse from a free-form mapping (e.g. ``yaml.safe_load``).

        ``None`` / missing / non-dict → empty config (graceful default).
        Per-account values that aren't dicts are skipped with no entry
        (we don't raise because a single misconfigured account
        shouldn't take down every other account on the channel).
        """
        if not isinstance(raw, dict):
            return cls()
        out: dict[str, ChannelAccount] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if not isinstance(v, dict):
                continue
            out[k] = ChannelAccount(id=k, params=dict(v))
        return cls(accounts=out)

    def get(self, account_id: str) -> ChannelAccount | None:
        """Lookup by id. ``None`` when not configured."""
        return self.accounts.get(account_id)

    def ids(self) -> list[str]:
        """Sorted list of configured account ids — stable for display."""
        return sorted(self.accounts)


class MultiAccountChannel(abc.ABC):
    """Mixin ABC declaring a channel adapter supports multi-account routing.

    Adapters inherit this alongside :class:`BaseChannelAdapter` to
    advertise the capability. The gateway looks for
    :func:`isinstance(adapter, MultiAccountChannel)` before splitting
    inbound events by ``account_id``.

    Subclasses MUST implement :meth:`resolve_account_id` to extract
    the account identifier from an incoming raw event (e.g. for
    Telegram, the bot username that received the message).
    """

    @abc.abstractmethod
    def resolve_account_id(self, raw_event: Any) -> str | None:
        """Return the account id this event belongs to, or ``None``.

        ``None`` means "fall through to the channel's default
        single-account path" — preserves backwards compatibility for
        events that don't fit the multi-account model.
        """

    def configured_accounts(self) -> ChannelAccountsConfig:
        """Default: no accounts. Subclasses override to surface the
        actual loaded config so the gateway can list / dispatch.

        Returning ``ChannelAccountsConfig()`` (the empty default)
        keeps a subclass single-account-equivalent unless it opts in
        by overriding both this AND :meth:`resolve_account_id`.
        """
        return ChannelAccountsConfig()


__all__ = [
    "ACCOUNTS_FIELD",
    "ChannelAccount",
    "ChannelAccountsConfig",
    "MultiAccountChannel",
]

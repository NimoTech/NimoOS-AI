# NimoOS-AI/agent/channels/manager.py
"""ChannelManager — owns adapter lifecycles for enabled channel instances.
reload() diffs the DB against running adapters: stops removed/disabled/
reconfigured ones, starts newly enabled ones. Called at startup and after
any instance management write."""
from __future__ import annotations

import json
import logging

from channels import store
from channels.discord import DiscordAdapter
from channels.telegram import TelegramAdapter

_LOG = logging.getLogger("nimoos-agent.channels.manager")

ADAPTERS: dict[str, type] = {"telegram": TelegramAdapter, "discord": DiscordAdapter}


class ChannelManager:
    def __init__(self, conn, router, adapters: dict[str, type] | None = None):
        self._conn = conn
        self._router = router
        self._adapters = adapters if adapters is not None else ADAPTERS
        # instance_id -> (adapter, config_json fingerprint)
        self._running: dict[str, tuple] = {}

    async def start_all(self) -> None:
        await self.reload()

    async def reload(self) -> None:
        rows = store.list_instances(self._conn)
        want = {r["id"]: r for r in rows
                if r["enabled"] and r["channel_type"] in self._adapters}
        for iid in list(self._running):
            row = want.get(iid)
            if row is None or row["config_json"] != self._running[iid][1]:
                adapter, _fp = self._running.pop(iid)
                try:
                    await adapter.stop()
                except Exception:
                    _LOG.exception("failed stopping channel instance %s", iid)
        for iid, row in want.items():
            if iid in self._running:
                continue
            cls = self._adapters[row["channel_type"]]
            try:
                adapter = cls(iid, json.loads(row["config_json"]),
                              self._router.handle,
                              on_callback=self._router.handle_confirm)
                await adapter.start()
            except Exception:
                _LOG.exception("failed starting channel instance %s", iid)
                continue
            self._running[iid] = (adapter, row["config_json"])

    async def stop_all(self) -> None:
        for adapter, _fp in self._running.values():
            try:
                await adapter.stop()
            except Exception:
                pass
        self._running.clear()

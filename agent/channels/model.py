# NimoOS-AI/agent/channels/model.py
"""Channel-agnostic message model. Platform quirks (length limits, markdown
dialects, media forms) must be absorbed inside adapters; the router and sink
layers only ever see these types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar


@dataclass
class InboundAttachment:
    filename: str
    mime: str
    tmp_path: str
    size: int


@dataclass
class InboundMessage:
    channel_type: str
    instance_id: str
    external_chat_id: str
    external_user_id: str
    external_username: str | None
    message_id: str
    text: str
    reply_to: str | None = None
    raw: dict = field(default_factory=dict)
    attachments: "list[InboundAttachment]" = field(default_factory=list)


@dataclass
class OutboundMessage:
    text: str


@dataclass
class ChannelCapabilities:
    max_text_len: int
    supports_edit: bool = False
    supports_buttons: bool = False
    supports_typing: bool = False
    supports_media: bool = False


# async (adapter, message) -> None
InboundHandler = Callable[["ChannelAdapter", InboundMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    channel_type: ClassVar[str]
    capabilities: ClassVar[ChannelCapabilities]

    def __init__(self, instance_id: str, config: dict[str, Any],
                 on_inbound: InboundHandler, on_callback=None):
        self.instance_id = instance_id
        self._config = config
        self._on_inbound = on_inbound
        self._on_callback = on_callback

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, external_chat_id: str,
                   msg: OutboundMessage) -> str | None: ...

    async def send_typing(self, external_chat_id: str) -> None:
        return None

    async def send_file(self, external_chat_id: str, path: str,
                        caption: str = "") -> str | None:
        import logging
        logging.getLogger("nimoos-agent.channels").warning(
            "send_file not supported by %s", self.channel_type)
        return None

    async def send_buttons(self, external_chat_id: str, text: str,
                           buttons: "list[tuple[str, str]]") -> str | None:
        """buttons = [(label, callback_data), ...]. Default: unsupported →
        warn + None (caller treats None as 'deny')."""
        import logging
        logging.getLogger("nimoos-agent.channels").warning(
            "send_buttons not supported by %s", self.channel_type)
        return None

    async def edit_to_resolved(self, external_chat_id: str, message_id: str,
                               text: str) -> None:
        """Replace a button message with a plain resolution line and drop the
        buttons. Default: no-op."""
        return None


def split_text(text: str, max_len: int) -> list[str]:
    """Split into <=max_len chunks, preferring newline then space boundaries
    in the second half of the window; hard cut when none exist."""
    if not text:
        return []
    chunks: list[str] = []
    rest = text
    while len(rest) > max_len:
        window = rest[:max_len]
        cut = window.rfind("\n")
        if cut < max_len // 2:
            cut = window.rfind(" ")
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut].rstrip("\n "))
        rest = rest[cut:].lstrip("\n ")
    if rest:
        chunks.append(rest)
    return chunks

from typing import Any, Dict

from discord.gateway import DiscordVoiceWebSocket

from .voice_client import VoiceClient


__all__ = ("hook",)


async def hook(self: DiscordVoiceWebSocket, msg: Dict[str, Any]):
    # TODO: implement other voice events
    op: int = msg["op"]
    data: Dict[str, Any] = msg.get("d", {})
    vc: VoiceClient = self._connection

    if not isinstance(vc, VoiceClient):
        return

    if op == DiscordVoiceWebSocket.SPEAKING:
        vc.update_ssrc(data)

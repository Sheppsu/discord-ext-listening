from typing import Any, Dict

from .voice_client import DiscordVoiceWebSocket, VoiceClient


async def gateway_hook(self: DiscordVoiceWebSocket, msg: Dict[str, Any]):
    # TODO: implement other voice events
    op: int = msg["op"]
    data: Dict[str, Any] = msg.get("d", {})
    vc: VoiceClient = self._connection

    if not isinstance(vc, VoiceClient):
        return

    if op == DiscordVoiceWebSocket.SPEAKING:
        vc.update_ssrc(data)

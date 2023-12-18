from discord.opus import Decoder as BaseDecoder

__all__ = ("Decoder",)


class Decoder(BaseDecoder):
    def packet_get_nb_channels(self, data: bytes) -> int:
        return self.CHANNELS

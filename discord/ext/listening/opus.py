from discord.opus import Decoder as BaseDecoder

__all__ = ("Decoder",)


class Decoder(BaseDecoder):
    # The base method returns wrong number of channels
    def packet_get_nb_channels(self, data: bytes) -> int:
        return self.CHANNELS

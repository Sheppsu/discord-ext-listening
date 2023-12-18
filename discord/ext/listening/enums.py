from discord.enums import Enum

__all__ = ("RTCPMessageType",)


class RTCPMessageType(Enum):
    sender_report = 200
    receiver_report = 201
    source_description = 202
    goodbye = 203
    application_defined = 204

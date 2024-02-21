import asyncio
import logging
import os
import queue
import struct
import threading
from collections import defaultdict
from concurrent.futures import Future
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple, Union

from discord.object import Object

from .enums import RTCPMessageType
from .opus import Decoder as OpusDecoder

if TYPE_CHECKING:
    from discord.member import Member


__all__ = (
    "AudioFrame",
    "AudioSink",
    "AudioHandlingSink",
    "AudioFileSink",
    "AudioFile",
    "WaveAudioFile",
    "MP3AudioFile",
    "RTCPPacket",
    "RTCPSenderReportPacket",
    "RTCPReceiverReportPacket",
    "RTCPSourceDescriptionPacket",
    "RTCPGoodbyePacket",
    "RTCPApplicationDefinedPacket",
    "RTCPReceiverReportBlock",
    "RTCPSourceDescriptionChunk",
    "RTCPSourceDescriptionItem",
)


SILENT_FRAME = b"\xf8\xff\xfe"
_log = logging.getLogger(__name__)


@dataclass
class RTCPReceiverReportBlock:
    """Receiver report block from :class:`RTCPSenderReportPacket`
    or :class:`RTCPReceiverReportPacket`

    Conveys statistics on the reception of RTP packets from a single synchronization source.

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/19.htm

    Attributes
    ----------
    ssrc: :class:`int`
        The SSRC identifier of the source to which the information in this
        reception report block pertains.
    f: :class:`int`
        The fraction of RTP data packets from source SSRC lost since the
        previous SR or RR packet was sent.
    c: :class:`int`
        The total number of RTP data packets from source SSRC that have
        been lost since the beginning of reception.
    ehsn: :class:`int`
        The low 16 bits contain the highest sequence number received in an RTP
        data packet from source SSRC, and the most significant 16 bits extend
        that sequence number with the corresponding count of sequence number cycles.
    j: :class:`int`
        An estimate of the statistical variance of the RTP data packet interarrival
        time, measured in timestamp units and expressed as an unsigned integer.
    lsr: :class:`int`
        The middle 32 bits out of 64 in the NTP timestamp received as part of the most
        recent RTCP sender report (SR) packet from source SSRC. If no SR has been
        received yet, the field is set to zero.
    dlsr: :class:`int`
        The delay, expressed in units of 1/65536 seconds, between receiving the last
        SR packet from source SSRC and sending this reception report block. If no
        SR packet has been received yet from SSRC, the DLSR field is set to zero.
    """

    __slots__ = (
        "ssrc",
        "f",
        "c",
        "ehsn",
        "j",
        "lsr",
        "dlsr",
    )

    ssrc: int
    f: int
    c: int
    ehsn: int
    j: int
    lsr: int
    dlsr: int


@dataclass
class RTCPSourceDescriptionItem:
    """An item of a :class:`RTCPSourceDescriptionChunk` object

    Attributes
    ----------
    cname: :class:`int`
        Type of description.
    description: :class:`bytes`
        Description pertaining to the source of the chunk containing this item.
    """

    __slots__ = (
        "cname",
        "description",
    )

    cname: int
    description: bytes


@dataclass
class RTCPSourceDescriptionChunk:
    """A chunk of a :class:`RTCPSourceDescriptionPacket` object.

    Contains items that describe a source.

    Attributes
    ----------
    ssrc: :class:`int`
        The source which is being described.
    items: Sequence[:class:`RTCPSourceDescriptionItem`]
        A sequence of items which have a description.
    """

    __slots__ = (
        "ssrc",
        "items",
    )

    ssrc: int
    items: Sequence[RTCPSourceDescriptionItem]


class RTCPPacket:
    """Base class for all RTCP packet classes. Contains header attributes.

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/19.htm

    Attributes
    ----------
    v: :class:`int`
        Identifies the version of RTP, which is the same in RTCP packets
        as in RTP data packets.
    p: :class:`bool`
        If the padding bit is set, this RTCP packet contains some additional
        padding octets at the end which are not part of the control information.
        The last octet of the padding is a count of how many padding octets
        should be ignored.
    rc: :class:`int`
        Indicates the number of "items" within a packet. For sender and receiver
        packets it indicates the number of Receiver Report Blocks.
    pt: :class:`RTCPMessageType`
        Indicates the RTCP packet type.
    l: :class:`int`
        The length of this RTCP packet in 32-bit words minus one, including
        the header and any padding.
    """

    __slots__ = (
        "v",
        "p",
        "rc",
        "pt",
        "l",
    )

    if TYPE_CHECKING:
        v: int
        p: bool
        rc: int
        pt: RTCPMessageType
        l: int

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int):
        self.v = version_flag >> 6
        self.p = bool((version_flag >> 5) & 0b1)
        self.rc = version_flag & 0b11111
        self.pt = rtcp_type
        self.l = length


class RTCPSenderReportPacket(RTCPPacket):
    """RTCP Sender Report packet which provides quality feedback

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/19.htm

    Extends :class:`RTCPPacket` and inherits its attributes.

    Attributes
    ----------
    ssrc: :class:`int`
        The synchronization source identifier for the originator of this SR packet.
    nts: :class:`int`
        NTP timestamp. Indicates the wallclock time when this report was sent
        so that it may be used in combination with timestamps returned in
        reception reports from other receivers to measure round-trip
        propagation to those receivers.
    rts: :class:`int`
        RTP timestamp. Corresponds to the same time as the NTP timestamp (above),
        but in the same units and with the same random offset as the RTP
        timestamps in data packets.
    spc: :class:`int`
        The total number of RTP data packets transmitted by the sender since
        starting transmission up until the time this SR packet was generated.
        The count is reset if the sender changes its SSRC identifier.
    soc: :class:`int`
        The total number of payload octets (i.e., not including header or padding)
        transmitted in RTP data packets by the sender since starting transmission
        up until the time this SR packet was generated. The count is reset if
        the sender changes its SSRC identifier.
    report_blocks: Sequence[:class:`RTCPReceiverReportPacket`]
        Sequence of :class:`RTCPReceiverReportPacket` objects that tell statistics.
        Receivers do not carry over statistics when a source changes its SSRC
        identifier due to a collision.
    extension: :class:`bytes`
        Profile-specific extension that may or may not contain a value.
    """

    __slots__ = (
        "ssrc",
        "nts",
        "rts",
        "spc",
        "soc",
        "report_blocks",
        "extension",
    )

    if TYPE_CHECKING:
        ssrc: int
        nts: int
        rts: int
        spc: int
        soc: int
        report_blocks: List
        extension: bytes

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int, data: bytes):
        super().__init__(version_flag, rtcp_type, length)

        self.ssrc, self.nts, self.rts, self.spc, self.soc = struct.unpack_from("!IQ3I", buffer=data)
        self.report_blocks = []
        self.extension = data[24:]


class RTCPReceiverReportPacket(RTCPPacket):
    """RTCP Receiver Report packet which provides quality feedback.

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/20.htm

    Extends :class:`RTCPPacket` and inherits its attributes.

    Attributes
    ----------
    ssrc: :class:`int`
        The synchronization source identifier for the originator of this SR packet.
    report_blocks: Sequence[:class:`RTCPReceiverReportPacket`]
        Sequence of :class:`RTCPReceiverReportPacket` objects that tell statistics.
        Receivers do not carry over statistics when a source changes its SSRC
        identifier due to a collision.
    extension: :class:`bytes`
        Profile-specific extension that may or may not contain a value.
    """

    __slots__ = (
        "ssrc",
        "report_blocks",
        "extension",
    )

    if TYPE_CHECKING:
        ssrc: int
        report_blocks: List
        extension: bytes

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int, data: bytes):
        super().__init__(version_flag, rtcp_type, length)

        self.ssrc = struct.unpack_from("!I", buffer=data)[0]
        self.report_blocks = []
        self.extension = data[4:]


class RTCPSourceDescriptionPacket(RTCPPacket):
    """Source Description packet which describes sources.

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/23.htm

    Extends :class:`RTCPPacket` and inherits its attributes.

    Attributes
    ----------
    chunks: Sequence[:class:`RTCPSourceDescriptionChunk`]
        Sequence of chunks that contain items.
    """

    __slots__ = ("chunks",)

    if TYPE_CHECKING:
        chunks: List[RTCPSourceDescriptionChunk]

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int, data: bytes):
        super().__init__(version_flag, rtcp_type, length)

        self.chunks = []
        for _ in range(self.rc):
            chunk, offset = self._parse_chunk(data)
            data = data[offset:]
            self.chunks.append(chunk)

    def _parse_chunk(self, data: bytes) -> Tuple[RTCPSourceDescriptionChunk, int]:
        ssrc = struct.unpack("!I", data)[0]
        items = []
        i = 4
        while True:
            cname = struct.unpack_from("!B", buffer=data, offset=i)[0]
            i += 1
            if cname == 0:
                break

            length = struct.unpack_from("!B", buffer=data, offset=i)[0]
            i += 1
            description = struct.unpack_from(f"!{length}s", buffer=data, offset=i)[0]
            i += length

            items.append(RTCPSourceDescriptionItem(cname, description))

        # Chunks are padded by 32-bit boundaries
        if i % 4 != 0:
            i += 4 - (i % 4)
        return RTCPSourceDescriptionChunk(ssrc, items), i


class RTCPGoodbyePacket(RTCPPacket):
    """A Goodbye packet indicating a number of SSRCs that are disconnected
    and possibly providing a reason for the disconnect

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/32.htm

    Extends :class:`RTCPPacket` and inherits its attributes.

    Attributes
    ----------
    ssrc_byes: Tuple[:class:`int`]
        List of SSRCs that are disconnecting. Not guaranteed to contain any values.
    reason: :class:`bytes`
        Reason for disconnect.
    """

    __slots__ = (
        "ssrc_byes",
        "reason",
    )

    if TYPE_CHECKING:
        ssrc_byes: Union[Tuple[int], Tuple]
        reason: bytes

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int, data: bytes):
        super().__init__(version_flag, rtcp_type, length)

        buf_size = self.rc * 4
        self.ssrc_byes = struct.unpack_from(f"!{self.rc}I", buffer=data)
        reason_length = struct.unpack_from("!B", buffer=data, offset=buf_size)[0]
        self.reason = (
            b"" if reason_length == 0 else struct.unpack_from(f"!{reason_length}s", buffer=data, offset=buf_size + 1)[0]
        )


class RTCPApplicationDefinedPacket(RTCPPacket):
    """An application-defined packet  intended for experimental use.

    Read in detail here: https://www.freesoft.org/CIE/RFC/1889/33.htm

    Extends :class:`RTCPPacket` and inherits its attributes.

    Attributes
    ----------
    rc: :class:`int`
        rc in this packet represents a subtype
    ssrc: :class:`int`
        The synchronization source identifier for the originator of this SR packet.
    name: :class:`str`
        A name chosen by the person defining the set of APP packets to be unique
        with respect to other APP packets this application might receive.
    app_data: :class:`bytes`
        Application-dependent data may or may not appear in an APP packet.
    """

    __slots__ = (
        "ssrc",
        "name",
        "app_data",
    )

    if TYPE_CHECKING:
        ssrc: int
        name: str
        app_data: bytes

    def __init__(self, version_flag: int, rtcp_type: RTCPMessageType, length: int, data: bytes):
        super().__init__(version_flag, rtcp_type, length)

        self.ssrc, name = struct.unpack_from("!I4s", buffer=data)
        self.name = name.decode("ascii")
        self.app_data = data[8:]


class RawAudioData:
    """Takes in a raw audio frame from discord and extracts its characteristics.

    Attributes
    ----------
    version: :class:`int`
        RTP version
    extended :class:`bool`
        Whether a header extension is present.
    marker: :class:`int`
        The interpretation of the marker is defined by a profile.
    payload_type: :class:`int`
        Type of payload, audio in this case
    sequence: :class:`int`
        The sequence number increments by one for each RTP data packet sent.
    timestamp: :class:`int`
        The timestamp reflects the sampling instant of the first octet in the audio data
    ssrc: :class:`int`
        Identifies the synchronization source.
    csrc_list: Sequence[:class:`int`]
        The CSRC list identifies the contributing sources for the payload
        contained in this packet.
    """

    __slots__ = (
        "version",
        "extended",
        "marker",
        "payload_type",
        "sequence",
        "timestamp",
        "ssrc",
        "csrc_list",
        "audio",
    )

    if TYPE_CHECKING:
        sequence: int
        timestamp: int
        ssrc: int
        version: int
        extended: bool
        marker: bool
        payload_type: int
        csrc_list: Tuple
        audio: bytes

    def __init__(self, data: bytes, decrypt_method: Callable[[bytes, bytes], bytes]):
        version_flag, payload_flag, self.sequence, self.timestamp, self.ssrc = struct.unpack_from(">BBHII", buffer=data)
        i = 12
        self.version = version_flag >> 6
        padding = (version_flag >> 5) & 0b1
        self.extended = bool((version_flag >> 4) & 0b1)
        self.marker = bool(payload_flag >> 7)
        self.payload_type = payload_flag & 0b1111111
        csrc_count = version_flag & 0b1111
        self.csrc_list = struct.unpack_from(f">{csrc_count}I", buffer=data, offset=i)
        i += csrc_count * 4

        # Extension parsing would go here, but discord's packets seem to have some problems
        # related to that, so no attempt will be made to parse extensions.

        if padding and data[-1] != 0:
            data = data[: -data[-1]]

        self.audio = decrypt_method(data[:i], data[i:])


class AudioFrame:
    """Represents audio that has been fully decoded.

    Attributes
    ----------
    sequence: :class:`int`
        The sequence of this frame in accordance with other frames
        that precede or follow it
    timestamp: :class:`int`
        Timestamp of the audio in accordance with its frame size
    ssrc: :class:`int`
        The source of the audio
    audio: :class:`bytes`
        Raw audio data
    user: Optional[Union[:class:`Member`, :class:`int`]]
        If the ssrc can be resolved to a user then this attribute
        contains the Member object for that user.
    """

    __slots__ = (
        "sequence",
        "timestamp",
        "ssrc",
        "audio",
        "user",
    )

    def __init__(self, frame: bytes, raw_audio: RawAudioData, user: Optional[Union['Member', 'Object']]):
        self.sequence: int = raw_audio.sequence
        self.timestamp: int = raw_audio.timestamp
        self.ssrc: int = raw_audio.ssrc
        self.audio: bytes = frame
        self.user: Optional[Union[Member, Object]] = user


_PACKET_TYPE = Union[
    RTCPSenderReportPacket,
    RTCPReceiverReportPacket,
    RTCPSourceDescriptionPacket,
    RTCPGoodbyePacket,
    RTCPApplicationDefinedPacket,
    RawAudioData,
]
_RTCP_MAP = {
    RTCPMessageType.sender_report: RTCPSenderReportPacket,
    RTCPMessageType.receiver_report: RTCPReceiverReportPacket,
    RTCPMessageType.source_description: RTCPSourceDescriptionPacket,
    RTCPMessageType.goodbye: RTCPGoodbyePacket,
    RTCPMessageType.application_defined: RTCPApplicationDefinedPacket,
}


def get_audio_packet(data: bytes, decrypt_method: Callable[[bytes, bytes], bytes]) -> _PACKET_TYPE:
    version_flag, payload_type, length = struct.unpack_from(">BBH", buffer=data)
    if 200 <= payload_type <= 204:
        rtcp_type = RTCPMessageType(payload_type)
        return _RTCP_MAP[rtcp_type](version_flag, rtcp_type, length, data[4:])
    return RawAudioData(data, decrypt_method)


class AudioSink:
    """An object that handles fully decoded and decrypted audio frames

    This class defines three major functions that an audio sink object must outline
    """

    def on_audio(self, frame: AudioFrame) -> Any:
        """This function receives :class:`AudioFrame` objects.

        Abstract method

        IMPORTANT: This method must not run stalling code for a substantial amount of time.

        Parameters
        ----------
        frame: :class:`AudioFrame`
            A frame of audio received from discord
        """
        raise NotImplementedError()

    def on_rtcp(self, packet: RTCPPacket) -> Any:
        """This function receives :class:`RTCPPacket` objects.

        Abstract method

        IMPORTANT: This method must not run stalling code for a substantial amount of time.

        Parameters
        ----------
        packet: Union[:class:`RTCPSenderReportPacket`, :class:`RTCPReceiverReportPacket`,
        :class:`RTCPSourceDescriptionPacket`, :class:`RTCPGoodbyePacket`, :class:`RTCPApplicationDefinedPacket`]
            A RTCP Packet received from discord.
        """
        raise NotImplementedError()

    def cleanup(self) -> Any:
        """This function is called when the bot is done receiving
        audio and before the after callback is called.

        Abstract method
        """
        raise NotImplementedError()


class AudioHandlingSink(AudioSink):
    """An abstract class extending :class:`AudioSink` that implements functionality for
    dealing with out-of-order packets and delays.
    """

    __slots__ = (
        "_last_sequence",
        "_buffer",
        "_buffer_wait",
        "_frame_queue",
        "_is_validating",
        "_buffer_till",
        "_lock",
        "_done_validating",
    )
    # how long to wait for missing a packet
    PACKET_WAIT_TIME = 2
    # how long to wait for a new packet before closing the _validation_loop thread
    VALIDATION_LOOP_TIMEOUT = 3
    # how long to wait for _validation_loop thread to start in _start_validation_loop
    VALIDATION_LOOP_START_TIMEOUT = 1

    def __init__(self):
        self._last_sequence: Dict[int, int] = defaultdict(lambda: -65535)
        # _buffer is not shared across threads
        self._buffers: Dict[int, List[AudioFrame]] = defaultdict(list)
        self._frame_queue = queue.Queue()
        self._is_validating: threading.Event = threading.Event()
        self._buffer_till: Dict[int, Optional[float]] = defaultdict(lambda: None)
        self._lock: threading.Lock = threading.Lock()
        self._done_validating: threading.Event = threading.Event()

    def on_audio(self, frame: AudioFrame) -> None:
        """Puts frame in a queue and lets a processing loop thread deal with it."""
        # lock is also used by _empty_buffer
        self._lock.acquire()
        self._frame_queue.put_nowait(frame)
        self._start_validation_loop()
        self._lock.release()

    def _start_validation_loop(self) -> None:
        if not self._is_validating.is_set():
            threading.Thread(target=self._validation_loop).start()
            # prevent multiple threads spawning and make sure it spawns, otherwise giving a warning
            if not self._is_validating.wait(timeout=self.VALIDATION_LOOP_START_TIMEOUT):
                _log.warning("Timeout reached waiting for _validation_loop thread to start")

    def _validation_loop(self) -> None:
        self._is_validating.set()
        self._done_validating.clear()
        while True:
            try:
                frame = self._frame_queue.get(timeout=self.VALIDATION_LOOP_TIMEOUT)
            except queue.Empty:
                break

            self._validate_audio_frame(frame)
        self._is_validating.clear()
        # Only set _done_validating if no audio was put back in the queue
        # Otherwise the validation loop will be restarted by _empty_buffer
        if not self._empty_entire_buffer():
            self._done_validating.set()

    def _validate_audio_frame(self, frame: AudioFrame) -> None:
        # 1. If the last recorded sequence number is >= 65000 and this audio frame
        # sequence number is <= 1000, the sequence number has likely looped back
        # around, so we'll subtract 65536 from the last recorded sequence number
        # to prevent issues.
        # 2. If this audio frame sequence number is lower than the last recorded
        # audio sequence number, drop it. The last recorded audio sequence marks the
        # sequence number of the last validated audio frame, so this audio frame is already
        # too late.
        # 3. If this is the first audio frame being received, or it's the next audio
        # frame in the sequence, it's valid. Since it's valid, the last recorded
        # sequence number is updated, the audio frame is passed to on_valid_audio, and
        # the buffer is emptied.
        # 4. Otherwise, this audio frame is added to a buffer.

        last_sequence = self._last_sequence[frame.ssrc]
        if last_sequence >= 65000 and frame.sequence <= 1000:
            self._last_sequence[frame.ssrc] = last_sequence = last_sequence - 65536

        elif frame.sequence <= last_sequence:
            return

        if last_sequence == -65535 or frame.sequence == last_sequence + 1:
            self._last_sequence[frame.ssrc] = frame.sequence
            self.on_valid_audio(frame)
            self._empty_buffer(frame.ssrc)
        else:
            self._append_to_buffer(frame)

    def _append_to_buffer(self, frame) -> None:
        self._buffers[frame.ssrc].append(frame)
        buffer_till = self._buffer_till[frame.ssrc]
        if buffer_till is None:
            self._buffer_till[frame.ssrc] = monotonic() + self.PACKET_WAIT_TIME
        elif monotonic() >= buffer_till:
            self._buffer_till[frame.ssrc] = None
            self._empty_buffer(frame.ssrc)

    def _empty_entire_buffer(self) -> bool:
        # Returns true if any calls to _empty_buffer return true

        result = False
        for ssrc in self._buffers.keys():
            result = result or self._empty_buffer(ssrc)
        return result

    def _empty_buffer(self, ssrc) -> bool:
        # Returns true when frames have been put back in the queue

        buffer = self._buffers[ssrc]
        if len(buffer) == 0:
            return False

        # prevent on_audio from putting frames in queue before these frames
        # and no conflicts on starting validation loop
        self._lock.acquire()

        sorted_buffer = sorted(buffer, key=lambda f: f.sequence)
        self._last_sequence[ssrc] = sorted_buffer[0].sequence - 1
        for frame in sorted_buffer:
            self._frame_queue.put_nowait(frame)
        self._start_validation_loop()
        self._buffers[ssrc] = []

        self._lock.release()
        return True

    def on_valid_audio(self, frame: AudioFrame) -> Any:
        """When an audio packet is declared valid, it'll be passed to this function.

        Abstract method

        IMPORTANT: Stalling code will stall

        Parameters
        ----------
        frame: :class:`AudioFrame`
            A frame of audio received from discord that has been validated by
            :class:`AudioHandlingSink.on_audio`.
        """
        raise NotImplementedError()


class AudioFileSink(AudioHandlingSink):
    """This implements :class:`AudioHandlingSink` with functionality for saving
    the audio to file.

    Parameters
    ----------
    file_type: Callable[[str, int], :class:`AudioFile`]
        A callable (such as a class or function) that returns an :class:`AudioFile` type.
        Is used to create AudioFile objects. Its two arguments are the default audio file path and
        audio ssrc respectfully.
    output_dir: :class:`str`
        The directory to save files to.

    Attributes
    ----------
    file_type: Callable[[str, int], :class:`AudioFile`]
        The file_type passed as an argument.
    output_dir: :class:`str`
        The directory where files are being saved.
    output_files: Dict[int, :class:`AudioFile`]
        Dictionary that maps an ssrc to file object or file path. It's a file object unless
        convert_files has been called.
    done: :class:`bool`
        Indicates whether cleanup has been called.
    converted: :class:`bool`
        Whether convert_files has been called and finished
    """

    VALIDATION_WAIT_TIMEOUT = 1

    __slots__ = ("file_type", "output_dir", "output_files", "done", "_clean_lock", "_convert_lock", "converted")

    def __init__(self, file_type: Callable[[str, int], 'AudioFile'], output_dir: str = "."):
        super().__init__()
        if not os.path.isdir(output_dir):
            raise ValueError("Invalid output directory")
        self.file_type: Callable[[str, int], 'AudioFile'] = file_type
        self.output_dir: str = output_dir
        self.output_files: Dict[int, AudioFile] = {}
        self.done: bool = False
        self._clean_lock: threading.Lock = threading.Lock()
        self._convert_lock: threading.Lock = threading.Lock()
        self.converted: bool = False

    def on_valid_audio(self, frame: AudioFrame) -> None:
        """Takes an audio frame and passes it to a :class:`AudioFile` object. If
        the AudioFile object does not already exist then it is created.

        Parameters
        ----------
        frame: :class:`AudioFrame`
            The frame which will be added to the buffer.
        """

        self._clean_lock.acquire()
        if self.done:
            return self._clean_lock.release()

        if frame.ssrc not in self.output_files:
            self.output_files[frame.ssrc] = self.file_type(
                os.path.join(self.output_dir, f"audio-{frame.ssrc}.pcm"), frame.ssrc
            )

        self.output_files[frame.ssrc].on_audio(frame)
        self._clean_lock.release()

    def on_rtcp(self, packet: RTCPPacket) -> None:
        """This function receives RTCP Packets, but does nothing with them since
        there is no use for them in this sink.

        Parameters
        ----------
        packet: :class:`RTCPPacket`
            A RTCP Packet received from discord. Can be any of the following:
            :class:`RTCPSenderReportPacket`, :class:`RTCPReceiverReportPacket`,
            :class:`RTCPSourceDescriptionPacket`, :class:`RTCPGoodbyePacket`,
            :class:`RTCPApplicationDefinedPacket`
        """
        return

    def cleanup(self) -> None:
        """Waits a maximum of `VALIDATION_WAIT_TIMEOUT` for packet validation to finish and
        then calls `cleanup` on all :class:`AudioFile` objects.

        Sets `done` to True after calling all the cleanup functions.
        """

        self._done_validating.wait(self.VALIDATION_WAIT_TIMEOUT)
        self._clean_lock.acquire()
        if self.done:
            return self._clean_lock.release()

        for file in self.output_files.values():
            file.cleanup()
        self.done = True
        self._clean_lock.release()

    async def convert_files(self, *args, **kwargs) -> Optional[List[Future]]:
        """Calls cleanup if it hasn't already been called and
        then calls convert on all :class:`AudioFile` objects in output_files.

        Any arguments and keyword arguments specified will be passed to :class:`AudioFile`.convert.

        If the function is called while conversion is still in process, it will
        simply return without doing anything.

        Returns
        -------
        Optional[List[:class:`Future`]]
            List of futures from each :class:`AudioFile`.convert call
        """
        if not self._convert_lock.acquire(blocking=False):
            return
        if self.converted:
            return self._convert_lock.release()
        if not self.done:
            self.cleanup()

        result = await asyncio.gather(
            *(file.convert(self._create_name(file), *args, **kwargs) for file in self.output_files.values())
        )

        self.converted = True
        self._convert_lock.release()

        return result

    def _create_name(self, file: 'AudioFile') -> str:
        if file.user is None:
            return f"audio-{file.ssrc}"
        elif isinstance(file.user, Object):
            return f"audio-{file.user.id}-{file.ssrc}"
        else:
            return f"audio-{file.user.name}#{file.user.discriminator}-{file.ssrc}"


def get_new_path(path: str, ext: str, new_name: Optional[str] = None):
    ext = "." + ext
    directory, name = os.path.split(path)
    name = new_name + ext if new_name is not None else ".".join(name.split(".")[:-1]) + ext
    return os.path.join(directory, name)


async def convert_with_ffmpeg(path, new_path, **kwargs):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        '-f',
        's16le',
        '-ar',
        str(OpusDecoder.SAMPLING_RATE),
        '-ac',
        str(OpusDecoder.CHANNELS),
        '-y',
        '-i',
        path,
        new_path,
        **kwargs,
    )
    await process.wait()


class AudioFile:
    """Manages an audio file and its attributes.

    Parameters
    ----------
    path: :class:`str`
        Path to the audio file.
    ssrc: :class:`int`
        ssrc of the user this file belongs to

    Attributes
    ----------
    file: :term:`py:file object`
        File object of the audio file this object refers to.
    ssrc: :class:`int`
        ssrc of the user associated with this audio file
    done: :class:`bool`
        Indicates whether cleanup has been called and file is closed. Does not
        indicate that the convert has been called.
    converted: :class:`bool`
        Indicates whether convert has been called already.
    user: Optional[Union[:class:`Member`, :class:`Object`]]
        User of this audio file
    path: :class:`str`
        Path to the file object.
    """

    __slots__ = (
        "file",
        "ssrc",
        "done",
        "converted",
        "path",
        "_last_timestamp",
        "_last_sequence",
        "_packet_count",
        "user",
        "_clean_lock",
    )

    FRAME_BUFFER_LIMIT = 10

    def __init__(self, path: str, ssrc: int):
        self.file: BinaryIO = open(path, "wb")
        self.ssrc: int = ssrc
        self.done: bool = False
        self.converted: bool = False
        self.user: Optional[Union[Member, Object]] = None
        self.path: str = self.file.name
        self._clean_lock: threading.Lock = threading.Lock()

        self._last_timestamp: Optional[int] = None
        self._last_sequence: Optional[int] = None
        self._packet_count = 0

    def on_audio(self, frame: AudioFrame) -> None:
        """Takes an audio frame and adds it to a buffer. Once the buffer
        reaches a certain size, all audio frames in the buffer are
        written to file. The buffer allows leeway for packets that
        arrive out of order to be reorganized.

        Parameters
        ----------
        frame: :class:`AudioFrame`
            The frame which will be added to the buffer.
        """
        self._clean_lock.acquire()
        if self.done:
            return
        # specifically for a scenario mentioned in _write_frame
        if self._packet_count < 7:
            self._packet_count += 1
        self._write_frame(frame)
        self._clean_lock.release()

    def _write_frame(self, frame: AudioFrame) -> None:
        # When the bot joins a vc and starts listening and a user speaks for the first time,
        # the timestamp encompasses all that silence, including silence before the bot even
        # joined the vc. It goes in a pattern that the 6th packet has an 11 sequence skip, so
        # this last part of the if statement gets rid of that silence.
        if self._last_timestamp is not None and not (self._packet_count == 6 and frame.sequence - self._last_sequence == 11):  # type: ignore
            silence = frame.timestamp - self._last_timestamp - OpusDecoder.SAMPLES_PER_FRAME
            if silence > 0:
                self.file.write(b"\x00" * silence * OpusDecoder.SAMPLE_SIZE)
        if frame.audio != SILENT_FRAME:
            self.file.write(frame.audio)
        self._last_timestamp = frame.timestamp
        self._last_sequence = frame.sequence
        self._cache_user(frame.user)

    def _cache_user(self, user: Optional[Union['Member', 'Object']]) -> None:
        if user is None:
            return
        if self.user is None:
            self.user = user
        elif type(self.user) == int and isinstance(user, Object):
            self.user = user

    def cleanup(self) -> None:
        """Writes remaining frames in buffer to file and then closes it."""
        self._clean_lock.acquire()
        if self.done:
            return
        self.file.close()
        self.done = True
        self._clean_lock.release()

    async def convert(self, new_name: Optional[str] = None) -> None:
        """Converts the file to its formatted file type.

        This function is abstract. Any implementation of this function should
        call AudioFile._convert_cleanup with the path of the formatted file
        after it finishes. It will delete the raw audio file and update
        some attributes.

        Parameters
        ----------
        new_name: Optional[:class:`str`]
            A new name for the file excluding the extension.
        """
        raise NotImplementedError()

    def _convert_cleanup(self, new_path: str) -> None:
        os.remove(self.path)
        self.path = new_path
        # this can be ignored because this function is meant to be used by subclasses.
        # where file is an optional type
        self.file = None  # type: ignore
        self.converted = True


class WaveAudioFile(AudioFile):
    """Extends :class:`AudioFile` with a method for converting the raw audio file
    to a wave file.

    Attributes
    ----------
    file: Optional[:term:`py:file object`]
        Same as in :class:`AudioFile`, but this attribute becomes None after convert is called.
    """

    if TYPE_CHECKING:
        file: Optional[BinaryIO]

    async def convert(self, new_name: Optional[str] = None, **kwargs) -> None:
        """Uses asyncio.create_subprocess_exec to create an ffmpeg process that converts the file.

        Extends :class:`AudioFile`

        Parameters
        ----------
        new_name: Optional[:class:`str`]
            Name for the wave file excluding ".wav". Defaults to current name if None.
        """
        if self.converted:
            return

        new_path = get_new_path(self.path, "wav", new_name)
        await convert_with_ffmpeg(self.path, new_path, **kwargs)

        self._convert_cleanup(new_path)


class MP3AudioFile(AudioFile):
    """Extends :class:`AudioFile` with a method for converting the raw audio file
    to a mp3 file.

    Attributes
    ----------
    file: Optional[:term:`py:file object`]
        Same as in :class:`AudioFile`, but this attribute becomes None after convert is called.
    """

    if TYPE_CHECKING:
        file: Optional[BinaryIO]

    async def convert(self, new_name: Optional[str] = None, **kwargs) -> None:
        """Uses asyncio.create_subprocess_exec to create an ffmpeg process that converts the file.

        Extends :class:`AudioFile`

        Parameters
        ----------
        new_name: Optional[:class:`str`]
            Name for the wave file excluding ".mp3". Defaults to current name if None.
        """
        if self.converted:
            return

        new_path = get_new_path(self.path, "mp3", new_name)
        await convert_with_ffmpeg(self.path, new_path, **kwargs)

        self._convert_cleanup(new_path)

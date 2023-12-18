import multiprocessing
import queue
import struct
import threading
from concurrent.futures import Future
from typing import Dict, List, Optional, Tuple, Union

import nacl.secret

from .opus import Decoder
from .sink import SILENT_FRAME, AudioFrame, RawAudioData, RTCPPacket, get_audio_packet

__all__ = ("AudioProcessPool",)


_mp_ctx = multiprocessing.get_context("spawn")


class AudioProcessPool:
    """Process pool for processing audio packets received from voice channels.

    Parameters
    ----------
    max_processes: :class:`int`
        The audio processing pool will distribute audio processing across
        this number of processes.
    wait_timeout: Optional[:class:`int`]
        A process will automatically finish when it has not received any audio
        after this amount of time. Default is 3. None means it will never finish
        via timeout.

    Raises
    ------
    ValueError
        max_processes or wait_timeout must be greater than 0
    """

    # TODO: add cleanup functionality
    def __init__(self, max_processes: int, *, wait_timeout: Optional[float] = 3):
        if max_processes < 1:
            raise ValueError("max_processes must be greater than 0")
        if wait_timeout is None or wait_timeout < 1:
            raise ValueError("wait_timeout must be greater than 0")

        self.max_processes: int = max_processes
        self.wait_timeout: Optional[float] = wait_timeout
        self._processes: Dict[int, Tuple] = {}
        self._wait_queue: queue.Queue = queue.Queue()
        self._wait_loop_running: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()

    def submit(self, data: bytes, n_p: int, decode: bool, mode: str, secret_key: List[int]) -> Future:
        self._lock.acquire()

        if n_p >= self.max_processes:
            raise ValueError(f"n_p must be less than the maximum processes ({self.max_processes})")

        if n_p not in self._processes:
            self._spawn_process(n_p)

        future = Future()
        self._processes[n_p][0].send((data, decode, mode, secret_key))
        self._wait_queue.put((n_p, future))
        self._start_recv_loop()

        self._lock.release()
        return future

    def _spawn_process(self, n_p) -> None:
        conn1, conn2 = _mp_ctx.Pipe(duplex=True)
        process = AudioUnpacker(args=(conn2,))
        process.start()
        self._processes[n_p] = (conn1, process)

    def _start_recv_loop(self) -> None:
        if not self._wait_loop_running.is_set():
            threading.Thread(target=self._recv_loop).start()

    def _recv_loop(self) -> None:
        self._wait_loop_running.set()
        while True:
            try:
                n_p, future = self._wait_queue.get(timeout=self.wait_timeout)
            except queue.Empty:
                break
            try:
                ret = self._processes[n_p][0].recv()
            except EOFError:
                self._lock.acquire()
                self._processes.pop(n_p)
                self._lock.release()
                continue
            (future.set_exception if isinstance(ret, BaseException) else future.set_result)(ret)

        self._wait_loop_running.clear()


class AudioUnpacker(_mp_ctx.Process):
    def __init__(self, **kwargs):
        super().__init__(daemon=True, **kwargs)

        self.secret_key: Optional[List[int]] = None
        self.decoders: Dict[int, Decoder] = {}

    def run(self) -> None:
        pipe = self._args[0]  # type: ignore
        while True:
            try:
                data, decode, mode, secret_key = pipe.recv()
                if secret_key is not None:
                    self.secret_key = secret_key

                packet = self.unpack_audio_packet(data, mode, decode)
                if isinstance(packet, RTCPPacket):
                    # enum not picklable
                    packet.pt = packet.pt.value  # type: ignore

                pipe.send(packet)
            except BaseException as exc:
                pipe.send(exc)
                return

    def _decrypt_xsalsa20_poly1305(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce = bytearray(24)
        nonce[:12] = header

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    def _decrypt_xsalsa20_poly1305_suffix(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce_size = nacl.secret.SecretBox.NONCE_SIZE
        nonce = data[-nonce_size:]

        return self.strip_header_ext(box.decrypt(bytes(data[:-nonce_size]), nonce))

    def _decrypt_xsalsa20_poly1305_lite(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        data = data[:-4]

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    @staticmethod
    def strip_header_ext(data: bytes) -> bytes:
        if data[0] == 0xBE and data[1] == 0xDE and len(data) > 4:
            _, length = struct.unpack_from('>HH', data)
            offset = 4 + length * 4
            data = data[offset:]
        return data

    def unpack_audio_packet(self, data: bytes, mode: str, decode: bool) -> Union[RTCPPacket, AudioFrame]:
        packet = get_audio_packet(data, getattr(self, '_decrypt_' + mode))

        if not isinstance(packet, RawAudioData):  # is RTCP packet
            return packet

        if decode and packet.audio != SILENT_FRAME:
            if packet.ssrc not in self.decoders:
                self.decoders[packet.ssrc] = Decoder()
            return AudioFrame(self.decoders[packet.ssrc].decode(packet.audio), packet, None)  # type: ignore

        return AudioFrame(packet.audio, packet, None)

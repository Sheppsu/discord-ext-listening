This is a [discord.py](https://github.com/rapptz/discord.py/) extension with functionality for listening in voice 
channels. It uses Python's standard multiprocessing library for audio processing, striving for efficiency and a 
flexible design.

# How to use
`pip install git+https://github.com/Sheppsu/discord-ext-listening`

Check examples folder to see how to use.
I might get some actual documentation up eventually.
Read section below if you want a full explanation.

# How it works
### VoiceClient
The listening functionality is tied to `VoiceClient` just as the playing functionality is.
The `VoiceClient` can be instantiated like normally, but specifying to use this extension's `VoiceClient` class.
All the listening methods of `VoiceClient` call `AudioReceiver` methods.
- `VoiceClient.listen` -> `AudioReceiver.start_listening`
- `VoiceClient.stop_listening` -> `AudioReceiver.stop_listening`
- `VoiceClient.pause_listening` -> `AudioReceiver.pause`
- `VoiceClient.resume_listening` -> `AudioReceiver.resume`
- `VoiceClient.wait_for_listen_ready` -> `AudioReceiver.wait_for_standby` & `AudioReceiver.wait_for_clean`

### AudioReceiver
**Functionality:** `AudioReceiver` facilitates the flow of unprocessed audio frames to processing areas and exposes methods
for `VoiceClient` to interact with that flow. 

**Design:** `AudioReceiver` inherits from `threading.Thread` and will constantly receive audio while 
running and the voice client being connected. If there's not a listening session in progress or the 
listening session is paused, the audio data is simply discarded after being received. Otherwise,
the receiver calls `AudioProcessPool.submit`, adding a callback to the returned `Future`. The callback
handles the processed audio frame or any errors, sending audio frames to and `AudioSink` object.
Stopping the listening session calls `AudioSink.cleanup` and any callback function passed to `VoiceClient.listen`.

### AudioProcessPool
**Functionality:** Manages child processes and facilitates sending and receiving audio frames to those processes
for processing.

**Design:** The design is based off `ProcessPoolExecutor`, with a `submit` function for sending audio frames to
a child process. The maximum number of child processes and other options are specified upon creation, but it doesn't
spawn a process until it's needed. The processes are spawned with the multiprocessing library, and it uses a duplex
pipe to send and receive audio frames. When audio is sent across the pipe, a `Future` object is created, which is
returned by `submit`, and the future along with a process index are put on a queue that another thread will poll.
When this separate thread pulls an item off the queue, it knows to wait for an audio frame from that process
(specified by the process index) and set the result of the future.

### AudioUnpacker
**Functionality:** Processes audio frames, decrypting and optionally decoding them. This is the class which receives
audio frames from `AudioProcessPool`.

**Design:** Inheriting from `multiprocessing.Process`, this process receives, processes, and send back audio frames
for the entirety of its runtime. If its pipe breaks or closes or another error occurs, it terminates. In the case
of an error not related to its pipe, the error is returned.

### AudioSink
**Functionality:** Abstract class that outlines the functionality of any class which inherits from this one.

**Design:** Defines `on_audio`, `on_rtcp`, and `cleanup` methods. `on_audio` receives all processed audio frames. 
`on_rtcp` receives all RTCP packets. `cleanup` is called when a listening session ends.

### AudioHandlingSink
**Functionality:** Abstract class that implements functionality for dealing with out-of-order packets and delays.

**Design:** Audio is received from `on_audio` and after being validated, is passed to `on_valid_audio`
(an abstract method). When an audio frame is initially received, it's put on a queue where another thread will
handle validation. When this separate thread pulls the audio frame off the queue, it goes through a validation 
procedure:
1. If the last recorded sequence number is >= 65000 and this audio frame sequence number is <= 1000, the sequence number
has likely looped back around, so we'll subtract 65536 from the last recorded sequence number to prevent issues.
2. If this audio frame sequence number is lower than the last recorded audio sequence number, drop it. The last
recorded audio sequence marks the sequence number of the last validated audio frame, so this audio frame is already
too late.
3. If this is the first audio frame being received, or it's the next audio frame in the sequence, it's valid. 
Since it's valid, the last recorded sequence number is updated, the audio frame is passed to on_valid_audio, and
the buffer is emptied (more on that below).
4. Otherwise, this audio frame is added to a buffer.

Emptying the buffer: The buffer is emptied when a valid frame is found, a specific amount of time has passed, or
when the thread handling validation is finishing. When the buffer is emptied, all the audio frames are sorted in
ascending order by their sequence number and put back in the queue. The last recorded sequence is then set to the first
audio frame's sequence minus one. Whenever the buffer is emptied, if the validation loop is finishing, a new one is
created.

### AudioFileSink
**Functionality:** Inherits from `AudioHandlingSink`, implementing functionality for managing multiple SSRC's and
associated audio files.

**Design:** Each SSRC (user in the vc) has its own associated `AudioFile` object for handling audio frame writing.
When `on_valid_audio` is called, it passes the audio to its respective `AudioFile.on_audio` handler, creating a new one
if needed. `AudioFileSink.cleanup` calls `AudioFile.cleanup` on all the managed `AudioFile` objects.

### AudioFile
**Functionality:** Abstract class for managing an audio file of a specific format.

**Design:** `on_audio` writes audio frames to as raw pcm to a file. `cleanup` closes the files handles and marks the
`AudioFile` as `done`. The class has an abstract method `convert` meant to convert the pcm file to a specific format.
`convert` is meant to call `_convert_cleanup`, which will fix some attributes of `AudioFile` after the convert.

### convert_with_ffmpeg
Function for using ffmpeg to convert a pcm file to another format. Uses `asyncio.create_subprocess_exec` with the
arguments `ffmpeg -f s16le -ar {sampling_rate} -ac {channels} -y -i {path} {new_path}`. It then asynchronously waits
for the process to finish.

### WaveAudioFile
**Functionality:** Converts pcm file to a wave file.

**Design:** Calls `convert_with_ffmpeg` with the new path ending in `.wav`.

### MP3AudioFile
**Functionality:** Converts pcm file to an mp3 file.

**Design:** Calls `convert_with_ffmpeg` with the new path ending in `.mp3`.
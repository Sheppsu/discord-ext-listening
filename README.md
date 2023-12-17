NOTE: I've had the code for this ext for a while but forgot to actually put it on github. Right now I'm just putting it up without giving it more rigorous testing (I can't atm), but if it works then you can install via `pip install git+https://github.com/Sheppsu/discord-ext-listening` and check the examples folder.
I'll soon give it proper testing and then put it up on pypi

This is a [discord.py](https://github.com/rapptz/discord.py/) extension with functionality for listening in voice channels. It uses Python's standard multiprocessing library for audio processing, striving for efficiency and a flexible design.

How to use
----------
`pip install git+https://github.com/Sheppsu/discord-ext-listening`

Check examples folder to see how to use.
I might get some actual documentation up eventually.
Read section below if you want a full explanation.

How it works
------------
**User interaction**
There are 6 user-facing methods: 
 - `listen`: tells an already initialized `AudioReceiver` (threading.Thread subclass) object to start listening. The object is initialized and started when the socket connection is first formed. The `AudioReceiver` object upon starting listening is passed some arguments the user specifies, most notably an `AudioSink` object. This sink object will be responsible for handling processed audio.
 - `init_audio_processing_pool`: calls `ConnectionState.init_audio_processing_pool`. The listening functionality of this PR uses sub processes to handle audio processing and this function allows the user to decide certain settings. 
 - `stop_listening`: tells `_receiver` to stop listening (it will still receive audio packets, but just discard them).
 - `pause_listening`: tells the `_receiver` to not process any audio received until listening has been resumed. The audio data is simply thrown away right after being received.
 - `resume_listening`: tells the `_receiver` to begin processing received audio again.
 - `wait_for_listen_ready`: a function that waits until `listen` can be safely executed. It's important when `listen` is being called multiple times to ensure there is not conflict between separate listening sessions.

The user gains control over audio frames with sink objects. More on sink objects below.

**Audio Receiver**
The `AudioReceiver` thread is continuously receiving audio data using `VoiceClient.recv_audio`. If currently we want the audio, then it will be processed, but otherwise it's discarded. The raw audio frames are sent to `ConnectionState.process_audio`, which will handle distributing audio frames across sub processes. The function returns a future and a callback is added which will send processed audio frames to `AudioReceiver._audio_processing_callback`. The processed audio frames received at the callback are sent to the audio sink object. Finally, when listening is stopped, `AudioReceiver` will execute the callback specified by the user, similar to `AudioPlayer`.

**Connection State**
I wasn't entirely sure where the process pool object should lie, but I decided on the `ConnectionState` at least for now. The connection state is only responsible for initializing `AudioProcessPool` and submitting raw audio frames to it for processing. The pool is initialized with a maximum number of process workers and when audio is submitted, it's submitted to a specific process by index using the guild id modulus the maximum number of workers. This method of spreading the audio across different processes will ensure a fairly even distribution. 

**Audio Processing Pool**
The design of `AudioProcessPool` is based upon `ProcessPoolExecutor`, which I could not use due to its limitations. When audio is submitted to the pool, it first spawns the specified process if it hasn't already been spawned. The spawning function creates a duplex pipe and an `AudioUnpacker` object (multiprocessing.Process subclass), starts the process, and saves it to a dictionary. The dictionary is only accessed under a lock since it's used by two different threads (the current one and another one I will explain soon). Now that the process is running, the audio data can be sent to it. Finally, the process number and a future object are put into a `Queue` together and it checks that a separate receiving thread is running, starting a new one otherwise. The receiving thread will continuously fetch items from the queue, which tell it which processes will be sending back processed audio and where to resend it (the future object).

**Audio Unpacker**
`AudioUnpacker` is a subprocess that receives audio frames through a pipe, processes them, and sends them back. Audio frames are decrypted, decoded, and finally sent back as a `AudioFrame` object or one of the RTCP objects.

**Audio Sinks**
`AudioSink` is an abstract class that defines the base methods of a sink: `on_audio`, `on_rtcp`, `cleanup`. `on_audio` will receive processed audio frames, `on_rtcp` will receive RTCP packets, and `cleanup` is called when listening stops. This class can be subclassed by the user to carry out specific tasks with audio frames, however, there are also higher level sink objects: `AudioHandlingSink` and `AudioFileSink`. `AudioHandlingSink` implements functionality that takes audio frames and validates them by making sure they are received in order, account for delays and out-of-order packets. `AudioFileSink` subclasses `AudioHandlingSink`, receiving valid audio packets and then writing them to file using `AudioFile` objects. I'll go into depth on all of these objects in separate sections.

**Audio Handling Sink**
When an audio frame is received, it's immediately put into a queue and a separate validation thread will fetch it. The steps of the validation process are as follows:
1. If the audio frame is silent, empty the buffer (a list containing audio frames). Emptying the buffer involves sorting them by frame sequence, setting `_last_sequence`, and putting each audio frame into the queue that the validation thread pulls from. `_last_sequence` is a variable keeping track of the last validated audio frame. The emptying process is performed within a lock to make sure all the frames go in the queue before any frames from `on_audio`.
2. If the sequence of the frame is less than that of the last validated frame then it's dropped. 
3. 
   a. If the sequence of the frame matches up with the sequence of the last validated frame, then the frame is valid. `_last_sequence` is updated, the frame is passed to `on_valid_audio` (an abstract method in this class), and the buffer is emptied.
   b. If the sequence of the frame is not valid, then it is added to the buffer. If a certain amount of time has passed since the first audio frame was placed into the buffer, than it's forcefully emptied. This forceful empty of the buffer signifies that it will no longer wait for the lost frame. It's possible that it still arrives shortly after, but it would be dropped in step 2.

**Audio File Sink**
`AudioFileSink` creates an `AudioFile` type object for each user's audio data and manages them.  When `on_valid_audio` is called, the frame's ssrc is linked to an already existing `AudioFile` object or a new one is created. The audio frame is then passed to that object to handle. `AudioFileSink` implements the cleanup method which calls cleanup on all `AudioFile` objects. Finally, once cleanup is done, the user can call `convert_files`, which will call `convert` on all `AudioFile` objects.

**Audio File**
`AudioFile` receives audio frames and writes them to a pcm file. It saves the timestamp of the last received frame to calculate how much silence should be inserted in between some frames. When `cleanup` is called the file is closed and its set as being done. The `convert` method is abstract and should create a new file of a specific type using the raw audio data. There are two already made classes that implement `AudioFile`: `WaveAudioFile` and `MP3AudioFile`. 
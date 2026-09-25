"""Streaming mono PCM16 conversion for the Atom Echo microphone."""
import struct


class MicrophoneResampler:
    """16 kHz -> 24 kHz linear interpolation, independent of packet boundaries.

    Retains one sample across chunks. Wake-word PCM remains at 16 kHz.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = []
        self.position = 0
        self.partial = b""

    def convert(self, pcm):
        pcm = self.partial + pcm
        size = len(pcm) & ~1
        self.partial = pcm[size:]
        if size:
            self.samples.extend(struct.unpack("<%dh" % (size // 2), pcm[:size]))
        output = []
        while self.position // 3 + 1 < len(self.samples):
            i, fraction = divmod(self.position, 3)
            output.append((self.samples[i] * (3 - fraction) + self.samples[i + 1] * fraction) // 3)
            self.position += 2
        consumed = self.position // 3
        self.samples = self.samples[consumed:]
        self.position -= consumed * 3
        return struct.pack("<%dh" % len(output), *output)

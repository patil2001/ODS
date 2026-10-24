// Runs in an AudioContext created at 16 kHz, so the browser has already
// resampled the microphone. Packs 20 ms of audio (320 samples) into
// little-endian 16-bit PCM and hands it to the page.
const SAMPLES_PER_PACKET = 320;

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(SAMPLES_PER_PACKET);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this.filled === SAMPLES_PER_PACKET) {
        this.port.postMessage(this.buffer.buffer.slice(0));
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-capture", MicCapture);

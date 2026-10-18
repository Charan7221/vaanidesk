// AudioWorklet: captures mic audio, downsamples to 16 kHz PCM16 and posts 40 ms frames.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.frame = new Int16Array(640); // 40 ms @ 16 kHz
    this.idx = 0;
    this.pos = 0; // fractional read position into the incoming stream
    this.level = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let sum = 0;
    // Linear-interpolation downsampling
    while (this.pos < ch.length - 1) {
      const i = Math.floor(this.pos);
      const f = this.pos - i;
      const s = ch[i] * (1 - f) + ch[i + 1] * f;
      sum += s * s;
      const v = Math.max(-1, Math.min(1, s));
      this.frame[this.idx++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.idx === this.frame.length) {
        this.port.postMessage({ pcm: this.frame.buffer.slice(0), level: this.level });
        this.idx = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    this.level = Math.sqrt(sum / Math.max(1, ch.length / this.ratio));
    return true;
  }
}
registerProcessor("mic-processor", MicProcessor);

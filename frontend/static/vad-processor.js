/**
 * vad-processor.js  —  AudioWorkletProcessor
 *
 * Two responsibilities:
 *   1. Convert raw microphone Float32 samples → Int16 PCM frames
 *      and post them to the main thread for the Azure STT push-stream.
 *
 *   2. Emit lightweight RMS energy readings every ENERGY_REPORT_FRAMES
 *      so the main thread can detect silence without relying on STT
 *      events.  Used by the silence-watch system to:
 *        • Reset silence timers when the candidate is making ANY sound
 *          (speech, thinking aloud, "um", breathing into mic).
 *        • Distinguish true silence from mid-answer thinking pauses.
 *
 * Frame size: 20 ms @ 48 kHz = 960 samples per post.
 * Energy report: every 5 frames = every ~100 ms.
 */

const SAMPLE_RATE          = 48000;
const FRAME_MS             = 20;
const FRAME_SAMPLES        = (SAMPLE_RATE * FRAME_MS) / 1000;  // 960
const ENERGY_REPORT_FRAMES = 5;   // report RMS every 5 × 20 ms = 100 ms

class PcmPassthroughProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf        = new Float32Array(FRAME_SAMPLES);
    this._pos        = 0;
    this._frameCount = 0;
    this._rmsAccum   = 0;   // accumulate squared samples between reports
    this._rmsCount   = 0;   // number of samples accumulated
  }

  process(inputs) {
    const ch = (inputs[0] || [])[0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) {
      const sample = ch[i];
      this._buf[this._pos++] = sample;

      /* Accumulate for RMS */
      this._rmsAccum += sample * sample;
      this._rmsCount++;

      if (this._pos >= FRAME_SAMPLES) {
        /* ── 1. PCM frame → main thread (for Azure STT) ─────────────── */
        const pcm = new Int16Array(FRAME_SAMPLES);
        for (let j = 0; j < FRAME_SAMPLES; j++) {
          pcm[j] = Math.max(-32768, Math.min(32767,
            Math.round(this._buf[j] * 32767)
          ));
        }
        this.port.postMessage({ type: 'pcm', buffer: pcm }, [pcm.buffer]);
        this._pos = 0;

        /* ── 2. RMS energy report every N frames ─────────────────────── */
        this._frameCount++;
        if (this._frameCount >= ENERGY_REPORT_FRAMES) {
          const rms = this._rmsCount > 0
            ? Math.sqrt(this._rmsAccum / this._rmsCount)
            : 0;
          this.port.postMessage({ type: 'energy', rms });
          this._frameCount = 0;
          this._rmsAccum   = 0;
          this._rmsCount   = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('vad-processor', PcmPassthroughProcessor);

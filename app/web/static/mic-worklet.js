// Posts mono Float32 frames from the microphone to the main thread.
class BmoMicProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(new Float32Array(ch));
    return true;
  }
}
registerProcessor("bmo-mic", BmoMicProcessor);

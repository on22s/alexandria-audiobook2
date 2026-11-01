
'use strict';
globalThis.AudioMeasurementInstrument = (() => {
  const version = 'pcm-window-lab/1.0.1';
  const limits = {bytes: 20 * 1024 * 1024, durationSeconds: 60, channels: 2};
  const nearFullScaleAmplitude = 10 ** (-0.1 / 20);
  const db = value => value === 0 ? null : 20 * Math.log10(value);
  function parseWav(bytes) {
    if (!(bytes instanceof ArrayBuffer) || bytes.byteLength < 44 || bytes.byteLength > limits.bytes) { throw Error('Use a complete PCM WAV of at most 20 MiB.'); }
    const view = new DataView(bytes);
    const word = offset => String.fromCharCode(...new Uint8Array(bytes, offset, 4));
    if (word(0) !== 'RIFF' || word(8) !== 'WAVE' || view.getUint32(4, true) + 8 !== bytes.byteLength) { throw Error('Expected complete RIFF/WAVE; RF64 and trailing bytes are unsupported.'); }
    let format, data, offset = 12;
    while (offset + 8 <= bytes.byteLength) {
      const kind = word(offset), size = view.getUint32(offset + 4, true), start = offset + 8, end = start + size;
      if (end > bytes.byteLength) { throw Error('WAV chunk exceeds file bounds.'); }
      if (kind === 'fmt ') {
        if (format || size < 16) { throw Error('Duplicate or short WAV format chunk.'); }
        format = {encoding: view.getUint16(start, true), channels: view.getUint16(start + 2, true), sampleRate: view.getUint32(start + 4, true), byteRate: view.getUint32(start + 8, true), blockAlign: view.getUint16(start + 12, true), bits: view.getUint16(start + 14, true)};
      }
      if (kind === 'data') { if (data) { throw Error('Multiple data chunks are unsupported.'); } data = {start, size}; }
      offset = end + size % 2;
    }
    if (offset !== bytes.byteLength || !format || !data) { throw Error('Incomplete WAV chunk structure.'); }
    const {encoding, channels, sampleRate, byteRate, blockAlign, bits} = format;
    if (!((encoding === 1 && [16, 24, 32].includes(bits)) || (encoding === 3 && bits === 32))) { throw Error('Use PCM16/24/32 or Float32 WAV; extensible WAV is not supported.'); }
    if (channels < 1 || channels > 2 || sampleRate < 8000 || sampleRate > 96000 || blockAlign !== channels * bits / 8 || byteRate !== sampleRate * blockAlign || data.size % blockAlign) { throw Error('Invalid source clock/alignment or unsupported channel count.'); }
    const frames = data.size / blockAlign;
    if (!frames || frames / sampleRate > limits.durationSeconds) { throw Error('Choose a nonempty excerpt of at most 60 seconds.'); }
    const samples = Array.from({length: channels}, () => new Float32Array(frames));
    for (let frame = 0; frame < frames; frame++) {
      for (let channel = 0; channel < channels; channel++) {
        const position = data.start + frame * blockAlign + channel * bits / 8;
        let value;
        if (encoding === 3) { value = view.getFloat32(position, true); }
        else if (bits === 16) { value = view.getInt16(position, true) / 32768; }
        else if (bits === 32) { value = view.getInt32(position, true) / 2147483648; }
        else { let integer = view.getUint8(position) | view.getUint8(position + 1) << 8 | view.getUint8(position + 2) << 16; if (integer & 0x800000) { integer -= 0x1000000; } value = integer / 8388608; }
        if (!Number.isFinite(value)) { throw Error('Non-finite audio sample; refusing analysis.'); }
        samples[channel][frame] = value;
      }
    }
    return {samples, sampleRate, frames, channels, format, durationSeconds: frames / sampleRate};
  }
  function regionStats(signal, startFrame = 0, endFrame = signal.frames) {
    if (!Number.isInteger(startFrame) || !Number.isInteger(endFrame) || startFrame < 0 || endFrame > signal.frames || endFrame <= startFrame) { throw Error('Region must be a nonempty, in-bounds source-frame interval.'); }
    const channels = signal.samples.map((samples, index) => {
      let sum = 0, peak = 0, nearFullScaleSamples = 0, overFullScaleSamples = 0;
      for (let frame = startFrame; frame < endFrame; frame++) { const value = samples[frame], absolute = Math.abs(value); sum += value * value; peak = Math.max(peak, absolute); if (absolute >= nearFullScaleAmplitude) { nearFullScaleSamples++; } if (absolute > 1) { overFullScaleSamples++; } }
      const rms = Math.sqrt(sum / (endFrame - startFrame));
      return {channel: index + 1, rmsAmplitude: rms, rmsDbfs: db(rms), peakAmplitude: peak, peakDbfs: db(peak), allSamplesZero: peak === 0, nearFullScaleSamples, nearFullScalePercent: nearFullScaleSamples / (endFrame - startFrame) * 100, overFullScaleSamples};
    });
    return {startFrame, endFrameExclusive: endFrame, frames: endFrame - startFrame, startSeconds: startFrame / signal.sampleRate, endSeconds: endFrame / signal.sampleRate, durationSeconds: (endFrame - startFrame) / signal.sampleRate, channels};
  }
  function buildBlocks(signal) {
    const blockFrames = Math.round(signal.sampleRate * 0.010), count = Math.floor(signal.frames / blockFrames), maxChannelPowers = new Float64Array(count);
    for (let block = 0; block < count; block++) {
      for (const samples of signal.samples) { let sum = 0; for (let frame = block * blockFrames; frame < (block + 1) * blockFrames; frame++) { sum += samples[frame] * samples[frame]; } maxChannelPowers[block] = Math.max(maxChannelPowers[block], sum / blockFrames); }
    }
    return {blockFrames, blockSeconds: blockFrames / signal.sampleRate, maxChannelPowers, analyzedFrames: count * blockFrames, droppedTailFrames: signal.frames - count * blockFrames};
  }
  function findQuiet(signal, blocks, thresholdDbfs, minimumSeconds) {
    if (!Number.isFinite(thresholdDbfs) || thresholdDbfs < -100 || thresholdDbfs > 0 || !Number.isFinite(minimumSeconds) || minimumSeconds < 0.01 || minimumSeconds > 60) { throw Error('Threshold: -100 to 0 dBFS. Minimum duration: 0.01 to 60 seconds.'); }
    const thresholdPower = 10 ** (thresholdDbfs / 10), intervals = [];
    let run = null;
    const finish = end => { if (run !== null) { const startFrame = run * blocks.blockFrames, endFrameExclusive = end * blocks.blockFrames, durationSeconds = (endFrameExclusive - startFrame) / signal.sampleRate; if (durationSeconds >= minimumSeconds) { intervals.push({startFrame, endFrameExclusive, startSeconds: startFrame / signal.sampleRate, endSeconds: endFrameExclusive / signal.sampleRate, durationSeconds, blocks: end - run}); } } run = null; };
    for (let block = 0; block < blocks.maxChannelPowers.length; block++) { if (blocks.maxChannelPowers[block] <= thresholdPower) { if (run === null) { run = block; } } else { finish(block); } }
    finish(blocks.maxChannelPowers.length);
    return {thresholdDbfs, minimumSeconds, thresholdPower, count: intervals.length, totalSeconds: intervals.reduce((sum, entry) => sum + entry.durationSeconds, 0), intervals};
  }
  function calibration() {
    const sampleRate = 48000, results = [];
    const constant = value => new Float32Array(sampleRate).fill(value);
    const sine = amplitude => Float32Array.from({length: sampleRate}, (_, frame) => amplitude * Math.sin(2 * Math.PI * 1000 * frame / sampleRate));
    const signal = samples => ({sampleRate, frames: samples[0].length, channels: samples.length, samples});
    const check = (name, observed, expected, tolerance, passed) => results.push({name, observed, expected, tolerance, passed});
    const zero = signal([constant(0)]);
    check('Digital zero: null RMS and one-second quiet interval', regionStats(zero).channels[0].rmsDbfs, null, 0, regionStats(zero).channels[0].rmsDbfs === null && findQuiet(zero, buildBlocks(zero), -50, 0.5).totalSeconds === 1);
    const half = regionStats(signal([constant(0.5)])).channels[0].rmsDbfs;
    check('DC amplitude 0.5 RMS', half, -6.020599913279624, 0.01, Math.abs(half + 6.020599913279624) < 0.01);
    for (const amplitude of [1, 0.5]) { const measured = regionStats(signal([sine(amplitude)])).channels[0], expected = 20 * Math.log10(amplitude / Math.sqrt(2));check('Sine ' + amplitude + ' RMS', measured.rmsDbfs, expected, 0.01, Math.abs(measured.rmsDbfs - expected) < 0.01);check('Sine ' + amplitude + ' peak', measured.peakDbfs, 20 * Math.log10(amplitude), 0.01, Math.abs(measured.peakDbfs - 20 * Math.log10(amplitude)) < 0.01); }
    const positive = sine(1), anti = signal([positive, positive.map(value => -value)]), antiQuiet = findQuiet(anti, buildBlocks(anti), -50, 0.5).count;
    check('Anti-phase stereo is not quiet', antiQuiet, 0, 0, antiQuiet === 0);
    const gap = constant(0.5);gap.fill(0, 12000, 36000);const gapSignal = signal([gap]), gaps = findQuiet(gapSignal, buildBlocks(gapSignal), -50, 0.1).intervals;
    check('Known digital gap: source frames', gaps.map(entry => [entry.startFrame, entry.endFrameExclusive]), [[12000, 36000]], 0, gaps.length === 1 && gaps[0].startFrame === 12000 && gaps[0].endFrameExclusive === 36000);
    for (const delta of [-0.5, 0.5]) { const value = signal([constant(10 ** ((-50 + delta) / 20))]), count = findQuiet(value, buildBlocks(value), -50, 0.5).count;check('Threshold direction ' + delta + ' dB', count, delta < 0 ? 1 : 0, 0, count === (delta < 0 ? 1 : 0)); }
    const boundary = findQuiet({sampleRate}, {blockFrames:480,maxChannelPowers:new Float64Array([1])}, 0, 0.01).count;
    check('Exact power equality uses <=', boundary, 1, 0, boundary === 1);
    const count = regionStats(signal([Float32Array.from([1,-1,0.99,-0.99,0.98,-0.98,0])])).channels[0].nearFullScaleSamples;
    check('Near-full-scale samples at -0.1 dBFS', count, 4, 0, count === 4);
    const tail = buildBlocks(signal([new Float32Array(481)])).droppedTailFrames;
    check('Partial block dropped, not padded', tail, 1, 0, tail === 1);
    const decoderFixtures = [
      {name:'PCM16 signed extrema',hex:'524946462e00000057415645666d74201000000001000100401f0000803e000002001000646174610a0000000080ffff00000100ff7f',channels:1,frames:5,samples:[[-1,-0.000030517578125,0,0.000030517578125,0.999969482421875]]},
      {name:'PCM24 signed extrema and odd padding',hex:'524946463400000057415645666d74201000000001000100401f0000c05d000003001800646174610f000000000080ffffff000000010000ffff7f00',channels:1,frames:5,samples:[[-1,-1.1920928955078125e-7,0,1.1920928955078125e-7,0.9999998807907104]]},
      {name:'PCM32 Float32 representation rounding',hex:'524946463800000057415645666d74201000000001000100401f0000007d000004002000646174611400000000000080ffffffff0000000001000000ffffff7f',channels:1,frames:5,samples:[[-1,-4.656612873077393e-10,0,4.656612873077393e-10,1]]},
      {name:'Float32 signed values beyond full scale',hex:'524946463800000057415645666d74201000000003000100401f0000007d00000400200064617461140000000000a0bf000000bf000000000000003f0000a03f',channels:1,frames:5,samples:[[-1.25,-0.5,0,0.5,1.25]]},
      {name:'Stereo PCM16 channel order',hex:'524946462c00000057415645666d74201000000001000200401f0000007d0000040010006461746108000000004000c0002000e0',channels:2,frames:2,samples:[[0.5,0.25],[-0.5,-0.25]]}
    ];
    for (const fixture of decoderFixtures) {
      const expected = {sampleRate:8000,channels:fixture.channels,frames:fixture.frames,samples:fixture.samples};
      try {
        const bytes = Uint8Array.from(fixture.hex.match(/../g), pair => parseInt(pair, 16));
        const decoded = parseWav(bytes.buffer);
        const observed = {sampleRate:decoded.sampleRate,channels:decoded.channels,frames:decoded.frames,samples:decoded.samples.map(channel => Array.from(channel))};
        check('RIFF decoder: ' + fixture.name, observed, expected, 0, JSON.stringify(observed) === JSON.stringify(expected));
        if (decoded.format.encoding === 3) {
          const beyond = regionStats(decoded).channels[0].overFullScaleSamples;
          check('Float32 over-full-scale values are countable, not a clipping diagnosis', beyond, 2, 0, beyond === 2);
        }
      } catch (error) {
        check('RIFF decoder: ' + fixture.name, {error:String(error)}, expected, 0, false);
      }
    }
    return {version, passed: results.every(entry => entry.passed), count:results.length, results};
  }
  return {version,limits,nearFullScaleAmplitude,db,parseWav,regionStats,buildBlocks,findQuiet,calibration};
})();

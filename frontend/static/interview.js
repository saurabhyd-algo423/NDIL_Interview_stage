/* interview.js — Frontend interview controller: manages avatar WebRTC connection, mic capture/VAD, Azure Speech STT/TTS, SSE streaming of AI replies, interrupt handling, and phase/timer UI updates for the candidate interview page. */
/* ════════════════════════════════════════════════════════════════════
   FLASK CONFIG  (injected by Jinja2 at render time — keep in index.html)
   ════════════════════════════════════════════════════════════════════
   NOTE: CFG is declared in index.html via an inline <script> block
   because it contains Jinja2 template variables ({{ ... }}).
   This file assumes CFG is already defined on window before it loads.
   ════════════════════════════════════════════════════════════════════ */

const PHASES = [
  "GREETING", "ROLE_CONFIRMATION", "EXPERIENCE_DEEP_DIVE", "PROJECTS_DISCUSSION",
  "SKILLS_COVERAGE", "JD_ALIGNMENT", "BEHAVIORAL", "CANDIDATE_QUESTIONS", "CLOSING"
];

/* ════════════════════════════════════════════════════════════════════
   STATE
   ════════════════════════════════════════════════════════════════════ */
let STATE = 'idle';
let sessionId = null;
let clientId = null;
let avatarSynth = null;
let speechCfg = null;
let recognizer = null;
let audioCtx = null;
let pushStream = null;
let micStream = null;   // mic MediaStream  (audio only)
let camStream = null;   // camera MediaStream (video only)
let camOn = false;  // is candidate camera on?
let micOn = false;  // is microphone pre-enabled in setup?
let setupMicStream = null; // mic stream acquired during setup (for indicator only)
let avatarPC = null;   // RTCPeerConnection — stored so recorder can tap avatar tracks

let currentWordIdx = 0;
let wordTicker = null;
let wasInterrupt = false;
let lastSubmittedText = '';
let submitLock = false;
let timerIv = null;
let pollIv = null;
let startEpoch = null;
let currentSubtitleSentences = [];

/* Background save timers and state */
let _bgSaveIv = null;
let _sessionTs = null;
let _finishCalled = false;
let _emergencySaveLock = false;   /* prevents double-fire between visibilitychange and pagehide */

/*
 * _avatarSpeechDone — set by speakText(), resolved by TurnEnd.
 * Allows handleUserInput to truly await the avatar finishing speech
 * before calling finishInterview(), so the farewell message always
 * completes fully before any cleanup/save runs.
 */
let _avatarSpeechResolve = null;

/* ── Streaming SSE state (sentence-by-sentence TTS) ── */
let _speechQueue = [];          // subtitle texts queued for display
let _speechPending = 0;         // sentences still being spoken by avatar
let _streamComplete = false;    // SSE stream has ended
let _interviewEndedFlag = false; // server says interview is over
let _isStreamingMode = false;   // true when using SSE streaming path

/* ── AI Interrupt — candidate analysis state ── */
let _candidateTurnFinal = '';       // accumulated finalized STT phrases this turn
let _candidateTurnPartial = '';     // current partial phrase from recognizing event
let _aiInterruptWordCount = 0;      // word count at last analysis call
let _aiInterruptInFlight = false;   // analysis HTTP request in progress
let _aiInterruptTriggered = false;  // interrupt decision made — don't re-check
const _AI_INTERRUPT_FIRST = 60;    // trigger first analysis after this many words
const _AI_INTERRUPT_STRIDE = 30;    // re-check every this many additional words

/* ── Silence Watch — adaptive natural conversation ── */
let _lastEnergyMs = 0;      // timestamp of last detected mic energy
let _hadEnergyThisTurn = false;  // did candidate produce ANY audio this turn?
let _accumulatedSpeech = '';     // all recognized speech this turn (survives VAD rejects)
let _silenceWatchIv = null;   // setInterval handle for silence poller
let _silenceCumulMs = 0;      // consecutive ms of confirmed silence (no energy)
let _silenceTier = 0;      // 0=none fired, 1=rephrase sent, 2=move_on sent
let _postSpeechDone = false;  // post-speech-pause action already taken
let _nudgeInFlight = false;  // nudge SSE stream in progress — don't double-fire

const _ENERGY_THRESHOLD = 0.008; // RMS below this → silence (tuned for mic + compressor chain)
const _ENERGY_LATCH_MS = 700;   // ms without energy before "silence" is confirmed
const _POST_SPEECH_MS = 3500;  // ms of silence after speech → post-speech action
const _SILENCE_TIER1_MS = 4000;  // ms pure silence → tier 1: rephrase nudge
const _SILENCE_TIER2_MS = 10000; // ms cumulative silence → tier 2: move on
const _SW_POLL_MS = 300;   // silence watch polling interval

/* ── Pre-cached speech token (fetched on page load) ── */
let _prefetchedToken = null;
fetch('/api/getSpeechToken')
  .then(r => r.json())
  .then(t => { if (!t.error) { _prefetchedToken = t; console.log('[Prefetch] Speech token cached, expiresAt:', new Date(t.expiresAt).toISOString()); } })
  .catch(() => { });

/* ── Token refresh — Azure tokens expire in 10 min; we refresh every 9 min ──
   _tokenRefreshTimer is started once the interview begins and cleared ONLY
   when the interview fully ends (STATE === 'done' or _finishCalled).
   The timer keeps firing every 9 min for the full 30-min interview duration
   (up to ~3–4 refresh cycles), so the STT recognizer never sees an expired token.
   On each tick we hot-swap speechCfg.authorizationToken (the Azure SDK
   supports this without recreating the recognizer).                            */
let _tokenRefreshTimer  = null;
let _sttCancelCount     = 0;        // consecutive cancel events — triggers rebuild
const _TOKEN_REFRESH_MS = 9 * 60 * 1000;  // 9 minutes — matches backend cache TTL

const SILENCE = new Int16Array(960).buffer;  /* 20 ms @ 48 kHz mono 16-bit */

/* ════════════════════════════════════════════════════════════════════
   COMPOSITED RECORDING
   ════════════════════════════════════════════════════════════════════
   We want ONE recording file containing:
     VIDEO : avatar (left half) + candidate camera (right half) side-by-side
     AUDIO : mic (candidate voice) + avatar speaker audio (interviewer voice)

   Architecture:
     ┌─────────────────────────────────────────────────┐
     │  Off-screen Canvas 1280×720                     │
     │  ┌──────────────────┬────────────────────────┐  │
     │  │  Avatar video    │  Candidate camera      │  │
     │  │  (left 640×720) │  (right 640×720)       │  │
     │  └──────────────────┴────────────────────────┘  │
     └─────────────────────────────────────────────────┘
     Canvas stream → MediaRecorder

     File saved as:  interview_YYYY-MM-DD_HH-MM-SS.webm
   Records: candidate camera (video) + candidate mic (audio)
   ════════════════════════════════════════════════════════════════════ */
let mediaRecorder = null;
let recordedChunks = [];
let canvasDrawLoop = null;   // requestAnimationFrame ID

/* AudioContext used to mix candidate mic + avatar audio for recording */
let recAudioCtx = null;
let recDestination = null;

function _buildRecordingStream() {
  /*
   * VIDEO : candidate camera (mirrored, 1280x720 canvas @ 30 fps)
   * AUDIO : candidate mic  +  avatar voice  — both mixed together
   *
   * HOW AVATAR AUDIO IS CAPTURED
   * The avatar runs over WebRTC. pc.ontrack adds every incoming track
   * (video + audio) to avatarVideoEl.srcObject (a MediaStream).
   * We tap the audio tracks from that MediaStream using
   *   AudioContext.createMediaStreamSource(avatarStream)
   * and mix them with the candidate mic into one MediaStreamDestination.
   * createMediaElementSource() does NOT work on WebRTC <video> elements.
   */

  const canvas = document.getElementById('rec-canvas');
  const ctx = canvas.getContext('2d');
  const camEl = document.getElementById('cam-video');
  const avatarEl = document.getElementById('avatar-video');
  const W = canvas.width;   /* 1280 */
  const H = canvas.height;  /* 720  */

  /* 1. Canvas: candidate camera drawn at 30 fps */
  let _lastDraw = 0;
  const _targetMs = 1000 / 30;

  function drawFrame() {
    ctx.fillStyle = '#080a10';
    ctx.fillRect(0, 0, W, H);

    const camReady = camOn
      && camEl
      && camEl.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      && camEl.videoWidth > 0;

    if (camReady) {
      ctx.save();
      ctx.translate(W, 0);
      ctx.scale(-1, 1);
      ctx.drawImage(camEl, 0, 0, W, H);
      ctx.restore();
    } else {
      ctx.fillStyle = '#14172a';
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = '#3d4470';
      ctx.font = 'bold 18px Segoe UI, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Candidate Camera Off', W / 2, H / 2 - 10);
      ctx.font = '13px Segoe UI, sans-serif';
      ctx.fillText('Video paused', W / 2, H / 2 + 18);
    }

    const ts = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    ctx.fillStyle = 'rgba(232,234,246,.4)';
    ctx.font = '12px monospace';
    ctx.textAlign = 'right';
    ctx.fillText(ts, W - 12, H - 12);
    ctx.font = 'bold 11px Segoe UI, sans-serif';
    ctx.fillStyle = 'rgba(232,234,246,.5)';
    ctx.textAlign = 'left';
    ctx.fillText('CANDIDATE', 14, H - 12);
  }

  function rAFDraw(now) {
    if (now - _lastDraw >= _targetMs) { drawFrame(); _lastDraw = now; }
    canvasDrawLoop = requestAnimationFrame(rAFDraw);
  }
  drawFrame();
  canvasDrawLoop = requestAnimationFrame(rAFDraw);

  /* 2. Audio mixing: candidate mic + avatar voice */
  recAudioCtx = new AudioContext({ sampleRate: 48000 });
  recDestination = recAudioCtx.createMediaStreamDestination();

  /* A. Candidate microphone */
  if (micStream && micStream.getAudioTracks().length > 0) {
    try {
      const micSrc = recAudioCtx.createMediaStreamSource(micStream);
      const micGain = recAudioCtx.createGain();
      micGain.gain.value = 1.2;
      micSrc.connect(micGain);
      micGain.connect(recDestination);
      console.log('[Recorder] Candidate mic connected');
    } catch (e) {
      console.warn('[Recorder] Mic connect failed:', e.message);
    }
  } else {
    console.warn('[Recorder] No mic audio tracks');
  }

  /* B. Avatar audio — tap WebRTC audio tracks from avatarEl.srcObject.
     We route through AudioContext so we can mix, and also reconnect to
     recAudioCtx.destination so the user still hears the avatar normally. */
  function _connectAvatarAudio() {
    const avatarStream = avatarEl ? avatarEl.srcObject : null;
    if (!(avatarStream instanceof MediaStream)) return false;
    const audioTracks = avatarStream.getAudioTracks();
    if (audioTracks.length === 0) return false;
    try {
      const avatarAudioOnly = new MediaStream(audioTracks);
      const avatarSrc = recAudioCtx.createMediaStreamSource(avatarAudioOnly);
      const avatarGain = recAudioCtx.createGain();
      avatarGain.gain.value = 1.0;
      avatarSrc.connect(avatarGain);
      avatarGain.connect(recDestination);          /* into recording */
      avatarGain.connect(recAudioCtx.destination); /* to speakers — still audible */
      console.log('[Recorder] Avatar audio connected (' + audioTracks.length + ' track/s)');
      return true;
    } catch (e) {
      console.warn('[Recorder] Avatar audio connect failed:', e.message);
      return false;
    }
  }

  /* Try immediately; avatar tracks may already be present */
  if (!_connectAvatarAudio()) {
    /* Tracks sometimes arrive a few seconds after video — retry every 500 ms */
    let _attempts = 0;
    const _retryId = setInterval(() => {
      _attempts++;
      if (_connectAvatarAudio() || _attempts >= 16) {
        clearInterval(_retryId);
        if (_attempts >= 16)
          console.warn('[Recorder] Avatar audio never arrived — mic-only recording');
      }
    }, 500);
  }

  /* 3. Combine canvas video + mixed audio */
  const combined = new MediaStream([
    ...canvas.captureStream(30).getVideoTracks(),
    ...recDestination.stream.getAudioTracks(),
  ]);

  console.log('[Recorder] Stream ready — video:', combined.getVideoTracks().length,
    'audio:', combined.getAudioTracks().length);

  return combined;
}
function startRecording() {
  try {
    const stream = _buildRecordingStream();

    /* ── Choose best codec ───────────────────────────────────────────
       Priority: vp9 (better quality) → vp8 (wider support) → default.
       We explicitly set BOTH video AND audio bitrates.
       audioBitsPerSecond:128000  → 128 kbps Opus — clear speech + avatar audio
       videoBitsPerSecond:5000000 → 5 Mbps VP8/VP9 — eliminates pixel breakup    */
    const codecPrefs = [
      { mimeType: 'video/webm;codecs=vp9,opus', vbr: 5_000_000, abr: 128_000 },
      { mimeType: 'video/webm;codecs=vp8,opus', vbr: 5_000_000, abr: 128_000 },
      { mimeType: 'video/webm', vbr: 4_000_000, abr: 128_000 },
    ];
    const chosen = codecPrefs.find(p => MediaRecorder.isTypeSupported(p.mimeType))
      || { mimeType: '', vbr: 4_000_000, abr: 128_000 };

    mediaRecorder = new MediaRecorder(stream, {
      ...(chosen.mimeType ? { mimeType: chosen.mimeType } : {}),
      videoBitsPerSecond: chosen.vbr,
      audioBitsPerSecond: chosen.abr,
    });
    recordedChunks = [];
    // _chunkPointer  = 0;

    /* Fix session timestamp once — used in the final recording blob name */
    const _n = new Date();
    _sessionTs =
      `${_n.getFullYear()}${String(_n.getMonth() + 1).padStart(2, '0')}${String(_n.getDate()).padStart(2, '0')}` +
      `_${String(_n.getHours()).padStart(2, '00')}${String(_n.getMinutes()).padStart(2, '00')}${String(_n.getSeconds()).padStart(2, '0')}`;

    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) recordedChunks.push(e.data); };

    /* ── Chunk interval ──────────────────────────────────────────────
       100 ms chunks: small enough to avoid gap artefacts at seams,
       large enough not to overwhelm the ondataavailable handler.
       The old 1000 ms (1 s) chunks caused audible clicks/glitches
       at every chunk boundary.                                        */
    mediaRecorder.start(100);

    $('rec-badge').style.display = 'flex';
    console.log('[Recorder] Started —', chosen.mimeType || 'browser default',
      `| video: ${chosen.vbr / 1000}kbps | audio: ${chosen.abr / 1000}kbps`);

    /* Start periodic background saves now that recording is live */
    _startBackgroundSaves();
  } catch (err) {
    console.warn('[Recorder] Could not start:', err);
  }
}

/* ════════════════════════════════════════════════════════════════════
   BACKGROUND SAVES
   ════════════════════════════════════════════════════════════════════
   One setInterval loop every 30 s once recording starts:

   _bgSaveIv — POST /api/saveTranscription (save_reason:"background")
               Server overwrites the ONE fixed JSON blob for this session.
               Candidate folder always has exactly one up-to-date transcript.

   Recording is NOT uploaded in background chunks.
   All recorded data accumulates in recordedChunks[] in memory.
   The FULL recording is uploaded as ONE complete file when the interview
   ends (finishInterview) or the tab is closed (_emergencySave).

   On any sudden exit (power cut / screen-off / tab close / crash):
     _emergencySave() fires via beforeunload + pagehide + visibilitychange.
     sendBeacon for transcription (guaranteed on unload).
     keepalive fetch for the full recording (browser queues it after page dies).

   Normal finishInterview() does a guaranteed final upload.
   ════════════════════════════════════════════════════════════════════ */

function _startBackgroundSaves() {
  /* Transcription checkpoint every 30 s — overwrites the same fixed JSON blob */
  _bgSaveIv = setInterval(() => {
    if (!sessionId || STATE === 'idle' || STATE === 'done') return;
    fetch('/api/saveTranscription', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, save_reason: 'background' })
    })
      .then(r => r.json())
      .then(d => console.log('[BgSave] Transcription ✓ turns:', d.turns_saved))
      .catch(e => console.warn('[BgSave] Transcription failed:', e));
  }, 30000);

  /* Recording is NOT uploaded in background chunks.
     All chunks accumulate in recordedChunks[] in memory.
     The full recording is uploaded as ONE complete file on interview end
     (finishInterview) or tab close (_emergencySave). */

  console.log('[BgSave] Background transcription saves started (30 s interval)');
}

function _stopBackgroundSaves() {
  clearInterval(_bgSaveIv); _bgSaveIv = null;
}

/* ════════════════════════════════════════════════════════════════════
   EMERGENCY SAVE — screen-off / tab close / power cut / network drop
   ════════════════════════════════════════════════════════════════════ */
/* ════════════════════════════════════════════════════════════════════
   EMERGENCY SAVE — tab close / screen-off / power cut / network drop
   ════════════════════════════════════════════════════════════════════

   Two separate strategies depending on how the page is leaving:

   A) visibilitychange → hidden  (tab switch, screen lock, app switch)
      Page is still alive — we have time for real async fetch uploads.
      Stop the recorder to flush final frames, then upload both the
      full recording AND the transcription properly.

   B) beforeunload / pagehide  (hard tab close, browser quit, refresh)
      Page is dying — only sendBeacon is reliable here.
      sendBeacon has a ~64 KB limit so it can only carry the transcription
      JSON (which is small). Recording cannot be sent via beacon for a
      full interview — this is a hard browser constraint we cannot bypass.
      We still attempt a keepalive fetch for the recording as a best-effort.

   ════════════════════════════════════════════════════════════════════ */


/* ── Strategy A: async full save when tab goes hidden ────────────────── */
async function _emergencySaveAsync() {
  if (!sessionId || STATE === 'idle' || STATE === 'done') return;
  if (_emergencySaveLock) return;
  _emergencySaveLock = true;
  console.log('[Emergency] Tab hidden — async full save starting');

  /* Step 1: Stop MediaRecorder to flush the final segment into recordedChunks */
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    await new Promise(resolve => {
      mediaRecorder.onstop = resolve;
      try { mediaRecorder.stop(); } catch (_) { resolve(); }
    });
    /* Give ondataavailable a tick to fire for the final chunk */
    await new Promise(r => setTimeout(r, 150));
  }

  /* Step 2: Save transcription with save_reason='final' so the server
     deletes the old folder, recreates it, and sets state['final_folder_ts'].
     The recording upload in Step 3 then uses that same timestamp. */
  try {
    const res = await fetch('/api/saveTranscription', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, save_reason: 'final' })
    });
    const d = await res.json();
    console.log('[Emergency] Transcription saved ✓ turns:', d.turns_saved, '| folder:', d.folder);
  } catch (e) {
    console.warn('[Emergency] Transcription save failed:', e);
  }

  /* Step 3: Upload full recording — all chunks from interview start */
  if (recordedChunks.length) {
    const mime = (mediaRecorder && mediaRecorder.mimeType) || 'video/webm';
    const fullBlob = new Blob(recordedChunks, { type: mime });
    console.log(`[Emergency] Uploading full recording: ${(fullBlob.size / 1024 / 1024).toFixed(2)} MB`);
    const fd = new FormData();
    fd.append('session_id', sessionId);
    fd.append('recording', fullBlob, `recording_${_sessionTs || 'unk'}.webm`);
    try {
      await fetch('/api/uploadRecording', { method: 'POST', body: fd });
      console.log('[Emergency] Recording uploaded ✓');
    } catch (e) {
      console.warn('[Emergency] Recording upload failed:', e);
    }
  } else {
    console.warn('[Emergency] No recording chunks to upload');
  }

  console.log('[Emergency] Async save complete');
}

/* ── Strategy B: best-effort sync save on hard close ─────────────────── */
function _emergencySaveSync() {
  if (!sessionId || STATE === 'idle' || _finishCalled) return;
  console.log('[Emergency] Hard close — sync beacon save');

  /* Transcription via sendBeacon — small JSON, guaranteed on unload.
     Use save_reason='final' so the server deletes the old folder and
     sets final_folder_ts, keeping the folder clean and consistent. */
  navigator.sendBeacon('/api/saveTranscription',
    new Blob(
      [JSON.stringify({ session_id: sessionId, save_reason: 'final' })],
      { type: 'application/json' }
    )
  );
  console.log('[Emergency] Transcription beacon sent');

  /* Recording via keepalive fetch — best effort on hard close.
     Works reliably for interviews up to ~500 MB depending on browser.
     visibilitychange (Strategy A) handles the async upload when the tab
     is merely hidden; this covers the hard-close / refresh path. */
  if (recordedChunks.length) {
    const mime = (mediaRecorder && mediaRecorder.mimeType) || 'video/webm';
    const fullBlob = new Blob(recordedChunks, { type: mime });
    const fd = new FormData();
    fd.append('session_id', sessionId);
    fd.append('recording', fullBlob, `recording_${_sessionTs || 'unk'}.webm`);
    fetch('/api/uploadRecording', { method: 'POST', body: fd, keepalive: true })
      .catch(() => { });
    console.log('[Emergency] Recording keepalive-fetch sent:', (fullBlob.size / 1024 / 1024).toFixed(2), 'MB');
  }
}

/* Register on every possible exit path */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && !_finishCalled) {
    _emergencySaveAsync().catch(() => { });
  }
});
window.addEventListener('beforeunload', _emergencySaveSync);
window.addEventListener('pagehide', _emergencySaveSync);

/* ════════════════════════════════════════════════════════════════════
   STOP & FINAL UPLOAD  (normal end-interview path)
   ════════════════════════════════════════════════════════════════════ */
function stopAndUploadRecording() {
  _stopBackgroundSaves();
  if (canvasDrawLoop) { cancelAnimationFrame(canvasDrawLoop); canvasDrawLoop = null; }
  $('rec-badge').style.display = 'none';

  /* Helper: build the full recording blob from ALL chunks and upload it */
  async function _uploadFullRecording() {
    if (!sessionId || !recordedChunks.length) {
      console.warn('[Recorder] No recording data to upload');
      return;
    }
    const mime = (mediaRecorder && mediaRecorder.mimeType) || 'video/webm';
    const fullBlob = new Blob(recordedChunks, { type: mime });
    console.log(`[Recorder] Full recording: ${(fullBlob.size / 1024 / 1024).toFixed(2)} MB — uploading…`);

    const fd = new FormData();
    fd.append('session_id', sessionId);
    fd.append('recording', fullBlob, `recording_${_sessionTs || 'final'}.webm`);
    try {
      const res = await fetch('/api/uploadRecording', { method: 'POST', body: fd });
      const data = await res.json();
      if (res.ok) {
        console.log(`[Recorder] Full recording uploaded ✓ → ${data.blob?.blob_url}`);
      } else {
        console.error('[Recorder] Full recording upload failed:', data.error || data);
      }
    } catch (err) {
      console.error('[Recorder] Full recording upload error:', err);
    }
  }

  if (!mediaRecorder || mediaRecorder.state === 'inactive') {
    /* Recorder already stopped — still upload whatever was collected */
    console.warn('[Recorder] MediaRecorder already inactive — uploading collected chunks');
    const p = _uploadFullRecording();
    if (recAudioCtx) { recAudioCtx.close().catch(() => { }); recAudioCtx = null; recDestination = null; }
    return p;
  }

  return new Promise(resolve => {
    mediaRecorder.onstop = async () => {
      await _uploadFullRecording();
      if (recAudioCtx) { recAudioCtx.close().catch(() => { }); recAudioCtx = null; recDestination = null; }
      resolve();
    };
    mediaRecorder.stop();
  });
}

/* ════════════════════════════════════════════════════════════════════
   SETUP SCREEN CAMERA  (starts before interview)
   ════════════════════════════════════════════════════════════════════ */
async function toggleSetupCamera() {
  if (!camOn) {
    try {
      camStream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
        audio: false
      });
      /* Show in setup screen preview */
      const setupVid = document.getElementById('setup-cam-video');
      setupVid.srcObject = camStream;
      setupVid.style.display = 'block';
      $('setup-cam-placeholder').style.display = 'none';
      $('setup-cam-status').style.display = 'flex';
      $('setup-cam-btn').classList.add('active');
      $('setup-cam-btn-txt').textContent = 'Disable Camera';
      camOn = true;
      console.log('[Camera] ON (setup screen)');
    } catch (err) {
      alert('Cannot access camera:\n' + err.message + '\nPlease allow camera access in your browser.');
      console.warn('[Camera] Error:', err);
    }
  } else {
    _shutCamera();
  }
}

function _shutCamera() {
  if (camStream) { camStream.getTracks().forEach(t => t.stop()); camStream = null; }
  /* Setup screen */
  const setupVid = document.getElementById('setup-cam-video');
  setupVid.srcObject = null;
  setupVid.style.display = 'none';
  $('setup-cam-placeholder').style.display = 'flex';
  $('setup-cam-status').style.display = 'none';
  $('setup-cam-btn').classList.remove('active');
  $('setup-cam-btn-txt').textContent = 'Enable Camera';
  /* Interview PIP */
  $('cam-video').srcObject = null;
  $('candidate-pip').style.display = 'none';
  $('mic-pill').classList.remove('pip-active');

  camOn = false;
  console.log('[Camera] OFF');
}



function _activatePIP() {
  /* Feed the interview PIP */
  $('cam-video').srcObject = camStream;
  $('candidate-pip').style.display = 'block';
  $('mic-pill').classList.add('pip-active');

}

/* ════════════════════════════════════════════════════════════════════
   SETUP SCREEN MICROPHONE  (test mic before interview starts)
   ════════════════════════════════════════════════════════════════════ */
async function toggleSetupMic() {
  if (!micOn) {
    try {
      setupMicStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      $('setup-mic-btn').classList.add('active');
      $('setup-mic-btn-txt').textContent = 'Disable Microphone';
      $('setup-mic-indicator').classList.add('active');
      micOn = true;
      console.log('[Mic] Pre-enabled in setup');
    } catch (err) {
      alert('Cannot access microphone:\n' + err.message + '\nPlease allow microphone access in your browser.');
      console.warn('[Mic] Error:', err);
    }
  } else {
    _shutSetupMic();
  }
}

function _shutSetupMic() {
  if (setupMicStream) { setupMicStream.getTracks().forEach(t => t.stop()); setupMicStream = null; }
  $('setup-mic-btn').classList.remove('active');
  $('setup-mic-btn-txt').textContent = 'Enable Microphone';
  $('setup-mic-indicator').classList.remove('active');
  micOn = false;
  console.log('[Mic] OFF (setup)');
}

/* ════════════════════════════════════════════════════════════════════
   UI HELPERS
   ════════════════════════════════════════════════════════════════════ */
const $ = id => document.getElementById(id);

function setStatus(txt, cls = '') {
  const el = $('status-badge'); el.textContent = txt; el.className = cls;
}
function showOverlay(msg) { $('overlay-msg').textContent = msg; $('overlay').style.display = 'flex'; }
function hideOverlay() { $('overlay').style.display = 'none'; }

function showMicPill(dotCls, partialText) {
  const pill = $('mic-pill'), dot = $('mic-dot'),
    txt = $('mic-status-txt'), ptxt = $('partial-txt');
  dot.className = dotCls || '';
  ptxt.textContent = partialText || '';
  if (dotCls === 'listening') {
    txt.textContent = 'Listening — speak anytime';
    pill.style.display = 'flex';
  } else if (dotCls === 'speaking') {
    txt.textContent = 'Avatar speaking — just talk to interrupt';
    pill.style.display = 'flex';
  } else if (dotCls === 'thinking') {
    txt.textContent = 'Processing…';
    ptxt.textContent = '';
    pill.style.display = 'flex';
  } else {
    pill.style.display = 'none';
  }
}

function tickTimer() {
  if (!startEpoch) return;
  const rem = Math.max(0, 30 * 60 - Math.floor((Date.now() - startEpoch) / 1000));
  $('timer').textContent =
    `${String(Math.floor(rem / 60)).padStart(2, '0')}:${String(rem % 60).padStart(2, '0')}`;
}

async function pollStatus() {
  if (!sessionId) return;
  try {
    const d = await fetch(`/api/sessionStatus?session_id=${sessionId}`).then(r => r.json());
    if (d.error) return;
    $('phase-name').textContent = d.phase.replace(/_/g, ' ');
    $('phase-fill').style.width = Math.min(100, ((d.phase_index + 1) / PHASES.length) * 100) + '%';
    if (!d.active && !_finishCalled) finishInterview();
  } catch (e) { console.warn('[poll]', e); }
}

/* ════════════════════════════════════════════════════════════════════
   AUDIO PIPELINE  (mic → Azure STT)
   ════════════════════════════════════════════════════════════════════ */
async function buildAudioPipeline() {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true, noiseSuppression: true,
      autoGainControl: true, channelCount: 1, sampleRate: 48000
    }
  });
  console.log('[Mic] Acquired');

  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
  if (audioCtx.state === 'suspended') await audioCtx.resume();

  const src = audioCtx.createMediaStreamSource(micStream);
  const hp = audioCtx.createBiquadFilter();
  hp.type = 'highpass'; hp.frequency.value = 80; hp.Q.value = 0.7;
  const comp = audioCtx.createDynamicsCompressor();
  comp.threshold.value = -24; comp.knee.value = 10;
  comp.ratio.value = 4; comp.attack.value = 0.003; comp.release.value = 0.1;

  await audioCtx.audioWorklet.addModule('/static/vad-processor.js');
  const worklet = new AudioWorkletNode(audioCtx, 'vad-processor');
  src.connect(hp); hp.connect(comp); comp.connect(worklet);

  pushStream = SpeechSDK.AudioInputStream.createPushStream(
    SpeechSDK.AudioStreamFormat.getWaveFormatPCM(48000, 16, 1)
  );
  worklet.port.onmessage = ({ data }) => {
    /* ── PCM: forward to Azure STT push-stream ── */
    if (data.type === 'pcm') {
      if (STATE === 'listening' || STATE === 'speaking') {
        pushStream.write(data.buffer.buffer);
      } else if (STATE === 'thinking') {
        pushStream.write(SILENCE);
      }
      return;
    }

    /* ── Energy: update silence-watch tracker ── */
    if (data.type === 'energy' && STATE === 'listening') {
      if (data.rms >= _ENERGY_THRESHOLD) {
        _lastEnergyMs = Date.now();
        _hadEnergyThisTurn = true;
      }
    }
  };
  console.log('[Audio] Pipeline ready');
}

/* ════════════════════════════════════════════════════════════════════
   AVATAR SETUP
   ════════════════════════════════════════════════════════════════════ */
async function setupAvatar() {
  for (let i = 0; i < 100; i++) {
    if (typeof SpeechSDK !== 'undefined') break;
    await new Promise(r => setTimeout(r, 100));
  }
  if (typeof SpeechSDK === 'undefined')
    throw new Error('Azure Speech SDK did not load. Disable ad-blocker and refresh.');

  showOverlay('Registering session…');
  clientId = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
  // Pass candidate-specific avatar config (set from Cosmos DB by startInterview)
  // so the server-side client record also reflects the correct character/style/voice.
  const cfgR = await fetch('/api/connectAvatar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_id: clientId,
      avatar_character: CFG.avatarCharacter,
      avatar_style: CFG.avatarStyle,
      tts_voice: CFG.ttsVoice,
    })
  });
  if (!cfgR.ok) throw new Error('connectAvatar failed: ' + cfgR.status);

  showOverlay('Fetching speech token…');
  const tok = _prefetchedToken || await fetch('/api/getSpeechToken').then(r => r.json());
  _prefetchedToken = null;
  if (tok.error) throw new Error(tok.error);

  showOverlay('Fetching ICE token…');
  const ice = await fetch('/api/getIceToken').then(r => r.json());
  if (ice.error) throw new Error(ice.error);

  speechCfg = SpeechSDK.SpeechConfig.fromAuthorizationToken(tok.token, CFG.speechRegion);
  speechCfg.speechSynthesisVoiceName = CFG.ttsVoice;
  speechCfg.speechRecognitionLanguage = 'en-US';  /* eliminates language auto-detection latency */

  showOverlay('Creating WebRTC connection…');
  const iceUrls = ice.Urls || ice.urls || [];
  const iceUser = ice.Username || ice.username || '';
  const icePass = ice.Password || ice.password || '';

  const pc = new RTCPeerConnection({
    iceServers: [{ urls: iceUrls, username: iceUser, credential: icePass }]
  });
  avatarPC = pc;  /* save reference so _buildRecordingStream() can tap avatar audio/video */
  pc.addTransceiver('video', { direction: 'sendrecv' });
  pc.addTransceiver('audio', { direction: 'sendrecv' });
  pc.oniceconnectionstatechange = () => console.log('[ICE]', pc.iceConnectionState);
  pc.onconnectionstatechange = () => console.log('[PC]', pc.connectionState);

  const videoEl = $('avatar-video');

  // Fallback: hide placeholder as soon as the <video> element actually starts playing.
  // This covers edge cases where ontrack fires but the kind-check timing is missed
  // (observed with Lisa and other Azure avatar characters that stream video slightly later).
  videoEl.addEventListener('playing', () => {
    $('avatar-placeholder').style.display = 'none';
    videoEl.style.display = 'block';
    console.log('[Avatar] Video playing (fallback trigger)');
  }, { once: true });

  pc.ontrack = evt => {
    if (!videoEl.srcObject) videoEl.srcObject = new MediaStream();
    videoEl.srcObject.addTrack(evt.track);
    if (evt.track.kind === 'video') {
      $('avatar-placeholder').style.display = 'none';
      videoEl.style.display = 'block';
      console.log('[Avatar] Video track live');
    }
  };

  // Azure TTS Avatar SDK requires character-specific style name formats.
  // Lisa's valid styles are "graceful-sitting" and "casual-sitting" — the "-sitting"
  // suffix is mandatory. Cosmos DB may store the shorthand ("graceful", "casual"),
  // so we normalise here before passing to the SDK. Other avatars are unaffected.
  function resolveAvatarStyle(character, style) {
    if (character.toLowerCase() === 'lisa') {
      return 'casual-sitting'; // Lisa only supports casual-sitting
    }
    return style;
  }
  const resolvedStyle = resolveAvatarStyle(CFG.avatarCharacter, CFG.avatarStyle);
  console.log('[Avatar] Resolved style:', CFG.avatarCharacter, '→', resolvedStyle);
  const avatarCfg = new SpeechSDK.AvatarConfig(CFG.avatarCharacter, resolvedStyle);
  avatarCfg.backgroundColor = '#e8eceeff'; /* solid white background */

  avatarSynth = new SpeechSDK.AvatarSynthesizer(speechCfg, avatarCfg);
  avatarSynth.avatarEventReceived = (_s, e) => {
    const ev = e.description;
    console.log('[AvatarEvent]', ev);
    if (ev === 'TurnStart') {
      STATE = 'speaking';
      showMicPill('speaking');
      setStatus('Avatar speaking…', 'active');
      /* Streaming mode: show subtitle from queue */
      if (_isStreamingMode && _speechQueue.length > 0) {
        const sent = _speechQueue[0];
        $('subtitle-container').innerHTML = `<div class="subtitle-line">${sent}</div>`;
        $('subtitle-container').dataset.activeText = sent;
      }
    }
    if (ev === 'TurnEnd') {
      stopWordTicker();
      currentWordIdx = 0;
      $('subtitle-container').innerHTML = '';
      $('subtitle-container').dataset.activeText = '';

      /* Streaming mode: pop finished sentence from queue */
      if (_isStreamingMode) {
        if (_speechQueue.length > 0) _speechQueue.shift();
        _speechPending = Math.max(0, _speechPending - 1);
      }

      /* Legacy speakText promise (used for greeting) */
      if (_avatarSpeechResolve) {
        const _res = _avatarSpeechResolve;
        _avatarSpeechResolve = null;
        _res();
        if (STATE === 'done' || _finishCalled) return;
      }

      if (STATE === 'speaking') {
        if (_isStreamingMode) {
          /* Wait until ALL sentences are done AND stream has ended */
          if (_streamComplete && _speechPending <= 0) {
            _isStreamingMode = false;
            if (_interviewEndedFlag && !_finishCalled) {
              finishInterview();
            } else {
              wasInterrupt = false;
              goListening(true);
            }
          }
          /* Otherwise more sentences are queued — avatar auto-plays next */
        } else {
          /* Legacy mode (single speakText) */
          wasInterrupt = false;
          goListening(true);
        }
      }
    }
  };

  showOverlay('Starting avatar… (up to 15 s)');
  try { await avatarSynth.startAvatarAsync(pc); }
  catch (err) { throw new Error('Avatar start failed: ' + err); }
  console.log('[Avatar] Connected and ready');
}

/* ════════════════════════════════════════════════════════════════════
   STT RECOGNIZER
   ════════════════════════════════════════════════════════════════════ */
function setupRecognizer() {
  const audioCfg = SpeechSDK.AudioConfig.fromStreamInput(pushStream);
  recognizer = new SpeechSDK.SpeechRecognizer(speechCfg, audioCfg);

  recognizer.recognizing = (_s, e) => {
    const text = e.result.text.trim();
    if (!text) return;
    if (STATE === 'speaking') { doInterrupt(); return; }
    if (STATE === 'listening') {
      showMicPill('listening', text);
      /* Accumulate partial text for AI interrupt analysis */
      _candidateTurnPartial = text;
      _checkAndTriggerAIInterrupt();
    }
  };

  recognizer.recognized = async (_s, e) => {
    if (e.result.reason !== SpeechSDK.ResultReason.RecognizedSpeech) return;
    const text = e.result.text.trim();
    if (!text) return;
    if (STATE !== 'listening') { console.log('[STT] Ignored (STATE=' + STATE + '):', text); return; }
    if (submitLock || text === lastSubmittedText) { console.log('[STT] Dedup skip:', text); return; }
    console.log('[STT] Accepted:', text);

    /* Accumulate finalized phrase into the turn buffer & clear partial */
    _candidateTurnFinal += (_candidateTurnFinal ? ' ' : '') + text;
    _candidateTurnPartial = '';
    /* _accumulatedSpeech survives goListening() VAD-reject calls */
    _accumulatedSpeech += (_accumulatedSpeech ? ' ' : '') + text;

    submitLock = true; STATE = 'thinking'; showMicPill('thinking');
    _stopSilenceWatch();

    try {
      const vad = await fetch('/api/semanticVad', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, is_final: true })
      }).then(r => r.json());
      if (!vad.is_speech || !vad.is_complete) {
        console.log('[VAD] Rejected (' + vad.reason + '):', text);
        submitLock = false; goListening(); return;
      }
    } catch (_) { console.warn('[VAD] Unreachable — submitting anyway'); }

    lastSubmittedText = text;
    $('partial-txt').textContent = '';
    const isAfterInterrupt = wasInterrupt;
    wasInterrupt = false; submitLock = false;

    if (isAfterInterrupt) await handleInterruptResponse(text);
    else await handleUserInput(text);
  };

  recognizer.canceled = (_s, e) => {
    console.warn('[STT] Canceled:', e.reason, e.errorDetails);
    _sttCancelCount++;

    /* StatusCode 1006 = auth failure (expired token) or network drop.
       Always rebuild the recognizer with a fresh token rather than
       restarting on the same dead speechCfg — which causes the infinite
       cancel → restart → cancel loop visible in the console.            */
    const delayMs = Math.min(1000 * _sttCancelCount, 5000); // back-off up to 5 s
    setTimeout(async () => {
      if (STATE === 'done' || _finishCalled) return;
      try {
        await _rebuildRecognizer();
        _sttCancelCount = 0;
        console.log('[STT] Rebuilt and restarted after cancel');
      } catch (err) {
        console.error('[STT] Rebuild failed:', err);
        /* Last resort: try a plain restart on whatever token we have */
        if (STATE === 'listening' || STATE === 'speaking') {
          recognizer.startContinuousRecognitionAsync(
            () => console.log('[STT] Plain restart after rebuild failure'),
            err2 => console.error('[STT] Plain restart also failed:', err2)
          );
        }
      }
    }, delayMs);
  };
}

function startSTT() {
  recognizer.startContinuousRecognitionAsync(
    () => console.log('[STT] Running'),
    err => console.error('[STT] Start failed:', err)
  );
}

/* ════════════════════════════════════════════════════════════════════
   TOKEN REFRESH & RECOGNIZER REBUILD
   ════════════════════════════════════════════════════════════════════

   Azure Speech tokens expire after 10 minutes.  The SDK does NOT
   auto-refresh them.  Without proactive refresh the WebSocket drops
   with "HTTP Authentication failed; no valid credentials available"
   (StatusCode 1006) and the avatar stays stuck in listening mode.

   Two-layer defence:
     1. Proactive refresh every 8 min — hot-swaps speechCfg.authorizationToken
        so the next recognizer cycle always picks up a valid token.
     2. Reactive rebuild — if the recognizer fires a canceled event,
        _rebuildRecognizer() fetches a fresh token, recreates speechCfg
        and the recognizer object, then restarts STT.
   ════════════════════════════════════════════════════════════════════ */

/**
 * Fetch a FORCED-FRESH token (bypassing the backend 9-min cache), update
 * speechCfg, tear down the old recognizer, build a new one, and restart STT.
 * Called from the canceled handler on StatusCode 1006 (auth failure).
 * Using ?force=1 guarantees we get a brand-new Azure token — not the same
 * cached one that just expired and caused the 1006 in the first place.
 */
async function _rebuildRecognizer() {
  console.log('[TokenRefresh] Rebuilding recognizer with force-fresh token…');

  /* 1. Stop old recognizer cleanly (ignore errors — it may already be dead) */
  try {
    await new Promise((res, rej) => {
      recognizer.stopContinuousRecognitionAsync(res, rej);
    });
  } catch (_) { /* already stopped / dead — fine */ }
  try { recognizer.close(); } catch (_) { }

  /* 2. Fetch a FRESH token from Azure, bypassing the backend 9-min cache.
        Without ?force=1 the backend would return the same expired token
        that caused this rebuild, and the new recognizer would fail instantly. */
  const tok = await fetch('/api/getSpeechToken?force=1').then(r => r.json());
  if (tok.error) throw new Error('[TokenRefresh] getSpeechToken error: ' + tok.error);
  console.log('[TokenRefresh] Force-fresh token fetched, expiresAt:', new Date(tok.expiresAt).toISOString());

  /* 3. Hot-swap the auth token on the existing speechCfg object so any
        future recognizer built from it also uses the fresh token. */
  speechCfg.authorizationToken = tok.token;

  /* 4. Recreate the recognizer against the same push-stream */
  setupRecognizer();

  /* 5. Restart STT only if the interview is still active */
  if (STATE !== 'done' && !_finishCalled) {
    startSTT();
    console.log('[TokenRefresh] STT restarted with force-fresh token');
  }
}

/**
 * Start the proactive 9-minute token refresh cycle.
 * Called once when the interview begins (startInterview → after startSTT).
 *
 * The timer fires every 9 minutes and keeps running for the full 30-minute
 * interview (~3 refresh cycles: at 9 min, 18 min, 27 min).  It is stopped
 * ONLY when the interview truly ends (STATE === 'done' or _finishCalled).
 *
 * On each tick we do a normal (cached) fetch — the backend cache ensures we
 * get a token that was issued within the last 9 min, so it still has ≥1 min
 * of headroom.  We hot-swap speechCfg.authorizationToken in place; the Azure
 * SDK picks this up on the next recognition cycle without a full rebuild.
 */
function _startTokenRefresh() {
  if (_tokenRefreshTimer) return;   // already running
  _tokenRefreshTimer = setInterval(async () => {
    /* Only stop the timer when interview is fully over */
    if (STATE === 'done' || _finishCalled) {
      _stopTokenRefresh();
      return;
    }
    console.log('[TokenRefresh] Proactive refresh — fetching new token…');
    try {
      /* Normal (non-forced) fetch — gets a cached-but-still-valid token.
         The backend cache TTL is 9 min, matching this timer interval,
         so each tick either returns a freshly issued token or one that
         has at most 9 min of age (still 1+ min before Azure expiry). */
      const tok = await fetch('/api/getSpeechToken').then(r => r.json());
      if (tok.error) throw new Error(tok.error);
      speechCfg.authorizationToken = tok.token;
      console.log('[TokenRefresh] Token hot-swapped, expiresAt:', new Date(tok.expiresAt).toISOString());
      _sttCancelCount = 0;   // reset cancel counter after a successful refresh
    } catch (err) {
      console.error('[TokenRefresh] Proactive refresh failed, forcing rebuild:', err);
      try { await _rebuildRecognizer(); } catch (e2) { console.error('[TokenRefresh] Rebuild also failed:', e2); }
    }
  }, _TOKEN_REFRESH_MS);
  console.log('[TokenRefresh] Proactive refresh scheduled every', _TOKEN_REFRESH_MS / 60000, 'min — runs for full 30-min interview');
}

/**
 * Stop the proactive refresh cycle (called on interview end / cleanup).
 */
function _stopTokenRefresh() {
  if (_tokenRefreshTimer) {
    clearInterval(_tokenRefreshTimer);
    _tokenRefreshTimer = null;
    console.log('[TokenRefresh] Refresh timer cleared');
  }
}


function goListening(newTurn = false) {
  STATE = 'listening'; submitLock = false;
  showMicPill('listening');
  setStatus('Listening — speak now', 'active');

  if (newTurn) {
    /* Full turn reset — new AI question was just asked */
    _candidateTurnFinal = '';
    _candidateTurnPartial = '';
    _aiInterruptWordCount = 0;
    _aiInterruptTriggered = false;
    _accumulatedSpeech = '';
    _hadEnergyThisTurn = false;
    _silenceCumulMs = 0;
    _silenceTier = 0;
    _postSpeechDone = false;
    _nudgeInFlight = false;
    _lastEnergyMs = Date.now(); /* grace period — don't immediately fire timers */
  }
  _aiInterruptInFlight = false;
  _startSilenceWatch();
}
function stopWordTicker() { clearInterval(wordTicker); wordTicker = null; }

/* ════════════════════════════════════════════════════════════════════
   AI INTERRUPT — Content-quality based interruption of candidate
   ════════════════════════════════════════════════════════════════════ */

/**
 * Called on every STT `recognizing` event.
 * Counts total words spoken this turn (finalized + current partial).
 * Fires a backend analysis call at the first word milestone and then
 * every _AI_INTERRUPT_STRIDE words afterward — only one call in-flight
 * at a time, and never after an interrupt has already been triggered.
 */
function _checkAndTriggerAIInterrupt() {
  if (_aiInterruptTriggered || _aiInterruptInFlight) return;
  if (STATE !== 'listening') return;

  const combined = (_candidateTurnFinal + ' ' + _candidateTurnPartial).trim();
  const totalWords = combined.split(/\s+/).filter(Boolean).length;

  /* Not enough words yet to analyse */
  if (totalWords < _AI_INTERRUPT_FIRST) return;

  /* Only analyse at milestones: 60, 90, 120, 150 ... words */
  const nextMilestone = _aiInterruptWordCount === 0
    ? _AI_INTERRUPT_FIRST
    : _aiInterruptWordCount + _AI_INTERRUPT_STRIDE;
  if (totalWords < nextMilestone) return;

  _aiInterruptWordCount = totalWords;
  _aiInterruptInFlight = true;

  const snapshot = combined;   // capture stable snapshot for the async call
  console.log(`[AIInterrupt] Analysing at ${totalWords} words…`);

  fetch('/api/analyzeCandidate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, partial_text: snapshot }),
  })
    .then(r => r.json())
    .then(result => {
      _aiInterruptInFlight = false;
      if (!result.should_interrupt) {
        console.log(`[AIInterrupt] CONTINUE — ${result.reason}`);
        return;
      }
      /* Guard: state may have changed while the request was in-flight */
      if (STATE !== 'listening' || _aiInterruptTriggered || submitLock) return;

      console.log(`[AIInterrupt] INTERRUPT — ${result.reason}`);
      _aiInterruptTriggered = true;
      doAIInterrupt(snapshot, result.interrupt_phrase || 'Thank you — let me ask you the next question.');
    })
    .catch(err => {
      _aiInterruptInFlight = false;
      console.warn('[AIInterrupt] Analysis error:', err);
    });
}

/**
 * Execute an AI-initiated interrupt:
 *  1. Stop STT recording (we already have the partial text)
 *  2. Immediately speak the short interrupt_phrase via the avatar
 *  3. Stream the next question from /api/aiInterruptStream
 *
 * @param {string} partialText     - Everything the candidate said so far this turn
 * @param {string} interruptPhrase - The bridging phrase the avatar says first
 */
async function doAIInterrupt(partialText, interruptPhrase) {
  if (STATE === 'done') return;

  console.log('[AIInterrupt] Firing — phrase:', interruptPhrase);

  submitLock = true;
  STATE = 'thinking';
  _stopSilenceWatch();
  showMicPill('thinking');
  setStatus('Avatar thinking…', '');
  $('partial-txt').textContent = '';

  await _processSSEStream('/api/aiInterruptStream', {
    session_id: sessionId,
    partial_text: partialText,
    interrupt_phrase: interruptPhrase,
  });

  submitLock = false;
  _aiInterruptTriggered = false;
}

/* ════════════════════════════════════════════════════════════════════
   SILENCE WATCH — adaptive conversation system
   ════════════════════════════════════════════════════════════════════

   Runs every _SW_POLL_MS (300 ms) while STATE === 'listening'.
   Uses RMS energy from the audio worklet to detect real silence vs
   thinking pauses, then escalates through three tiers:

     Post-speech pause  (_POST_SPEECH_MS = 3.5 s after speech stops):
       Candidate spoke words then trailed off → submit what we have,
       or nudge if too short.

     Tier 1  (_SILENCE_TIER1_MS = 4 s of accumulated silence):
       No response after post-speech nudge, OR candidate never spoke
       for 4 s → AI gently rephrases / offers a hint.
       Fires regardless of whether candidate previously spoke, so the
       interview can never get permanently stuck in listening mode.

     Tier 2  (_SILENCE_TIER2_MS - _SILENCE_TIER1_MS after tier 1):
       Still no response after tier 1 nudge → AI gracefully moves on.

   Energy latch: _ENERGY_LATCH_MS (700 ms) of sub-threshold RMS is
   required before silence is "confirmed" — short thinking pauses
   and breath sounds don't trigger timers.
   ════════════════════════════════════════════════════════════════════ */

function _startSilenceWatch() {
  _stopSilenceWatch();                      /* clear any previous interval */
  if (!sessionId || STATE !== 'listening') return;
  _lastEnergyMs = _lastEnergyMs || Date.now();   /* init if never set */
  _silenceWatchIv = setInterval(_silenceWatchLoop, _SW_POLL_MS);
}

function _stopSilenceWatch() {
  if (_silenceWatchIv) { clearInterval(_silenceWatchIv); _silenceWatchIv = null; }
}

function _silenceWatchLoop() {
  /* Guard: only run while genuinely listening and session is live */
  if (STATE !== 'listening' || !sessionId || _finishCalled) {
    _stopSilenceWatch(); return;
  }
  /* Don't fire while a nudge or AI interrupt is already being processed */
  if (_nudgeInFlight || _aiInterruptTriggered || submitLock) return;

  const now = Date.now();
  const msSinceEnergy = now - _lastEnergyMs;
  const silenceConfirmed = msSinceEnergy >= _ENERGY_LATCH_MS;

  if (!silenceConfirmed) {
    /* Candidate is making sound — reset cumulative silence counter */
    _silenceCumulMs = 0;
    return;
  }

  /* True silence — accumulate */
  _silenceCumulMs += _SW_POLL_MS;

  /* ── Post-speech pause: candidate spoke then stopped ─────────────── */
  if (_hadEnergyThisTurn && !_postSpeechDone && _silenceCumulMs >= _POST_SPEECH_MS) {
    _postSpeechDone = true;
    _doPostSpeechPause();
    return;
  }

  /* ── Tier 1: silence for _SILENCE_TIER1_MS ───────────────────────── */
  /* Fires whether or not the candidate spoke — covers both "never spoke"
     and "spoke too briefly / post-speech nudge didn't advance" cases.   */
  if (_silenceTier < 1 && _silenceCumulMs >= _SILENCE_TIER1_MS) {
    _silenceTier = 1;
    _silenceCumulMs = 0;   /* reset for tier 2 measurement */
    _doTier1Nudge();
    return;
  }

  /* ── Tier 2: still silent after tier 1 nudge ─────────────────────── */
  if (_silenceTier >= 1 && _silenceTier < 2 && _silenceCumulMs >= (_SILENCE_TIER2_MS - _SILENCE_TIER1_MS)) {
    _silenceTier = 2;
    _doTier2MoveOn();
  }
}

/**
 * Post-speech pause: candidate spoke some words then went quiet for 3.5 s.
 * If we have enough accumulated speech → submit it.
 * If too short to be a real answer → treat as silence, do a rephrase nudge.
 */
async function _doPostSpeechPause() {
  const text = _accumulatedSpeech.trim();
  const words = text ? text.split(/\s+/).length : 0;
  console.log(`[SilenceWatch] Post-speech pause — ${words} word(s) accumulated`);

  if (words >= 6) {
    /* Enough words for a real answer — submit directly, bypassing VAD strictness */
    console.log('[SilenceWatch] Submitting accumulated speech:', text.slice(0, 60));
    _stopSilenceWatch();
    submitLock = true; STATE = 'thinking'; showMicPill('thinking');
    $('partial-txt').textContent = '';
    await handleUserInput(text);
  } else {
    /* Too short — probably a false start; give a gentle post-silence nudge */
    console.log('[SilenceWatch] Accumulated text too short — post_silence nudge');
    await _fireNudge('post_silence', text);
  }
}

/**
 * Tier 1: pure silence for 8 s — candidate never started speaking.
 * AI gently rephrases the question or offers a hint.
 */
async function _doTier1Nudge() {
  console.log('[SilenceWatch] Tier 1 — rephrase nudge');
  await _fireNudge('rephrase', '');
}

/**
 * Tier 2: still silent after tier 1 nudge (12 more seconds).
 * AI gracefully acknowledges and moves to the next question.
 */
async function _doTier2MoveOn() {
  console.log('[SilenceWatch] Tier 2 — move on');
  await _fireNudge('move_on', _accumulatedSpeech.trim());
}

/**
 * Core nudge dispatcher — calls /api/nudgeCandidate via the shared
 * SSE stream processor so the avatar speaks naturally.
 *
 * @param {string} nudgeType - 'rephrase' | 'move_on' | 'post_silence'
 * @param {string} candidateText - what candidate said this turn (may be empty)
 */
async function _fireNudge(nudgeType, candidateText) {
  if (_nudgeInFlight || STATE === 'done' || _finishCalled) return;
  /* Extra guard: if candidate just started speaking while we were scheduling */
  if (submitLock) return;

  _nudgeInFlight = true;
  _stopSilenceWatch();
  submitLock = true; STATE = 'thinking'; showMicPill('thinking');
  setStatus('Avatar thinking…', '');
  $('partial-txt').textContent = '';

  console.log(`[Nudge] Firing type=${nudgeType} text="${(candidateText || '').slice(0, 40)}"`);

  await _processSSEStream('/api/nudgeCandidate', {
    session_id: sessionId,
    nudge_type: nudgeType,
    candidate_text: candidateText || '',
  });

  _nudgeInFlight = false;
  submitLock = false;
  /* goListening(true) is called by TurnEnd after the SSE stream completes */
}

/* ════════════════════════════════════════════════════════════════════
   INTERRUPT
   ════════════════════════════════════════════════════════════════════ */
function doInterrupt() {
  if (STATE !== 'speaking') return;
  STATE = 'listening'; wasInterrupt = true;
  stopWordTicker();
  _stopSilenceWatch();
  avatarSynth.stopSpeakingAsync()
    .then(() => console.log('[Interrupt] Stopped at word ~' + currentWordIdx))
    .catch(() => { });
  $('subtitle-container').innerHTML = '';
  showMicPill('listening');
  setStatus('Listening…', 'active');
  fetch('/api/notifyInterrupt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, word_index: currentWordIdx })
  }).catch(() => { });
  currentWordIdx = 0;
}

/* ════════════════════════════════════════════════════════════════════
   SPEECH SYNTHESIS
   ════════════════════════════════════════════════════════════════════ */
async function speakText(text) {
  if (!avatarSynth || !text) return;
  try { await avatarSynth.stopSpeakingAsync(); } catch (_) { }

  /* Split the full text into sentences */
  const sents = text.match(/.*?[.!?]+(?:\s|$)|.+/g)
    ?.map(s => s.trim()).filter(s => s.length > 0) || [text];

  /* Compute cumulative word offsets for each sentence */
  currentSubtitleSentences = [];
  let wCount = 0;
  for (let s of sents) {
    const wc = s.trim().split(/\s+/).length;
    currentSubtitleSentences.push({
      text: s,
      startWord: wCount,
      endWord: wCount + wc       /* exclusive — subtitle clears at this boundary */
    });
    wCount += wc;
  }

  /* Reset state */
  $('subtitle-container').innerHTML = '';
  $('subtitle-container').dataset.activeText = '';
  currentWordIdx = 0;
  stopWordTicker();

  /* Tick word index ~every 380 ms while avatar is speaking */
  wordTicker = setInterval(() => {
    if (STATE === 'speaking') {
      currentWordIdx++;
      updateSubtitles();
    }
  }, 320);  /* tuned for lower latency */

  /*
   * speakTextAsync callback fires when Azure ACCEPTS the TTS request,
   * NOT when the avatar finishes speaking. We resolve on TurnEnd instead
   * (via _avatarSpeechResolve) so that:
   *   await speakText(farewell)   ← truly waits until avatar goes silent
   * A 30 s safety timeout prevents hanging if TurnEnd never fires.
   */
  return new Promise(resolve => {
    const _timeout = setTimeout(() => {
      _avatarSpeechResolve = null;
      console.warn('[speakText] TurnEnd timeout — resolving anyway');
      resolve();
    }, 30000);

    _avatarSpeechResolve = () => {
      clearTimeout(_timeout);
      resolve();
    };

    avatarSynth.speakTextAsync(
      text,
      () => { /* TTS accepted — TurnEnd will resolve the promise */ },
      er => {
        console.error('[TTS]', er);
        clearTimeout(_timeout);
        _avatarSpeechResolve = null;
        resolve();
      }
    );
  });
}

function updateSubtitles() {
  const container = $('subtitle-container');

  /* Clear subtitles when avatar is not speaking */
  if (STATE !== 'speaking') {
    container.innerHTML = '';
    container.dataset.activeText = '';
    return;
  }

  /* Find which sentence the current word index falls inside */
  const activeSent = currentSubtitleSentences.find(
    s => currentWordIdx >= s.startWord && currentWordIdx < s.endWord
  ) || null;

  if (activeSent) {
    /* Show sentence — re-render only when sentence changes (triggers CSS fade-in) */
    if (container.dataset.activeText !== activeSent.text) {
      container.dataset.activeText = activeSent.text;
      container.innerHTML = `<div class="subtitle-line">${activeSent.text}</div>`;
    }
  } else {
    /* Between sentences or after the last one — clear the subtitle */
    container.innerHTML = '';
    container.dataset.activeText = '';
  }
}

/* ════════════════════════════════════════════════════════════════════
   INTERVIEW FLOW
   ════════════════════════════════════════════════════════════════════ */
async function handleUserInput(text) {
  await _processSSEStream('/api/userResponseStream', { session_id: sessionId, text });
}

async function handleInterruptResponse(text) {
  await _processSSEStream('/api/resumeAfterInterruptStream', { session_id: sessionId, text });
}

/**
 * Shared SSE stream processor for sentence-by-sentence TTS.
 * Reads sentences from the server and immediately queues each one
 * on the avatar synthesizer so it starts speaking ~1-3s earlier.
 */
async function _processSSEStream(url, body) {
  STATE = 'thinking'; setStatus('Thinking…', '');
  showMicPill('thinking');

  /* Reset streaming state */
  _speechQueue = [];
  _speechPending = 0;
  _streamComplete = false;
  _interviewEndedFlag = false;
  _isStreamingMode = true;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      console.error('[Stream] HTTP error:', response.status, errData);
      _isStreamingMode = false;
      setStatus('Error — retrying…', 'error');
      setTimeout(goListening, 1500);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      /* Process complete SSE messages (delimited by \n\n) */
      while (true) {
        const idx = buffer.indexOf('\n\n');
        if (idx === -1) break;
        const message = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        for (const line of message.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));

            if (data.type === 'sentence' && data.text) {
              _speechQueue.push(data.text);
              _speechPending++;
              console.log('[Stream] Sentence:', data.text.slice(0, 50));
              avatarSynth.speakTextAsync(
                data.text,
                () => { /* TTS accepted — TurnStart/TurnEnd will handle state */ },
                err => {
                  console.error('[TTS Stream]', err);
                  _speechPending = Math.max(0, _speechPending - 1);
                }
              );
            } else if (data.type === 'error') {
              console.error('[Stream] Server error:', data.message);
            } else if (data.type === 'done') {
              _interviewEndedFlag = data.interview_ended || false;
              _streamComplete = true;
              console.log('[Stream] Done. interview_ended:', _interviewEndedFlag,
                'pending:', _speechPending);

              /* Edge case: no sentences (empty response or immediate end) */
              if (_speechPending <= 0) {
                _isStreamingMode = false;
                if (_interviewEndedFlag && !_finishCalled) {
                  finishInterview();
                } else {
                  goListening(true);
                }
              }
            }
          } catch (parseErr) {
            console.warn('[Stream] Parse error:', parseErr);
          }
        }
      }
    }
  } catch (e) {
    console.error('[SSEStream]', e);
    _isStreamingMode = false;
    setStatus('Error — retrying…', 'error');
    setTimeout(goListening, 1500);
  }
}

/* ════════════════════════════════════════════════════════════════════
   START INTERVIEW
   ════════════════════════════════════════════════════════════════════ */
async function startInterview() {
  for (let i = 0; i < 50; i++) {
    if (typeof SpeechSDK !== 'undefined') break;
    await new Promise(r => setTimeout(r, 100));
  }
  if (typeof SpeechSDK === 'undefined') {
    alert('Azure Speech SDK has not loaded.\nDisable ad-blocker and refresh.');
    return;
  }

  const resumeId = ($('resume-id-input').value || '').trim();
  const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

  function showSetupError(msg) {
    const el = $('setup-error'); el.textContent = msg; el.style.display = 'block';
  }
  $('setup-error').style.display = 'none';

  if (!resumeId) { showSetupError('Please enter a Candidate ID.'); $('resume-id-input').focus(); return; }
  if (!uuidRe.test(resumeId)) {
    showSetupError('Invalid Candidate ID.\nMust contain a UUID.\nVisit /api/debug/resumes to see real IDs.');
    $('resume-id-input').focus(); return;
  }

  $('start-btn').disabled = true;
  setStatus('Initializing…', '');

  try {
    showOverlay('Fetching candidate data from database…');
    const data = await fetch('/api/startInterview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resume_id: resumeId })
    }).then(r => r.json());

    if (data.error) throw new Error(data.error);
    sessionId = data.session_id;

    // Avatar config comes from Cosmos DB resume metadata with lowercase keys:
    // { avatar_character, avatar_style, tts_voice }
    if (data.avatar && data.avatar.avatar_character) {
      // Azure TTS Avatar SDK is case-sensitive: "lisa" fails silently, "Lisa" works.
      // Normalise to Title Case (first letter upper, rest lower) for every character name.
      const raw = data.avatar.avatar_character;
      CFG.avatarCharacter = raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
      if (data.avatar.avatar_style) CFG.avatarStyle = data.avatar.avatar_style;
      if (data.avatar.tts_voice) CFG.ttsVoice = data.avatar.tts_voice;
      console.log('[Avatar] Config loaded from Cosmos DB:',
        CFG.avatarCharacter, CFG.avatarStyle, CFG.ttsVoice);
    } else {
      console.warn('[Avatar] No avatar metadata in Cosmos DB response — using .env defaults:',
        CFG.avatarCharacter, CFG.avatarStyle, CFG.ttsVoice);
    }

    showOverlay('Requesting microphone access…');
    /* Release setup mic stream if user pre-enabled it — buildAudioPipeline will re-acquire */
    if (setupMicStream) { setupMicStream.getTracks().forEach(t => t.stop()); setupMicStream = null; }
    await buildAudioPipeline();
    await setupAvatar();
    setupRecognizer();

    /* ── Transition: setup → interview ── */
    $('setup-screen').style.display = 'none';
    $('phase-bar').style.display = 'block';
    $('timer').style.display = 'block';
    $('end-interview-btn').style.display = 'flex';

    /* If camera was ON in setup, move it to interview PIP */
    if (camOn && camStream) {
      $('cam-video').srcObject = camStream;   /* reuse same stream */
      _activatePIP();
    }

    hideOverlay();

    /* Start composited recording after a short delay so the avatar's
       WebRTC video and audio tracks are fully established before we
       try to tap them. Without this, srcObject may have no audio tracks yet. */
    setTimeout(startRecording, 1500);

    startEpoch = Date.now();
    timerIv = setInterval(tickTimer, 1000);
    pollIv = setInterval(pollStatus, 5000);

    startSTT();
    _startTokenRefresh();   // proactive 8-min token renewal — prevents STT auth expiry

    if (data.greeting) {
      setStatus('Avatar speaking…', 'active');
      await speakText(data.greeting);
    } else {
      goListening();
    }

  } catch (e) {
    console.error('[startInterview]', e);
    const errMsg = e.message || 'Unknown error';
    $('start-btn').disabled = false;
    setStatus('Error', 'error');
    hideOverlay();
    if ($('setup-screen').style.display !== 'none') {
      const el = $('setup-error');
      el.textContent = 'Failed to start interview: ' + errMsg;
      el.style.display = 'block';
    } else {
      alert('Failed to start interview:\n\n' + errMsg);
    }
  }
}

/* ════════════════════════════════════════════════════════════════════
   END INTERVIEW
   ════════════════════════════════════════════════════════════════════ */
async function endInterview() {
  if (!confirm('End the interview now?')) return;
  if (STATE === 'speaking') {
    try { await avatarSynth.stopSpeakingAsync(); } catch (_) { }
  }
  STATE = 'thinking';
  await handleUserInput('end interview');
}

async function finishInterview() {
  if (_finishCalled) return;
  _finishCalled = true;

  /*
   * Called the moment TurnEnd fires after the avatar's closing remark —
   * i.e. the avatar has FULLY finished speaking.
   *
   * UI contract:
   *   • Camera + mic pill disappear immediately (no delay).
   *   • Status badge shows "Interview completed." right away.
   *   • All uploads (transcription, recording, evaluation) run silently
   *     in the background — no "Saving…" or progress text shown to user.
   */

  /* ── 1. Lock state + stop all UI intervals ───────────────────────────── */
  STATE = 'done';
  clearInterval(timerIv);
  clearInterval(pollIv);
  _stopBackgroundSaves();
  stopWordTicker();
  _stopSilenceWatch();
  _stopTokenRefresh();   // clear 8-min token refresh — interview is over

  /* ── 2. Immediate UI cleanup — happens before any async work ─────────── */
  $('subtitle-container').innerHTML = '';
  $('end-interview-btn').style.display = 'none';
  showMicPill('');
  setStatus('Interview completed.');   // final status — never changes again

  /* ── 3. Stop MediaRecorder FIRST — while mic, camera, and avatar tracks
          are still alive. This ensures the WebM container gets a proper
          end-of-stream marker and all frames/audio are flushed. ────────── */
  let recorderStopped = false;
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    try {
      await new Promise(resolve => {
        mediaRecorder.onstop = resolve;
        mediaRecorder.stop();
      });
      /* Give ondataavailable one tick to fire for the very last chunk */
      await new Promise(r => setTimeout(r, 200));
      recorderStopped = true;
      console.log('[Finish] MediaRecorder stopped cleanly — chunks:', recordedChunks.length);
    } catch (e) {
      console.warn('[Finish] MediaRecorder stop error:', e);
    }
  }
  if (canvasDrawLoop) { cancelAnimationFrame(canvasDrawLoop); canvasDrawLoop = null; }
  $('rec-badge').style.display = 'none';

  /* ── 4. NOW safe to kill camera — recording already captured everything ── */
  _shutCamera();
  console.log('[Finish] Camera off');

  /* ── 5. Stop STT + mic audio pipeline ───────────────────────────────── */
  try { if (recognizer) await recognizer.stopContinuousRecognitionAsync(); } catch (_) { }
  try { if (pushStream) { pushStream.close(); pushStream = null; } } catch (_) { }
  try { if (audioCtx) { audioCtx.close(); audioCtx = null; } } catch (_) { }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }

  /* ── 6. Release avatar connection (fire-and-forget, silent) ─────────── */
  if (clientId) {
    fetch('/api/releaseClient', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId })
    }).catch(() => { });
  }
  if (avatarSynth) { try { avatarSynth.close(); } catch (_) { } avatarSynth = null; }
  avatarPC = null;

  /* ── 7. Background saves — all silent, no UI updates ────────────────────
          Runs async without await so the UI is never blocked.
          Order matters: transcription first (sets final_folder_ts on server),
          then recording (uses that ts for blob naming), then evaluation.     */
  (async () => {
    /* Show saving overlay to candidate */
    showOverlay('Please wait — securing your session…');

    /* 7a. Save transcription (JSON) */
    if (sessionId) {
      try {
        showOverlay('Verifying session integrity… (1/3)');
        const res = await fetch('/api/saveTranscription', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, save_reason: 'final' })
        });
        const d = await res.json();
        console.log('[Finish] Transcription ✓ turns:', d.turns_saved, '| folder:', d.folder);
      } catch (e) {
        console.warn('[Finish] Transcription save failed:', e);
      }
    }

    /* 7b. Trigger AI evaluation (PDF) — awaited so WebM upload follows after */
    showOverlay('Processing session data… (2/3)');
    console.log('[Finish] Triggering AI Evaluation...');
    if (sessionId) {
      try {
        const res = await fetch('/api/finalizeInterview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId })
        });
        const data = await res.json();
        console.log('[Finish] Evaluation queued:', data);
      } catch (err) {
        console.error('[Finish] Evaluation trigger failed:', err);
      }
    }

    /* 7c. Upload full recording (WebM) */
    if (sessionId && recordedChunks.length) {
      showOverlay('Encrypting and transmitting session media… (3/3)');
      const mime = (mediaRecorder && mediaRecorder.mimeType) || 'video/webm';
      const fullBlob = new Blob(recordedChunks, { type: mime });
      console.log(`[Finish] Uploading full recording: ${(fullBlob.size / 1024 / 1024).toFixed(2)} MB`);
      const fd = new FormData();
      fd.append('session_id', sessionId);
      fd.append('recording', fullBlob, `recording_${_sessionTs || 'final'}.webm`);
      try {
        const res = await fetch('/api/uploadRecording', { method: 'POST', body: fd });
        const data = await res.json();
        if (res.ok) {
          console.log(`[Finish] Recording uploaded ✓ → ${data.blob?.blob_url}`);
        } else {
          console.error('[Finish] Recording upload failed:', data.error || data);
        }
      } catch (err) {
        console.error('[Finish] Recording upload error:', err);
      }
    } else {
      console.warn('[Finish] No recording chunks to upload');
    }

    if (recAudioCtx) { recAudioCtx.close().catch(() => { }); recAudioCtx = null; recDestination = null; }

    showOverlay('Interview complete. Thank you — you may now close this tab.');
    console.log('[Finish] All background saves complete');
  })();
}
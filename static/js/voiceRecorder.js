/** Portable browser recording plus owner-scoped speech-to-text. */

let mediaRecorder = null;
let activeStream = null;
let audioChunks = [];
let isRecording = false;
let recordingInterval = null;
let autoStopTimer = null;
let _recognition = null;
let _browserTranscript = '';
let _sttProvider = 'disabled';
let _sttLanguage = '';

const MAX_RECORDING_SECONDS = 300;
const RECORDING_FORMATS = [
  { mime: 'audio/webm;codecs=opus', blob: 'audio/webm', ext: 'webm' },
  { mime: 'audio/ogg;codecs=opus', blob: 'audio/ogg', ext: 'ogg' },
  { mime: 'audio/mp4', blob: 'audio/mp4', ext: 'mp4' },
  { mime: '', blob: 'audio/webm', ext: 'webm' },
];

function recordingFormat() {
  if (!window.MediaRecorder) return null;
  return RECORDING_FORMATS.find((item) => !item.mime || MediaRecorder.isTypeSupported(item.mime)) || null;
}

async function refreshSttProvider() {
  try {
    const response = await fetch('/api/stt/preferences', { credentials: 'same-origin' });
    if (!response.ok) return;
    const prefs = await response.json();
    _sttProvider = prefs.enabled === false ? 'disabled' : (prefs.provider || 'disabled');
    _sttLanguage = prefs.language || '';
    if (window._updateSendBtnIcon) window._updateSendBtnIcon();
  } catch (error) {
    console.warn('Failed to fetch STT preferences:', error);
  }
}

function resetRecordingUI() {
  isRecording = false;
  if (recordingInterval) clearInterval(recordingInterval);
  if (autoStopTimer) clearTimeout(autoStopTimer);
  recordingInterval = null;
  autoStopTimer = null;
  if (activeStream) activeStream.getTracks().forEach((track) => track.stop());
  activeStream = null;
  const sendButton = document.querySelector('.send-btn');
  if (sendButton) {
    sendButton.classList.remove('recording');
    sendButton.dataset.mode = '';
  }
  setTimeout(() => window._updateSendBtnIcon?.(), 50);
}

function startBrowserRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return false;
  _browserTranscript = '';
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = false;
  if (_sttLanguage) _recognition.lang = _sttLanguage;
  _recognition.onresult = (event) => {
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) _browserTranscript += `${event.results[index][0].transcript} `;
    }
  };
  _recognition.onerror = (event) => console.warn('Browser STT error:', event.error);
  try {
    _recognition.start();
    return true;
  } catch (error) {
    _recognition = null;
    return false;
  }
}

function stopBrowserRecognition() {
  if (_recognition) {
    try { _recognition.stop(); } catch (_) { /* already stopped */ }
    _recognition = null;
  }
  return _browserTranscript.trim();
}

async function transcribeOnServer(audioBlob, format) {
  const formData = new FormData();
  formData.append('file', audioBlob, `voice-message.${format.ext}`);
  const response = await fetch('/api/stt/transcribe', {
    method: 'POST', credentials: 'same-origin', body: formData,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || {};
    throw new Error(detail.message || 'Transcription failed');
  }
  return payload.text || '';
}

function insertTranscription(text, showToast) {
  if (!text) return false;
  const input = document.getElementById('message');
  if (!input) return false;
  input.value = input.value.trim() ? `${input.value.trim()} ${text}` : text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  showToast?.('Transcribed');
  return true;
}

function attachRecording(blob, format, onFileCreated) {
  if (!onFileCreated) return;
  onFileCreated(new File(
    [blob], `voice-message-${Date.now()}.${format.ext}`, { type: format.blob },
  ));
}

export function startRecording(onFileCreated, showToast, showError) {
  const host = window.location.hostname;
  const loopback = host === 'localhost' || host === '127.0.0.1' || host === '::1';
  if (!window.isSecureContext && !loopback) {
    showError?.('Your browser blocks microphones on remote HTTP pages. Odysseus can stay HTTP; open it through localhost or trust this origin in your browser.');
    resetRecordingUI();
    return;
  }
  const format = recordingFormat();
  if (!navigator.mediaDevices?.getUserMedia || !format) {
    showError?.('Microphone recording is not supported in this browser.');
    resetRecordingUI();
    return;
  }

  audioChunks = [];
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    activeStream = stream;
    const options = format.mime ? { mimeType: format.mime } : undefined;
    mediaRecorder = new MediaRecorder(stream, options);
    const actualType = (mediaRecorder.mimeType || format.blob).split(';', 1)[0];
    const detectedFormat = RECORDING_FORMATS.find((item) => item.blob === actualType);
    const actualFormat = { ...(detectedFormat || format), blob: actualType };

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size) audioChunks.push(event.data);
    };
    mediaRecorder.onerror = () => {
      showError?.('Recording failed.');
      resetRecordingUI();
    };
    mediaRecorder.onstop = async () => {
      stopBrowserRecognition();
      const blob = new Blob(audioChunks, { type: actualFormat.blob });
      try {
        if (_sttProvider === 'browser') {
          if (!insertTranscription(_browserTranscript.trim(), showToast)) {
            showToast?.('No speech detected; recording attached.');
            attachRecording(blob, actualFormat, onFileCreated);
          }
        } else if (_sttProvider === 'local' || _sttProvider.startsWith('endpoint:')) {
          showToast?.(_sttProvider === 'local' ? 'Transcribing locally…' : 'Sending audio for transcription…', 10000);
          try {
            const transcript = await transcribeOnServer(blob, actualFormat);
            if (!insertTranscription(transcript, showToast)) showToast?.('No speech detected');
          } catch (error) {
            showError?.(`Transcription failed: ${error.message}. Recording attached instead.`);
            attachRecording(blob, actualFormat, onFileCreated);
          }
        } else {
          attachRecording(blob, actualFormat, onFileCreated);
        }
      } finally {
        resetRecordingUI();
      }
    };

    mediaRecorder.start(1000);
    isRecording = true;
    if (_sttProvider === 'browser' && !startBrowserRecognition()) {
      showError?.('Browser speech recognition is unavailable; recording will be attached.');
      _sttProvider = 'disabled';
    }
    const started = Date.now();
    recordingInterval = setInterval(() => {
      const elapsed = Math.floor((Date.now() - started) / 1000);
      const sendButton = document.querySelector('.send-btn');
      if (sendButton) sendButton.title = `Stop recording (${elapsed}s)`;
    }, 1000);
    autoStopTimer = setTimeout(() => stopRecording(), MAX_RECORDING_SECONDS * 1000);
    showToast?.('Recording… click stop when finished');
  }).catch((error) => {
    if (error.name === 'NotAllowedError') showError?.('Microphone access denied. Check this site’s browser permissions.');
    else if (error.name === 'NotFoundError') showError?.('No microphone found.');
    else showError?.(`Microphone error: ${error.message}`);
    resetRecordingUI();
  });
}

export function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
  else resetRecordingUI();
}

export function getIsRecording() { return isRecording; }

export function init() {
  isRecording = false;
  refreshSttProvider();
  window.addEventListener('stt-preferences-changed', (event) => {
    _sttProvider = event.detail?.provider || 'disabled';
    _sttLanguage = event.detail?.language || '';
    window._updateSendBtnIcon?.();
  });
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  getIsRecording,
  init,
  refreshSttProvider,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(value) { _sttProvider = value; },
};

export default voiceRecorderModule;

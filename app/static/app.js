const form = document.querySelector('#job-form');
const submitButton = document.querySelector('#submit');
const filesInput = document.querySelector('#files');
const fileList = document.querySelector('#file-list');
const statusCard = document.querySelector('#status-card');
const jobTitle = document.querySelector('#job-title');
const jobStage = document.querySelector('#job-stage');
const jobIdEl = document.querySelector('#job-id');
const progressBar = document.querySelector('#progress-bar');
const jobDetails = document.querySelector('#job-details');
const downloads = document.querySelector('#downloads');
const errors = document.querySelector('#errors');
const recentJobs = document.querySelector('#recent-jobs');

const voiceSelect = document.querySelector('#voice-select');
const voiceNote = document.querySelector('#voice-note');
const silence = document.querySelector('#silence');
const silenceLabel = document.querySelector('#silence-label');
const subtitlesInput = document.querySelector('#generate-subtitles');

const selectedFilesSummary = document.querySelector('#selected-files-summary');
const exportSummary = document.querySelector('#export-summary');
const voiceSummary = document.querySelector('#voice-summary');
const subtitleSummary = document.querySelector('#subtitle-summary');
const pauseSummary = document.querySelector('#pause-summary');
const jobSummary = document.querySelector('#job-summary');

const ranges = [
  { range: '#rate-range', hidden: '#rate', label: '#rate-label', suffix: '%' },
  { range: '#volume-range', hidden: '#volume', label: '#volume-label', suffix: '%' },
  { range: '#pitch-range', hidden: '#pitch', label: '#pitch-label', suffix: 'Hz' },
];

let pollTimer = null;

function signedValue(value, suffix) {
  const number = Number(value);
  return `${number >= 0 ? '+' : ''}${number}${suffix}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[char]));
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(ms) {
  if (!ms) return '—';
  const seconds = Math.round(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}

function getOutputMode() {
  return form.querySelector('input[name="output_mode"]:checked')?.value || 'both';
}

function getOutputModeLabel(mode = getOutputMode()) {
  return {
    both: 'Both outputs',
    separate: 'Separate files',
    combined: 'Combined story',
  }[mode] || 'Both outputs';
}

function getTuningSummary() {
  const rate = document.querySelector('#rate')?.value || '+0%';
  const pitch = document.querySelector('#pitch')?.value || '+0Hz';
  const volume = document.querySelector('#volume')?.value || '+0%';
  const changes = [];

  if (rate !== '+0%') changes.push(`speed ${rate}`);
  if (pitch !== '+0Hz') changes.push(`pitch ${pitch}`);
  if (volume !== '+0%') changes.push(`volume ${volume}`);

  return changes.length ? changes.join(', ') : 'normal voice';
}

function updateSummary() {
  const files = [...filesInput.files];
  const outputMode = getOutputMode();
  const subtitlesEnabled = subtitlesInput?.checked !== false;
  const selectedVoice = voiceSelect?.value || 'Default voice';
  const pauseText = `${silence.value} ms`;

  selectedFilesSummary.textContent = files.length
    ? `${files.length} file${files.length === 1 ? '' : 's'} selected`
    : 'None selected';
  exportSummary.textContent = getOutputModeLabel(outputMode);
  voiceSummary.textContent = selectedVoice;
  subtitleSummary.textContent = subtitlesEnabled ? 'SRT enabled' : 'SRT disabled';
  pauseSummary.textContent = pauseText;
  silenceLabel.textContent = pauseText;
  jobSummary.textContent = `${getOutputModeLabel(outputMode)} · ${subtitlesEnabled ? 'SRT on' : 'SRT off'} · ${getTuningSummary()}`;
}

function renderFileList() {
  const files = [...filesInput.files];
  if (!files.length) {
    fileList.className = 'file-list empty';
    fileList.textContent = 'No files selected yet.';
    updateSummary();
    return;
  }

  const previewLimit = 12;
  const rows = files.slice(0, previewLimit).map((file, index) => `
    <div class="file-row">
      <span>${index + 1}. ${escapeHtml(file.name)}</span>
      <span>${formatBytes(file.size)}</span>
    </div>
  `);

  if (files.length > previewLimit) {
    rows.push(`
      <div class="file-row">
        <span>+ ${files.length - previewLimit} more file${files.length - previewLimit === 1 ? '' : 's'}</span>
        <span>${formatBytes(files.reduce((sum, file) => sum + file.size, 0))} total</span>
      </div>
    `);
  }

  fileList.className = 'file-list';
  fileList.innerHTML = rows.join('');
  updateSummary();
}

function setupRanges() {
  for (const item of ranges) {
    const range = document.querySelector(item.range);
    const hidden = document.querySelector(item.hidden);
    const label = document.querySelector(item.label);
    const update = () => {
      const value = signedValue(range.value, item.suffix);
      hidden.value = value;
      label.textContent = value;
      updateSummary();
    };
    range.addEventListener('input', update);
    update();
  }
}

function setupModals() {
  for (const opener of document.querySelectorAll('[data-modal-open]')) {
    opener.addEventListener('click', () => {
      const modal = document.querySelector(`#${opener.dataset.modalOpen}`);
      if (!modal) return;
      if (typeof modal.showModal === 'function') {
        modal.showModal();
      } else {
        modal.setAttribute('open', '');
      }
    });
  }

  for (const modal of document.querySelectorAll('dialog.modal')) {
    modal.addEventListener('click', event => {
      if (event.target === modal) modal.close();
    });

    for (const closeButton of modal.querySelectorAll('[data-modal-close]')) {
      closeButton.addEventListener('click', () => modal.close());
    }
  }
}

async function loadVoices() {
  try {
    const response = await fetch('/api/voices');
    const data = await response.json();
    const current = voiceSelect.value;
    voiceSelect.innerHTML = '';

    for (const voice of data.voices) {
      const option = document.createElement('option');
      option.value = voice.short_name;
      option.textContent = `${voice.short_name} — ${voice.gender || 'voice'}${voice.locale ? `, ${voice.locale}` : ''}`;
      voiceSelect.appendChild(option);
    }

    if ([...voiceSelect.options].some(option => option.value === current)) {
      voiceSelect.value = current;
    } else if ([...voiceSelect.options].some(option => option.value === 'en-US-AriaNeural')) {
      voiceSelect.value = 'en-US-AriaNeural';
    }

    voiceNote.textContent = data.source === 'edge-tts'
      ? `Loaded ${data.voices.length} voices.`
      : 'Using fallback voices. The full list loads when the voice service is reachable.';
  } catch (error) {
    voiceNote.textContent = `Could not load voices: ${error.message}`;
  } finally {
    updateSummary();
  }
}

async function refreshRecentJobs() {
  try {
    const response = await fetch('/api/jobs?limit=10');
    const data = await response.json();
    if (!data.jobs.length) {
      recentJobs.textContent = 'No jobs yet.';
      return;
    }

    recentJobs.innerHTML = data.jobs.map(job => `
      <div class="job-row">
        <span>
          <strong>${escapeHtml(job.status)}</strong>
          <small>${escapeHtml(job.stage || '')}</small>
        </span>
        <span><a href="#" data-job="${job.id}">${job.progress ?? 0}% · ${job.file_count ?? 0} files</a></span>
      </div>
    `).join('');

    for (const link of recentJobs.querySelectorAll('[data-job]')) {
      link.addEventListener('click', event => {
        event.preventDefault();
        statusCard.classList.remove('hidden');
        startPolling(link.dataset.job);
      });
    }
  } catch (error) {
    recentJobs.textContent = `Could not load recent jobs: ${error.message}`;
  }
}

async function createJob(event) {
  event.preventDefault();

  if (!filesInput.files.length) {
    downloads.innerHTML = "";
    errors.textContent = 'Choose at least one .txt file or a .zip containing text files first.';
    statusCard.classList.remove('hidden');
    statusCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = 'Creating…';
  downloads.innerHTML = '';
  errors.textContent = '';

  try {
    const formData = new FormData(form);
    if (!formData.has('generate_subtitles')) formData.append('generate_subtitles', 'false');

    const response = await fetch('/api/jobs', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not create job');

    statusCard.classList.remove('hidden');
    statusCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    startPolling(data.id);
  } catch (error) {
    errors.textContent = error.message;
    statusCard.classList.remove('hidden');
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = 'Create narration';
  }
}

function startPolling(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  renderLoading(jobId);
  poll(jobId);
  pollTimer = setInterval(() => poll(jobId), 2000);
}

function renderLoading(jobId) {
  jobIdEl.textContent = jobId;
  jobTitle.textContent = 'Loading…';
  jobStage.textContent = 'Reading job status…';
  progressBar.style.width = '0%';
}

async function poll(jobId) {
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || 'Could not read job');
    renderJob(job);
    if (job.status === 'done' || job.status === 'failed') {
      clearInterval(pollTimer);
      pollTimer = null;
      refreshRecentJobs();
    }
  } catch (error) {
    errors.textContent = error.message;
  }
}

function renderJob(job) {
  jobIdEl.textContent = job.id;
  jobTitle.textContent = `${job.status.toUpperCase()} · ${job.progress ?? 0}%`;
  jobStage.textContent = job.stage || '';
  progressBar.style.width = `${job.progress ?? 0}%`;

  const totalDuration = (job.inputs || []).reduce((sum, item) => sum + (item.duration_ms || 0), 0);
  jobDetails.innerHTML = `
    <div class="detail"><b>Files</b>${job.stats?.file_count ?? job.inputs?.length ?? 0}</div>
    <div class="detail"><b>Total characters</b>${(job.stats?.total_chars ?? 0).toLocaleString()}</div>
    <div class="detail"><b>Generated duration</b>${formatDuration(totalDuration)}</div>
    <div class="detail"><b>Voice</b>${escapeHtml(job.options?.voice || '')}</div>
  `;

  if (job.links && Object.keys(job.links).length) {
    const labels = {
      combined_mp3: 'Combined MP3',
      combined_srt: 'Combined SRT',
      separate_zip: 'Separate files ZIP',
      all_outputs_zip: 'All outputs ZIP',
      manifest: 'Manifest JSON'
    };
    downloads.innerHTML = Object.entries(job.links)
      .map(([key, url]) => `<a class="button-link" href="${url}">${labels[key] || key}</a>`)
      .join('');
  } else {
    downloads.innerHTML = '';
  }

  errors.textContent = (job.errors || []).join('\n');
}

filesInput.addEventListener('change', renderFileList);
silence.addEventListener('input', updateSummary);
voiceSelect.addEventListener('change', updateSummary);
subtitlesInput.addEventListener('change', updateSummary);
form.addEventListener('change', event => {
  if (event.target.matches('input[name="output_mode"]')) updateSummary();
});
form.addEventListener('submit', createJob);
document.querySelector('#refresh-jobs').addEventListener('click', refreshRecentJobs);

setupModals();
setupRanges();
updateSummary();
loadVoices();
refreshRecentJobs();

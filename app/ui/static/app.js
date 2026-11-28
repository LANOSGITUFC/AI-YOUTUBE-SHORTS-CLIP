/* Clipper Studio v2 — Frontend Controller (Zero console errors, full SSE sync) */

const VIEW_TITLES = {
  clips: 'Clipes 9:16',
  queue: 'Fila & Esteira',
  calendar: 'Agenda Semanal (4/dia × 7 dias)',
  experiments: 'Laboratório 7 Dias — Testes A/B por Canal',
  analytics: 'Analytics Real, BGM & Google Drive',
  timeline: 'Inspetor de Deduplicação & Linha do Tempo',
  presets: 'Estilo de Legendas & Presets',
  channels: 'Canais Monitorados 24/7',
  destinations: 'Contas YouTube & Pesos',
  settings: 'IA (BYOK) & Configurações',
};

const STAGE_LABELS = {
  idle: 'Esteira ociosa',
  monitoring: 'Varrendo RSS',
  downloading: 'Baixando vídeo',
  transcribing: 'Transcrevendo',
  analyzing: 'Analisando cortes',
  rendering: 'Renderizando 9:16',
  scheduling: 'Agendando slots',
  error: 'Erro no último ciclo',
};

const CROP_LABELS = {
  split_screen: 'Split-Screen',
  blur_bg: 'Blur 16:9',
  face_tracking: 'Center 9:16',
  three: '3 Painéis',
  four: 'Grid 2×2',
};

let currentView = 'clips';
let currentClipFilter = 'all';
let clipsCache = [];
const selectedClipIds = new Set();
let opusPresetsCache = [];
let brandKitsCache = [];
let selectedPresetId = 'think-media';
let knownBootId = null;
let lastSnapshot = '';
let lastSseTick = 0;
let toastTimer = null;

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtSec(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

function showToast(message, type = 'info') {
  const el = document.getElementById('globalToast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast show ${type === 'success' ? 'ok' : type === 'error' ? 'error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
  }, 4200);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(val);
}

function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

/* ==========================================
   NAVIGATION
   ========================================== */

function switchView(view) {
  currentView = view;
  document.querySelectorAll('.view-panel').forEach((p) => {
    p.classList.toggle('active', p.id === `view-${view}`);
  });
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  setText('topbarTitle', VIEW_TITLES[view] || 'Clipper Studio');
  refreshCurrentView();
}

function refreshCurrentView() {
  if (currentView === 'clips') loadClips();
  else if (currentView === 'queue') loadVideosTable();
  else if (currentView === 'calendar') loadCalendar();
  else if (currentView === 'experiments') loadExperimentsView();
  else if (currentView === 'analytics') loadAnalyticsAndStorageView();
  else if (currentView === 'timeline') loadVideosForTimeline();
  else if (currentView === 'presets') {
    loadSettingsIntoUI();
    loadOpusPresetsAndBrandKits();
  } else if (currentView === 'channels') loadChannels();
  else if (currentView === 'destinations') loadDestinations();
  else if (currentView === 'settings') {
    loadSettingsIntoUI();
    loadAIBusEvents();
    loadCookiesStatus();
  }
}

/* ==========================================
   TELEMETRY & SSE REALTIME
   ========================================== */

function applyStatusData(data) {
  if (!data || data.status !== 'online') return;

  setText('kpiChannels', data.monitored_channels ?? 0);
  setText('kpiVideos', data.videos_tracked ?? 0);
  setText('kpiClips', data.clips_total ?? 0);
  setText('kpiRendered', data.clips_rendered ?? 0);
  setText('kpiScheduled', data.scheduled_posts ?? 0);

  setText('navCountChannels', data.monitored_channels ?? 0);
  setText('navCountVideos', data.videos_tracked ?? 0);
  setText('navCountClips', data.clips_total ?? 0);
  setText('navCountScheduled', data.scheduled_posts ?? 0);
  setText('navCountDest', data.destination_channels ?? 0);

  if (data.pipeline) {
    renderPipelineLive(data.pipeline);
  }
}

function renderPipelineLive(p) {
  const dot = document.getElementById('workerDot');
  if (dot) {
    dot.className = `status-dot ${p.busy ? 'busy' : p.stage === 'error' ? 'error' : ''}`;
  }
  setText('workerStageLabel', STAGE_LABELS[p.stage] || p.stage || 'Esteira ociosa');
  setText('workerMsg', p.message || 'Aguardando próximo ciclo');
  const pct = Math.max(0, Math.min(100, Number(p.progress) || 0));
  setText('workerProgressPct', `${pct}%`);
  const bar = document.getElementById('workerProgressBar');
  if (bar) bar.style.width = `${pct}%`;

  const runBtn = document.getElementById('btnRunCycle');
  if (runBtn) {
    runBtn.disabled = Boolean(p.busy);
    runBtn.textContent = p.busy ? 'Processando...' : 'Executar ciclo agora';
  }

  const badge = document.getElementById('queueStageBadge');
  if (badge) {
    badge.textContent = STAGE_LABELS[p.stage] || p.stage || 'idle';
    badge.className = `badge ${p.busy ? 'badge-info' : p.stage === 'error' ? 'badge-err' : 'badge-ok'}`;
  }

  const logsEl = document.getElementById('pipelineLiveLogs');
  if (logsEl && Array.isArray(p.logs)) {
    if (!p.logs.length) {
      logsEl.innerHTML = '<div class="log-line"><span class="log-time">--:--:--</span><span class="log-info">Nenhum evento registrado ainda.</span></div>';
    } else {
      logsEl.innerHTML = p.logs.map((entry) => `
        <div class="log-line">
          <span class="log-time">${esc(entry.time)}</span>
          <span class="log-${esc(entry.level || 'info')}">${esc(entry.message)}</span>
        </div>
      `).join('');
    }
  }
}

function handleRealtimeStatusPayload(data) {
  if (!data || data.status === 'error') return;
  if (data.boot_id) {
    if (knownBootId && knownBootId !== data.boot_id) {
      showToast('Nova versão do servidor detectada. Atualizando painel...', 'info');
      setTimeout(() => window.location.reload(), 500);
      return;
    }
    knownBootId = data.boot_id;
  }
  applyStatusData(data);
  if (typeof data.experiments_count === 'number') {
    setText('navCountExperiments', data.experiments_count);
  }

  const snap = `${data.videos_tracked}-${data.clips_total}-${data.clips_rendered}-${data.scheduled_posts}-${data.destination_channels}-${data.active_destination_channels}-${data.monitored_channels}-${data.experiments_count}-${data.state_hash || ''}`;
  const modalOpen = Boolean(
    document.getElementById('editPostModal')?.classList.contains('open') ||
    document.getElementById('editClipModal')?.classList.contains('open') ||
    document.getElementById('wordsEditorModal')?.classList.contains('open')
  );
  if (lastSnapshot && lastSnapshot !== snap && !dndActive && !modalOpen) {
    refreshCurrentView();
  }
  lastSnapshot = snap;
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    handleRealtimeStatusPayload(data);
  } catch (e) {
    console.error('Status fetch failed:', e);
  }
}

function startRealtimeStream() {
  const es = new EventSource('/api/events/stream');
  es.onmessage = (ev) => {
    try {
      lastSseTick = Date.now();
      handleRealtimeStatusPayload(JSON.parse(ev.data));
    } catch (e) {
      console.error('SSE parse error:', e);
    }
  };
  es.onerror = () => {
    // EventSource reconecta automaticamente; fallback polling cobre qualquer queda
  };
  // Safety net: se SSE ficar >5s sem entregar tick, consulta /api/status automaticamente
  setInterval(() => {
    if (Date.now() - lastSseTick > 5000) {
      fetchStatus();
    }
  }, 4000);
}

/* ==========================================
   QUICK ACTIONS (IMPORT URL / CHECK / RUN)
   ========================================== */

async function importVideoUrl(event) {
  event.preventDefault();
  const input = document.getElementById('quickImportUrl');
  const btn = document.getElementById('btnQuickImport');
  const url = (input?.value || '').trim();
  if (!url) {
    showToast('Cole a URL de um vídeo do YouTube.', 'error');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Importando...';
  try {
    const res = await fetch('/api/videos/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, auto_process: true }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao importar URL.', 'error');
      return;
    }
    input.value = '';
    showToast(data.message || 'Vídeo importado com sucesso!', 'success');
    fetchStatus();
    switchView('queue');
  } catch (e) {
    showToast('Falha de rede ao importar URL.', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Cortar URL';
  }
}

async function triggerCheckChannels() {
  showToast('Varrendo feeds RSS dos canais ativos...', 'info');
  try {
    const res = await fetch('/api/channels/check-now', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Varredura concluída.', 'success');
    fetchStatus();
    refreshCurrentView();
  } catch (e) {
    showToast('Erro ao varrer canais.', 'error');
  }
}

async function triggerPipelineCycle() {
  try {
    const res = await fetch('/api/pipeline/run-cycle', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Ciclo disparado.', data.started ? 'success' : 'info');
    fetchStatus();
  } catch (e) {
    showToast('Erro ao iniciar ciclo da esteira.', 'error');
  }
}

/* ==========================================
   VIEW 1: CLIPS STUDIO
   ========================================== */

function setClipFilter(filter, btnEl) {
  currentClipFilter = filter;
  document.querySelectorAll('[data-clip-filter]').forEach((b) => b.classList.remove('active'));
  if (btnEl) btnEl.classList.add('active');
  renderClipsGrid();
}

async function loadClips() {
  loadClipQueue();
  try {
    const res = await fetch('/api/clips');
    clipsCache = await res.json();
    renderClipsGrid();
  } catch (e) {
    console.error(e);
  }
}

function renderClipsGrid() {
  const container = document.getElementById('clipsGrid');
  if (!container) return;

  const filtered = clipsCache.filter((c) => {
    if (currentClipFilter === 'all') return true;
    if (currentClipFilter === 'liked') return Boolean(c.liked);
    if (currentClipFilter === 'rendered') return c.is_ready;
    return c.status === currentClipFilter;
  });

  if (!filtered.length) {
    container.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1;">
        <h3>Nenhum clipe encontrado neste filtro</h3>
        <p>Cole uma URL do YouTube ou envie um MP4 na barra superior, ou clique em "Executar ciclo agora" para gerar 3 a 7 cortes inéditos.</p>
        <button class="btn btn-primary btn-sm" onclick="triggerPipelineCycle()">Executar ciclo agora</button>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map((c) => {
    const statusBadge = c.status === 'render_failed'
      ? `<span class="badge badge-err" title="O MP4 saiu corrompido ou o FFmpeg falhou. Clique em Renderizar para tentar de novo.">falhou no render</span>`
      : c.is_ready
        ? `<span class="badge badge-ok">${esc(c.status)}</span>`
        : `<span class="badge badge-warn">${esc(c.status)}</span>`;

    const mediaBlock = c.media_url
      ? `<div class="clip-media has-video">
           <video controls preload="metadata" poster="${esc(c.thumbnail_url || '')}" src="${esc(c.media_url)}"></video>
           <span class="clip-score-pill">Score ${esc(c.score)}</span>
         </div>`
      : `<div class="clip-media">
           <div class="clip-placeholder">
             <span class="clip-placeholder-time">${fmtSec(c.start)} — ${fmtSec(c.end)} (${esc(c.duration)}s)</span>
             <span style="font-size:12px;">${c.has_source_video ? 'Pronto para renderizar em 1080×1920' : 'Vídeo original não baixado em disco'}</span>
           </div>
           <span class="clip-score-pill">Score ${esc(c.score)}</span>
         </div>`;

    const isSel = selectedClipIds.has(c.id);
    return `
      <article class="clip-card" style="${isSel ? 'border-color: var(--accent, #e11d48); box-shadow: 0 0 0 1px var(--accent, #e11d48);' : ''}">
        ${mediaBlock}
        <div class="clip-body">
          <div class="clip-meta-top">
            <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
              <input type="checkbox" ${isSel ? 'checked' : ''} onchange="toggleClipSelection(${c.id})" title="Selecionar para ações em lote">
              <span>${esc(c.channel_name)}</span>
            </label>
            <div style="display:flex; align-items:center; gap:6px;">
              ${statusBadge}
              <button class="like-btn ${c.liked ? 'liked' : ''}" onclick="toggleLikeClip(${c.id})" title="${c.liked ? 'Remover dos favoritos' : 'Favoritar clipe'}" aria-label="Favoritar">${c.liked ? '♥' : '♡'}</button>
            </div>
          </div>
          <h3 class="clip-title" title="${esc(c.title)}">${esc(c.title)}</h3>
          <p class="clip-hook">${esc(c.hook || c.video_title)}</p>
          <div class="subscores-row">
            <div class="subscore-cell"><span class="subscore-label">Hook</span><span class="subscore-val">${esc(c.hook_score ?? c.score)}</span></div>
            <div class="subscore-cell"><span class="subscore-label">Ritmo</span><span class="subscore-val">${esc(c.pacing_score ?? c.score)}</span></div>
            <div class="subscore-cell"><span class="subscore-label">História</span><span class="subscore-val">${esc(c.story_score ?? c.score)}</span></div>
          </div>
          <div class="clip-specs">
            <span class="spec-chip">${fmtSec(c.start)} → ${fmtSec(c.end)}</span>
            <span class="spec-chip">${esc(c.duration)}s</span>
            <span class="spec-chip">${(c.editor_config && c.editor_config.layout && c.editor_config.layout.enabled) ? esc(c.editor_config.layout.name || 'Layout personalizado') : esc(CROP_LABELS[c.crop_mode] || c.crop_mode)}</span>
            ${c.is_ready ? `<span class="spec-chip">${esc(c.file_size_mb)} MB</span>` : ''}
            <span class="spec-chip">${esc(c.suggested_by)}</span>
          </div>
        </div>
        <div class="clip-actions">
          <div class="clip-actions-group">
            <button class="btn btn-primary btn-sm" id="btnRender-${c.id}" onclick="renderClip(${c.id})">
              ${c.is_ready ? 'Re-renderizar' : 'Renderizar 9:16'}
            </button>
            <button class="btn btn-sm" onclick="openEditClipModal(${c.id})">Editar</button>
            <button class="btn btn-sm" id="btnSeo-${c.id}" onclick="regenerateSingleClipSeo(${c.id})" title="Gerar novamente título, descrição, tags e hashtags">↻ SEO</button>
            <button class="btn btn-sm" onclick="openLayoutEditor(${c.id})" title="Posicionar câmera, gameplay e legenda">Layout</button>
          </div>
          <div class="clip-actions-group">
            ${c.is_ready ? `<button class="btn btn-sm" onclick="openExportPack(${c.id})">Exportar</button>` : ''}
            <button class="btn btn-danger btn-sm" onclick="deleteClip(${c.id})" title="Excluir clipe">Excluir</button>
          </div>
        </div>
      </article>
    `;
  }).join('');
  updateSelectedClipsCounter();
}

async function toggleLikeClip(clipId) {
  const clip = clipsCache.find((c) => c.id === clipId);
  if (!clip) return;
  const nextLiked = !clip.liked;
  try {
    const res = await fetch(`/api/clips/${clipId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ liked: nextLiked }),
    });
    if (res.ok) {
      clip.liked = nextLiked;
      renderClipsGrid();
    }
  } catch (e) {
    showToast('Erro ao favoritar clipe.', 'error');
  }
}

async function renderClip(clipId) {
  const btn = document.getElementById(`btnRender-${clipId}`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Renderizando...';
  }
  showToast(`Renderizando clipe #${clipId} em 1080×1920 com legendas...`, 'info');
  try {
    const res = await fetch(`/api/clips/${clipId}/render`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || `Erro ao renderizar clipe #${clipId}.`, 'error');
      return;
    }
    showToast(`Clipe #${clipId} renderizado (${(data.file_size_bytes / 1048576).toFixed(1)} MB)!`, 'success');
    await Promise.all([loadClips(), fetchStatus()]);
  } catch (e) {
    showToast(`Erro de rede ao renderizar clipe #${clipId}.`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Renderizar 9:16';
    }
  }
}

async function openEditClipModal(clipId) {
  if (!opusPresetsCache.length || !brandKitsCache.length) {
    await loadOpusPresetsAndBrandKits();
  }
  let clip = clipsCache.find((c) => c.id === clipId);
  if (!clip) {
    try {
      const res = await fetch(`/api/clips/${clipId}`);
      if (res.ok) { clip = await res.json(); clipsCache.push(clip); }
    } catch (e) { /* ignora */ }
  }
  if (!clip) return;

  const ed = clip.editor_config || {};
  document.getElementById('editClipId').value = clip.id;
  setText('editClipModalTitle', `Editar clipe #${clip.id} (OpusClip Studio)`);
  document.getElementById('editClipTitle').value = clip.title || '';
  document.getElementById('editClipHook').value = clip.hook || '';
  document.getElementById('editClipHeadline').value = ed.headline_text || '';
  document.getElementById('editClipStart').value = clip.start;
  document.getElementById('editClipEnd').value = clip.end;
  document.getElementById('editClipCrop').value = clip.crop_mode || 'split_screen';

  const presetSel = document.getElementById('editClipCaptionPreset');
  if (presetSel) {
    presetSel.innerHTML = '<option value="">Padrão do estúdio</option>' +
      opusPresetsCache.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} (${esc(p.source)})</option>`).join('');
    presetSel.value = ed.caption_preset || '';
  }

  const bkSel = document.getElementById('editClipBrandKit');
  if (bkSel) {
    bkSel.innerHTML = '<option value="">Padrão do estúdio</option>' +
      brandKitsCache.map((k) => `<option value="${k.id}">${esc(k.name)}</option>`).join('');
    bkSel.value = ed.brand_kit_id ? String(ed.brand_kit_id) : '';
  }

  document.getElementById('editClipMotion').value = ed.motion_type || 'none';
  document.getElementById('editClipTransition').value = ed.transition_type || 'hard-cut';
  document.getElementById('editClipProgressBar').checked = Boolean(ed.progress_bar);
  document.getElementById('editClipRemoveSilences').checked = Boolean(ed.remove_silences);
  document.getElementById('editClipAutoColor').checked = Boolean(ed.auto_color && ed.auto_color.enabled);
  if (document.getElementById('editClipHookEnabled')) document.getElementById('editClipHookEnabled').checked = Boolean(ed.hook_enabled);
  if (document.getElementById('editClipAutoDetectCam')) document.getElementById('editClipAutoDetectCam').checked = Boolean(ed.auto_detect_cam);
  if (document.getElementById('editClipBgmDucking')) document.getElementById('editClipBgmDucking').checked = ed.bgm_ducking !== false;
  if (document.getElementById('editClipBgmVolume')) document.getElementById('editClipBgmVolume').value = ed.bgm_volume ?? 0.18;
  const bgmSel = document.getElementById('editClipBgmFile');
  if (bgmSel) {
    try {
      const bgmRes = await fetch('/api/bgm');
      const tracks = bgmRes.ok ? await bgmRes.json() : [];
      bgmSel.innerHTML = '<option value="">Sem música de fundo</option>' +
        tracks.map((t) => `<option value="${esc(t.filename)}">${esc(t.filename)} (${t.size_kb} KB)</option>`).join('');
      bgmSel.value = ed.bgm_file || '';
    } catch (e) { /* ignora */ }
  }
  if (document.getElementById('editClipSeoDesc')) document.getElementById('editClipSeoDesc').value = ed.seo_description || '';
  if (document.getElementById('editClipSeoTags')) document.getElementById('editClipSeoTags').value = ed.seo_tags || '';
  if (document.getElementById('editClipSeoHashtags')) document.getElementById('editClipSeoHashtags').value = ed.seo_hashtags || '';
  setText('editClipEmojisStatus', '');

  openModal('editClipModal');
}

async function generateSeoForEditClip() {
  const clipId = Number(document.getElementById('editClipId').value);
  if (!clipId) return;
  const btn = document.getElementById('btnGenSeoModal');
  if (btn) { btn.disabled = true; btn.textContent = 'Gerando SEO...'; }
  try {
    const res = await fetch(`/api/clips/${clipId}/generate-seo`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao gerar SEO.', 'error');
      return;
    }
    const seo = data.seo || {};
    if (seo.title) document.getElementById('editClipTitle').value = seo.title;
    if (document.getElementById('editClipSeoDesc')) document.getElementById('editClipSeoDesc').value = seo.description || '';
    if (document.getElementById('editClipSeoTags')) document.getElementById('editClipSeoTags').value = seo.tags_csv || '';
    if (document.getElementById('editClipSeoHashtags')) document.getElementById('editClipSeoHashtags').value = seo.hashtags_str || '';
    showToast('Título, descrição, tags e hashtags otimizados gerados!', 'success');
    loadClips();
  } catch (e) {
    showToast('Erro de rede ao gerar SEO.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Gerar Título, Descrição & Tags'; }
  }
}

async function generateAiEmojisForEditClip() {
  const clipId = Number(document.getElementById('editClipId').value);
  if (!clipId) return;
  setText('editClipEmojisStatus', 'Gerando emojis...');
  try {
    const res = await fetch(`/api/clips/${clipId}/ai-emojis`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      setText('editClipEmojisStatus', data.detail || 'Erro');
      return;
    }
    setText('editClipEmojisStatus', `${data.emojis_added} emojis injetados!`);
    showToast(`${data.emojis_added} AI Emojis aplicados às legendas do clipe #${clipId}!`, 'success');
  } catch (e) {
    setText('editClipEmojisStatus', 'Erro de rede');
  }
}

async function saveClipEdits(renderAfter) {
  const clipId = Number(document.getElementById('editClipId').value);
  const bkVal = document.getElementById('editClipBrandKit')?.value || '';
  const autoColorOn = Boolean(document.getElementById('editClipAutoColor')?.checked);
  const payload = {
    title: document.getElementById('editClipTitle').value.trim(),
    hook_text: document.getElementById('editClipHook').value.trim(),
    start_time: parseFloat(document.getElementById('editClipStart').value),
    end_time: parseFloat(document.getElementById('editClipEnd').value),
    crop_mode: document.getElementById('editClipCrop').value,
    editor_config: {
      headline_text: document.getElementById('editClipHeadline')?.value.trim() || '',
      caption_preset: document.getElementById('editClipCaptionPreset')?.value || '',
      brand_kit_id: bkVal ? Number(bkVal) : null,
      motion_type: document.getElementById('editClipMotion')?.value || 'none',
      transition_type: document.getElementById('editClipTransition')?.value || 'hard-cut',
      progress_bar: Boolean(document.getElementById('editClipProgressBar')?.checked),
      remove_silences: Boolean(document.getElementById('editClipRemoveSilences')?.checked),
      auto_color: autoColorOn ? { enabled: true, sharpen: 0.5, denoise: 0.3, deblock: true, glow: false } : { enabled: false },
      hook_enabled: Boolean(document.getElementById('editClipHookEnabled')?.checked),
      auto_detect_cam: Boolean(document.getElementById('editClipAutoDetectCam')?.checked),
      bgm_file: document.getElementById('editClipBgmFile')?.value || '',
      bgm_volume: parseFloat(document.getElementById('editClipBgmVolume')?.value || '0.18') || 0.18,
      bgm_ducking: Boolean(document.getElementById('editClipBgmDucking')?.checked),
      seo_description: document.getElementById('editClipSeoDesc')?.value.trim() || '',
      seo_tags: document.getElementById('editClipSeoTags')?.value.trim() || '',
      seo_hashtags: document.getElementById('editClipSeoHashtags')?.value.trim() || '',
    },
  };
  try {
    const res = await fetch(`/api/clips/${clipId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar alterações.', 'error');
      return;
    }
    closeModal('editClipModal');
    showToast('Alterações OpusClip & SEO do clipe salvas!', 'success');
    await loadClips();
    if (renderAfter) {
      await renderClip(clipId);
    }
  } catch (e) {
    showToast('Erro de rede ao salvar clipe.', 'error');
  }
}

async function deleteClip(clipId) {
  if (!confirm(`Excluir permanentemente o clipe #${clipId} e seu arquivo MP4?`)) return;
  try {
    const res = await fetch(`/api/clips/${clipId}`, { method: 'DELETE' });
    if (!res.ok) {
      showToast('Erro ao excluir clipe.', 'error');
      return;
    }
    showToast(`Clipe #${clipId} removido.`, 'success');
    loadClips();
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao excluir clipe.', 'error');
  }
}

async function openExportPack(clipId) {
  try {
    const res = await fetch(`/api/clips/${clipId}/export-pack`);
    const data = await res.json();
    if (document.getElementById('exportTitleText')) document.getElementById('exportTitleText').value = data.title || '';
    document.getElementById('exportCaptionText').value = data.caption || '';
    if (document.getElementById('exportTagsText')) document.getElementById('exportTagsText').value = data.tags_csv || '';
    const dl = document.getElementById('exportDownloadLink');
    if (data.download_url) {
      dl.href = data.download_url;
      dl.setAttribute('download', `short_${clipId}.mp4`);
      dl.style.display = 'inline-flex';
    } else {
      dl.style.display = 'none';
    }
    openModal('exportPackModal');
  } catch (e) {
    showToast('Erro ao gerar pacote de exportação.', 'error');
  }
}

async function copyFieldText(elId, okMsg) {
  const txt = document.getElementById(elId)?.value || '';
  try {
    await navigator.clipboard.writeText(txt);
    showToast(okMsg || 'Copiado!', 'success');
  } catch {
    showToast('Selecione e copie o texto manualmente.', 'info');
  }
}

async function copyExportCaption() {
  return copyFieldText('exportCaptionText', 'Descrição e hashtags copiadas!');
}

/* ==========================================
   VIEW 2: QUEUE & VIDEOS
   ========================================== */

let videoSearchTimer = null;
function debouncedLoadVideos() {
  clearTimeout(videoSearchTimer);
  videoSearchTimer = setTimeout(loadVideosTable, 250);
}

async function populateQueueChannelFilter() {
  const sel = document.getElementById('queueChannelFilter');
  if (!sel || sel.options.length > 1) return;
  try {
    const res = await fetch('/api/channels');
    const channels = await res.json();
    sel.innerHTML = '<option value="">Todos os canais</option>' +
      channels.map((c) => `<option value="${esc(c.channel_id)}">${esc(c.name)} (${c.video_count})</option>`).join('');
  } catch (e) {
    console.error(e);
  }
}

async function loadVideosTable() {
  try {
    await populateQueueChannelFilter();
    const st = document.getElementById('queueStatusFilter')?.value || '';
    const ch = document.getElementById('queueChannelFilter')?.value || '';
    const q = (document.getElementById('queueSearchInput')?.value || '').trim();
    const params = new URLSearchParams({ limit: '250' });
    if (st) params.set('status', st);
    if (ch) params.set('channel_id', ch);
    if (q) params.set('q', q);

    const res = await fetch(`/api/videos?${params.toString()}`);
    const videos = await res.json();
    const tbody = document.getElementById('videosTableBody');
    if (!tbody) return;

    if (!videos.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:32px; color:var(--text-muted);">Nenhum vídeo encontrado neste filtro.</td></tr>';
      return;
    }

    tbody.innerHTML = videos.map((v) => {
      const badgeCls = v.status === 'processed' ? 'badge-ok'
        : v.status === 'error' ? 'badge-err'
        : v.status === 'downloading' || v.status === 'transcribing' ? 'badge-info'
        : '';
      return `
        <tr>
          <td>
            <div class="video-cell-title" title="${esc(v.title)}">
              <a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title)}</a>
            </div>
            <div class="video-cell-sub">
              ${esc(v.channel_name)} · <span class="input-mono">${esc(v.id)}</span>
              ${v.error_message ? `<span style="color:#f27478;"> · ${esc(v.error_message.slice(0, 90))}</span>` : ''}
            </div>
          </td>
          <td class="input-mono">${v.duration > 0 ? fmtSec(v.duration) : '—'}</td>
          <td class="input-mono">${esc(v.clips_count)}</td>
          <td><span class="badge ${badgeCls}">${esc(v.status)}</span></td>
          <td>
            <button class="btn btn-sm" onclick="processVideoNow('${esc(v.id)}')">Processar</button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    console.error(e);
  }
}

async function processVideoNow(videoId) {
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(videoId)}/process`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao iniciar processamento.', 'error');
      return;
    }
    showToast(data.message || 'Processamento iniciado!', 'success');
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao processar vídeo.', 'error');
  }
}

/* ==========================================
   VIEW 3: WEEKLY CALENDAR (PT-BR)
   ========================================== */

async function loadCalendar() {
  try {
    const sel = document.getElementById('calendarChannelSelect');
    const chRes = await fetch('/api/destination-channels');
    const destChannels = await chRes.json();

    if (sel) {
      const prev = sel.value;
      sel.innerHTML = destChannels.length
        ? destChannels.map((ch) => `<option value="${ch.id}">${esc(ch.name)} (${ch.weight ?? 0}%)</option>`).join('')
        : '<option value="">Nenhum canal de destino</option>';
      if (prev && destChannels.some((c) => String(c.id) === prev)) {
        sel.value = prev;
      }
    }

    const selectedId = sel && sel.value ? `?channel_id=${sel.value}` : '';
    const res = await fetch(`/api/scheduler/matrix${selectedId}`);
    const data = await res.json();
    const container = document.getElementById('calendarGrid');
    if (!container) return;

    if (!data.matrix) {
      container.innerHTML = '<div class="empty-state" style="grid-column:1/-1;"><h3>Nenhum canal de destino configurado</h3><p>Conecte uma conta YouTube na aba Contas YouTube para ativar a matriz semanal.</p></div>';
      return;
    }

    container.innerHTML = data.matrix.map((day) => `
      <div class="cal-day ${day.is_today ? 'today' : ''}">
        <div class="cal-day-head">
          <div class="cal-day-date">${esc(day.date)}</div>
          <div class="cal-day-name">${esc(day.day_name)}</div>
        </div>
        <div class="cal-slots">
          ${day.slots.map((s) => {
            const isExternal = Boolean(s.is_external || (s.is_filled && !s.post_id));
            const stLabel = s.is_simulated ? 'SIMULADO'
              : isExternal ? 'YT EXTERNO'
              : s.status === 'scheduled_on_youtube' ? 'NO YOUTUBE'
              : s.status === 'awaiting_oauth' ? 'SEM OAUTH'
              : s.status === 'pending' ? 'AGENDADO'
              : s.status === 'failed' ? 'ERRO'
              : 'VAGO';
            const movable = s.is_filled && !isExternal && (EDITABLE_POST_STATUSES.includes(s.status) || s.is_simulated);
            const stCls = s.status === 'scheduled_on_youtube' ? 'badge-ok'
              : s.status === 'awaiting_oauth' ? 'badge-warn'
              : s.status === 'failed' ? 'badge-err'
              : s.is_filled ? 'badge-info' : '';
            const safeYtUrl = s.youtube_url ? esc(s.youtube_url) : '';
            const clickAttr = !s.is_filled ? ''
              : isExternal
                ? (safeYtUrl
                    ? `onclick="window.open('${safeYtUrl}', '_blank', 'noopener')" title="Vídeo agendado diretamente no YouTube — clique para abrir"`
                    : `onclick="showToast('Vídeo agendado diretamente no YouTube Studio.', 'info')" title="Vídeo agendado diretamente no YouTube"`)
                : `data-post-id="${Number(s.post_id)}" onclick="openEditPostModal(${Number(s.post_id)})" title="Clique para editar${movable ? ', arraste para mover' : ''}"`;

            return `
              <div class="cal-slot ${s.is_filled ? 'filled' : ''} ${movable ? 'draggable' : ''}" data-slot-dt="${esc(s.datetime)}"
                   ${clickAttr}
                   ${movable ? 'data-draggable="true"' : ''}>
                <div class="cal-slot-top">
                  <span>${esc(s.is_filled && s.scheduled_for ? s.scheduled_for.slice(11, 16) : s.time)}</span>
                  <span class="badge ${stCls}" style="font-size:9.5px; padding:1px 5px;">${stLabel}</span>
                </div>
                ${s.is_filled && s.clip ? `
                  <div class="cal-slot-title" title="${esc(s.clip.title)}">${esc(s.clip.title)}</div>
                  <div class="cal-slot-foot">
                    ${isExternal
                      ? `<span>YouTube Studio</span>${safeYtUrl ? `<a class="btn btn-ghost btn-sm" style="height:20px; padding:0 5px; font-size:10.5px; text-decoration:none;" href="${safeYtUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Abrir ↗</a>` : ''}`
                      : `<span>${esc(s.clip.duration)}s · S${esc(s.clip.score)}</span>
                         <button class="btn btn-ghost btn-sm" style="height:20px; padding:0 5px; font-size:10.5px;" onclick="event.stopPropagation(); removeScheduledPost(${Number(s.post_id)})">Remover</button>`
                    }
                  </div>
                ` : '<div style="font-size:11px; color:var(--text-muted);">Horário disponível</div>'}
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `).join('');
    calendarDestChannels = destChannels;
    wireCalendarDragDrop();
  } catch (e) {
    console.error(e);
  }
}

async function autoFillSchedule() {
  try {
    const res = await fetch('/api/scheduler/auto-fill', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Grade atualizada.', 'success');
    loadCalendar();
    fetchStatus();
  } catch (e) {
    showToast('Erro ao preencher grade.', 'error');
  }
}

async function removeScheduledPost(postId) {
  try {
    const res = await fetch(`/api/scheduler/posts/${postId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Slot liberado com sucesso.', 'success');
      loadCalendar();
      fetchStatus();
    }
  } catch (e) {
    showToast('Erro ao remover agendamento.', 'error');
  }
}

/* ==========================================
   VIEW 4: TIMELINE & DEDUP FREE WINDOWS
   ========================================== */

async function loadVideosForTimeline() {
  try {
    const res = await fetch('/api/videos?limit=100');
    const videos = await res.json();
    const sel = document.getElementById('videoSelectTimeline');
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = videos.map((v) => `
      <option value="${esc(v.id)}">${esc(v.title.slice(0, 52))} (${esc(v.channel_name)}) [${v.clips_count} cortes]</option>
    `).join('');
    if (prev && videos.some((v) => v.id === prev)) {
      sel.value = prev;
    }
    if (videos.length) {
      loadVideoTimeline();
    }
  } catch (e) {
    console.error(e);
  }
}

async function loadVideoTimeline() {
  const vidId = document.getElementById('videoSelectTimeline')?.value;
  if (!vidId) return;

  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(vidId)}/timeline`);
    const data = await res.json();
    const total = Number(data.duration) || 0;
    const used = data.used_segments || [];
    const free = data.available_unused_windows || [];

    setText('timelineDuration', total > 0 ? fmtSec(total) : 'Duração pendente');
    setText('timelineSummaryText', `${used.length} cortes usados · ${free.length} janelas livres`);

    const bar = document.getElementById('timelineBar');
    if (bar) {
      bar.innerHTML = '';
      if (total > 0) {
        used.forEach((seg) => {
          const left = Math.max(0, (seg.start_time / total) * 100);
          const width = Math.max(1.2, ((seg.end_time - seg.start_time) / total) * 100);
          const el = document.createElement('div');
          el.className = 'timeline-used-block';
          el.style.left = `${left}%`;
          el.style.width = `${Math.min(100 - left, width)}%`;
          el.title = `Corte #${seg.id}: ${seg.title} (${fmtSec(seg.start_time)} - ${fmtSec(seg.end_time)})`;
          bar.appendChild(el);
        });
      }
    }

    const freeEl = document.getElementById('timelineFreeWindows');
    if (freeEl) {
      if (!free.length) {
        freeEl.innerHTML = '<div style="font-size:12px; color:var(--text-muted); grid-column:1/-1;">Duração ainda não detectada (processe ou baixe o vídeo) ou vídeo 100% utilizado.</div>';
      } else {
        freeEl.innerHTML = free.map((w) => `
          <div class="window-card">
            <div>
              <div class="input-mono" style="font-size:12px; color:#4cc38a;">${fmtSec(w.start)} — ${fmtSec(w.end)}</div>
              <div style="font-size:11px; color:var(--text-muted);">${Math.floor(w.duration)}s livres</div>
            </div>
            <button class="btn btn-sm" onclick="openManualClipModal(${w.start}, ${Math.min(w.end, w.start + 40)})">Usar</button>
          </div>
        `).join('');
      }
    }

    const usedEl = document.getElementById('timelineUsedList');
    if (usedEl) {
      if (!used.length) {
        usedEl.innerHTML = '<div style="font-size:12px; color:var(--text-muted); grid-column:1/-1;">Nenhum corte registrado ainda para este vídeo.</div>';
      } else {
        usedEl.innerHTML = used.map((u) => `
          <div class="window-card">
            <div style="min-width:0;">
              <div style="font-size:12px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${esc(u.title)}</div>
              <div class="input-mono" style="font-size:11px; color:var(--text-muted);">${fmtSec(u.start_time)} — ${fmtSec(u.end_time)}</div>
            </div>
            <span class="badge">${esc(u.status)}</span>
          </div>
        `).join('');
      }
    }
  } catch (e) {
    console.error(e);
  }
}

function openManualClipModal(prefStart = 10, prefEnd = 45) {
  document.getElementById('manualClipStart').value = Math.round(prefStart);
  document.getElementById('manualClipEnd').value = Math.round(prefEnd);
  document.getElementById('manualClipTitle').value = '';
  openModal('manualClipModal');
}

async function submitManualClip() {
  const video_id = document.getElementById('videoSelectTimeline')?.value;
  if (!video_id) {
    showToast('Selecione um vídeo primeiro.', 'error');
    return;
  }
  const payload = {
    video_id,
    title: document.getElementById('manualClipTitle').value.trim() || 'Corte Manual',
    start_time: parseFloat(document.getElementById('manualClipStart').value),
    end_time: parseFloat(document.getElementById('manualClipEnd').value),
    crop_mode: document.getElementById('manualClipCrop').value,
    suggested_by: 'manual_editor',
  };
  try {
    const res = await fetch('/api/clips/propose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Não foi possível registrar o corte.', 'error');
      return;
    }
    closeModal('manualClipModal');
    showToast('Corte manual salvo com proteção anti-duplicidade!', 'success');
    loadVideoTimeline();
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao salvar corte.', 'error');
  }
}

/* ==========================================
   VIEW 5 & 8: PRESETS + SYSTEM SETTINGS
   ========================================== */

async function loadSettingsIntoUI() {
  try {
    const res = await fetch('/api/settings');
    const s = await res.json();

    // Settings tab
    if (document.getElementById('setAiProvider')) {
      document.getElementById('setAiProvider').value = s.AI_PROVIDER || 'offline';
      document.getElementById('setGeminiKey').value = s.GEMINI_API_KEY || '';
      document.getElementById('setGroqKey').value = s.GROQ_API_KEY || '';
      document.getElementById('setOpenaiKey').value = s.OPENAI_API_KEY || '';
      document.getElementById('setClaudeKey').value = s.ANTHROPIC_API_KEY || '';
      document.getElementById('setOllamaUrl').value = s.OLLAMA_URL || 'http://localhost:11434';
      document.getElementById('setOllamaModel').value = s.OLLAMA_MODEL || 'llama3.1';
      document.getElementById('setMinDur').value = s.MIN_CLIP_DURATION || '20';
      document.getElementById('setMaxDur').value = s.MAX_CLIP_DURATION || '58';
      document.getElementById('setTimezone').value = s.TIMEZONE || 'America/Sao_Paulo';
      document.getElementById('setWhisperModel').value = s.WHISPER_MODEL || 'base';
      document.getElementById('setCookiesBrowser').value = s.YT_COOKIES_BROWSER || '';
      document.getElementById('setAutoSchedule').checked = (s.AUTO_SCHEDULE || 'true') === 'true';
    }

    // Presets tab
    const p = s.STUDIO_PRESET || {};
    if (document.getElementById('presetCropMode')) {
      document.getElementById('presetCropMode').value = s.CROP_MODE || 'split_screen';
      document.getElementById('presetStyleMode').value = p.style_mode || 'word_highlight';
      document.getElementById('presetFontSize').value = p.font_size ?? 74;
      document.getElementById('presetMarginV').value = p.margin_v ?? 460;
      document.getElementById('presetWordsPerLine').value = p.words_per_line ?? 3;
      document.getElementById('presetPrimaryColor').value = p.primary_color || '#FFFFFF';
      document.getElementById('presetHighlightColor').value = p.highlight_color || '#FACC15';
      document.getElementById('presetOutlineColor').value = p.outline_color || '#000000';
      document.getElementById('presetWatermark').value = p.watermark_text || '';
      document.getElementById('presetFontName').value = p.font_name || 'auto';
      document.getElementById('presetEnableSubs').checked = p.enable_subtitles !== false;
      document.getElementById('presetUppercase').checked = p.uppercase !== false;
      document.getElementById('presetEnableVfx').checked = p.enable_vfx !== false;
      if (document.getElementById('presetLanguage')) document.getElementById('presetLanguage').value = p.caption_language || 'auto';
      if (document.getElementById('presetOutlineWidth')) document.getElementById('presetOutlineWidth').value = p.outline_width ?? 6;
      if (p.preset_id) selectedPresetId = p.preset_id;
      updatePresetPreview();
      loadTranscriptionStatus();
    }
  } catch (e) {
    console.error(e);
  }
}

function updatePresetPreview() {
  const cropMode = document.getElementById('presetCropMode').value;
  const styleMode = document.getElementById('presetStyleMode').value;
  const fontSize = Number(document.getElementById('presetFontSize').value);
  const marginV = Number(document.getElementById('presetMarginV').value);
  const primary = document.getElementById('presetPrimaryColor').value;
  const highlight = document.getElementById('presetHighlightColor').value;
  const outline = document.getElementById('presetOutlineColor').value;
  const wm = document.getElementById('presetWatermark').value.trim();
  const enableSubs = document.getElementById('presetEnableSubs').checked;
  const uppercase = document.getElementById('presetUppercase').checked;

  setText('fontSizeVal', fontSize);
  setText('marginVVal', marginV);
  setText('previewWatermark', wm);

  const topZ = document.getElementById('previewTopZone');
  const botZ = document.getElementById('previewBottomZone');
  if (cropMode === 'split_screen') {
    topZ.style.display = 'flex';
    topZ.textContent = 'FACECAM (TOPO 50%)';
    botZ.textContent = 'GAMEPLAY (BASE 50%)';
  } else if (cropMode === 'blur_bg') {
    topZ.style.display = 'none';
    botZ.textContent = 'VÍDEO 16:9 + FUNDO BLUR';
  } else {
    topZ.style.display = 'none';
    botZ.textContent = 'CENTER CROP 9:16 TELA CHEIA';
  }

  const sub = document.getElementById('previewSubtitle');
  if (!sub) return;
  const outlineW = Number(document.getElementById('presetOutlineWidth')?.value ?? 6);
  setText('outlineWidthVal', outlineW);
  const frame = document.getElementById('phoneFramePreview');
  // Escala real do preview: altura do celular na tela / 1920px do vídeo
  const scale = frame && frame.clientHeight ? frame.clientHeight / 1920 : 0.25;
  const big = styleMode === 'single_word' ? 1.18 : 1;
  sub.style.display = enableSubs ? 'block' : 'none';
  sub.style.bottom = `${Math.round(marginV * scale)}px`;
  sub.style.fontSize = `${Math.max(11, Math.round(fontSize * big * scale * 1.08))}px`;
  sub.style.color = primary;
  sub.style.textTransform = uppercase ? 'uppercase' : 'none';
  const fontName = document.getElementById('presetFontName')?.value || 'auto';
  sub.style.fontFamily = fontName === 'auto' ? '' : `"${fontName}", var(--font-sans, sans-serif)`;
  sub.style.background = styleMode === 'box_caption' ? 'rgba(0,0,0,0.72)' : 'transparent';
  sub.style.padding = styleMode === 'box_caption' ? '6px 10px' : '0';
  sub.style.borderRadius = styleMode === 'box_caption' ? '6px' : '0';
  const ow = Math.max(0, Math.round(outlineW * scale * 1.3 * 10) / 10);
  sub.style.textShadow = styleMode === 'clean_minimal' || ow === 0
    ? `0 2px 4px ${outline}`
    : `-${ow}px -${ow}px 0 ${outline}, ${ow}px -${ow}px 0 ${outline}, -${ow}px ${ow}px 0 ${outline}, ${ow}px ${ow}px 0 ${outline}, 0 ${ow + 1}px ${ow * 2 + 2}px rgba(0,0,0,0.85)`;

  captionPreviewState.mode = styleMode;
  captionPreviewState.primary = primary;
  captionPreviewState.highlight = highlight;
  captionPreviewState.wordsPerLine = Math.max(1, Number(document.getElementById('presetWordsPerLine').value) || 3);
  captionPreviewState.language = document.getElementById('presetLanguage')?.value || 'auto';
  startCaptionPreviewLoop();
}

/* Prévia animada das legendas: simula a fala palavra a palavra no celular do preview */
var CAPTION_DEMO = {
  pt: 'olha esse clutch absurdo no finalzinho da partida mano',
  en: 'look at this insane clutch right at the end of the match',
};
var captionPreviewState = { idx: 0, timer: null, mode: 'word_highlight', primary: '#FFFFFF', highlight: '#FACC15', wordsPerLine: 3, language: 'auto' };

function renderCaptionPreviewFrame() {
  const sub = document.getElementById('previewSubtitle');
  if (!sub) return;
  const st = captionPreviewState;
  const words = (st.language === 'en' ? CAPTION_DEMO.en : CAPTION_DEMO.pt).split(' ');
  const i = st.idx % words.length;
  const per = st.mode === 'single_word' ? 1 : st.wordsPerLine;
  const chunkStart = Math.floor(i / per) * per;
  const chunk = words.slice(chunkStart, chunkStart + per);
  const activeInChunk = i - chunkStart;
  const pop = ['karaoke_pop', 'word_reveal', 'single_word'].includes(st.mode);
  const highlightOn = st.mode !== 'clean_minimal';
  const visible = st.mode === 'word_reveal' ? chunk.slice(0, activeInChunk + 1) : chunk;

  sub.replaceChildren(...visible.map((w, j) => {
    const span = document.createElement('span');
    const isActive = j === activeInChunk;
    span.className = 'cap-word' + (isActive ? ' is-active' : '') + (isActive && pop ? ' pop' : '');
    span.textContent = w;
    span.style.color = isActive && highlightOn ? st.highlight : st.primary;
    return span;
  }));
  st.idx = (i + 1) % words.length;
}

function startCaptionPreviewLoop() {
  renderCaptionPreviewFrame();
  if (captionPreviewState.timer) return;
  captionPreviewState.timer = setInterval(() => {
    const view = document.getElementById('view-presets');
    if (view && !view.classList.contains('active')) return;
    renderCaptionPreviewFrame();
  }, 380);
}

window.addEventListener('resize', () => {
  if (document.getElementById('previewSubtitle')) updatePresetPreview();
});

async function loadTranscriptionStatus() {
  const textEl = document.getElementById('transcribeStatusText');
  const dot = document.getElementById('transcribeDot');
  if (!textEl || !dot) return;
  try {
    const res = await fetch('/api/transcription/status');
    const st = await res.json();
    const engines = [];
    if (st.groq) engines.push('Groq Whisper');
    if (st.openai) engines.push('OpenAI Whisper');
    if (st.faster_whisper) engines.push('Whisper local');
    if (st.any) {
      dot.className = 'transcribe-dot ok';
      textEl.textContent = `Transcrição automática ativa (${engines.join(' + ')}). Todo clipe novo recebe legenda sincronizada sozinho.`;
    } else {
      dot.className = 'transcribe-dot warn';
      textEl.textContent = 'Sem motor de transcrição: só vídeos com legenda do YouTube terão legenda. Coloque a chave grátis da Groq (GROQ_API_KEY) em Configurações.';
    }
  } catch (e) {
    dot.className = 'transcribe-dot warn';
    textEl.textContent = 'Não consegui verificar o motor de transcrição.';
  }
}

async function saveStudioPreset() {
  const payload = {
    CROP_MODE: document.getElementById('presetCropMode').value,
    STUDIO_PRESET: {
      font_name: document.getElementById('presetFontName').value,
      font_size: Number(document.getElementById('presetFontSize').value),
      primary_color: document.getElementById('presetPrimaryColor').value,
      highlight_color: document.getElementById('presetHighlightColor').value,
      outline_color: document.getElementById('presetOutlineColor').value,
      outline_width: Number(document.getElementById('presetOutlineWidth')?.value ?? 6),
      shadow_depth: 3,
      caption_language: document.getElementById('presetLanguage')?.value || 'auto',
      preset_id: selectedPresetId || undefined,
      margin_v: Number(document.getElementById('presetMarginV').value),
      words_per_line: Number(document.getElementById('presetWordsPerLine').value),
      uppercase: document.getElementById('presetUppercase').checked,
      style_mode: document.getElementById('presetStyleMode').value,
      watermark_text: document.getElementById('presetWatermark').value.trim(),
      enable_vfx: document.getElementById('presetEnableVfx').checked,
      enable_subtitles: document.getElementById('presetEnableSubs').checked,
    },
  };
  try {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar preset.', 'error');
      return;
    }
    showToast('Preset visual salvo! As próximas renderizações usarão este estilo.', 'success');
  } catch (e) {
    showToast('Erro de rede ao salvar preset.', 'error');
  }
}

async function saveSystemSettings() {
  const payload = {
    AI_PROVIDER: document.getElementById('setAiProvider').value,
    GEMINI_API_KEY: document.getElementById('setGeminiKey').value,
    GROQ_API_KEY: document.getElementById('setGroqKey').value,
    OPENAI_API_KEY: document.getElementById('setOpenaiKey').value,
    ANTHROPIC_API_KEY: document.getElementById('setClaudeKey').value,
    OLLAMA_URL: document.getElementById('setOllamaUrl').value,
    OLLAMA_MODEL: document.getElementById('setOllamaModel').value,
    MIN_CLIP_DURATION: document.getElementById('setMinDur').value,
    MAX_CLIP_DURATION: document.getElementById('setMaxDur').value,
    TIMEZONE: document.getElementById('setTimezone').value,
    WHISPER_MODEL: document.getElementById('setWhisperModel').value,
    YT_COOKIES_BROWSER: document.getElementById('setCookiesBrowser').value,
    AUTO_SCHEDULE: document.getElementById('setAutoSchedule').checked ? 'true' : 'false',
  };
  try {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar configurações.', 'error');
      return;
    }
    showToast(data.message || 'Configurações salvas!', 'success');
    loadSettingsIntoUI();
  } catch (e) {
    showToast('Erro de rede ao salvar configurações.', 'error');
  }
}

async function loadAIBusEvents() {
  try {
    const res = await fetch('/api/ai-bus/events');
    const events = await res.json();
    const container = document.getElementById('aiBusLogs');
    if (!container) return;
    if (!events.length) {
      container.innerHTML = '<div class="log-line"><span class="log-time">--:--:--</span><span class="log-info">Sem eventos no barramento Multi-IA.</span></div>';
      return;
    }
    container.innerHTML = events.map((e) => `
      <div class="log-line">
        <span class="log-time">${esc((e.created_at || '').split('T')[1]?.slice(0, 8) || '')}</span>
        <span class="log-info"><strong>[${esc(e.sender)} → ${esc(e.target)}]</strong> ${esc(e.type)} ${esc(JSON.stringify(e.payload).slice(0, 120))}</span>
      </div>
    `).join('');
  } catch (e) {
    console.error(e);
  }
}

/* ==========================================
   VIEW 6: SOURCE CHANNELS
   ========================================== */

async function loadChannels() {
  try {
    const res = await fetch('/api/channels');
    const channels = await res.json();
    const container = document.getElementById('channelsList');
    if (!container) return;

    renderPriorityList(channels);
    sourceChannelsCache = channels;
    container.innerHTML = channels.map((c) => `
      <article class="channel-card ${c.is_active ? '' : 'paused'}">
        <div class="channel-card-head">
          <div>
            <div style="font-weight:600; font-size:14px;">${esc(c.name)}</div>
            <div class="input-mono" style="font-size:11.5px; color:var(--text-muted); margin-top:2px;">${esc(c.handle)}</div>
          </div>
          <span class="badge ${c.is_active ? 'badge-ok' : ''}">${c.is_active ? 'Ativo' : 'Pausado'}</span>
        </div>
        <div style="font-size:12px; color:var(--text-secondary);">
          <span class="input-mono">${esc(c.video_count)}</span> vídeos indexados
        </div>
        <div class="channel-card-foot">
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button class="btn btn-sm" onclick="backfillSingleChannel(${Number(c.id)})">+80 vídeos</button>
            <button class="btn btn-sm" onclick="toggleSourceChannel(${c.id})">${c.is_active ? 'Pausar' : 'Ativar'}</button>
            <a class="btn btn-ghost btn-sm" href="${esc(c.url)}" target="_blank" rel="noopener">Abrir</a>
          </div>
          <button class="btn btn-danger btn-sm" onclick="deleteSourceChannel(${Number(c.id)})">Remover</button>
        </div>
      </article>
    `).join('');
  } catch (e) {
    console.error(e);
  }
}

async function triggerDeepBackfill(overrideLimit) {
  const limit = overrideLimit || Number(document.getElementById('backfillLimitSelect')?.value || 100);
  showToast(`Iniciando backfill profundo (até ${limit} vídeos/canal + lives)...`, 'info');
  try {
    const res = await fetch(`/api/channels/backfill?limit_per_channel=${encodeURIComponent(limit)}&include_streams=true`, {
      method: 'POST',
    });
    const data = await res.json();
    showToast(data.message || 'Backfill iniciado!', 'success');
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao iniciar backfill.', 'error');
  }
}

async function backfillSingleChannel(id, name) {
  name = name || (sourceChannelsCache.find((c) => c.id === id) || {}).name || `#${id}`;
  showToast(`Puxando +80 vídeos históricos de ${name}...`, 'info');
  try {
    const res = await fetch(`/api/channels/${id}/backfill?limit=80&include_streams=true`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao puxar histórico do canal.', 'error');
      return;
    }
    showToast(data.message || 'Backfill iniciado!', 'success');
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao puxar histórico.', 'error');
  }
}

function openAddChannelModal() {
  document.getElementById('newChannelInput').value = '';
  openModal('addChannelModal');
}

async function submitNewChannel() {
  const raw = document.getElementById('newChannelInput').value.trim();
  const initial_videos = Number(document.getElementById('newChannelInitialVideos')?.value || 60);
  const btn = document.getElementById('btnSubmitChannel');
  if (!raw) {
    showToast('Cole pelo menos uma URL, @handle ou Channel ID.', 'error');
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Resolvendo canais...';
  try {
    const res = await fetch('/api/channels/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channels: raw, initial_videos }),
    });
    const data = await res.json();
    if (!res.ok || (!data.added_count && data.errors?.length)) {
      const errMsg = data.detail || data.errors?.[0]?.error || 'Não foi possível adicionar os canais.';
      showToast(errMsg, 'error');
      return;
    }
    closeModal('addChannelModal');
    const warn = data.errors?.length ? ` (${data.errors.length} ignorado(s))` : '';
    showToast(`${data.message}${warn}`, 'success');
    // Força recarregar dropdown de canais na fila também
    const qSel = document.getElementById('queueChannelFilter');
    if (qSel) qSel.innerHTML = '<option value="">Todos os canais</option>';
    loadChannels();
    fetchStatus();
  } catch (e) {
    showToast('Erro de rede ao adicionar canais.', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Adicionar e puxar vídeos';
  }
}

async function toggleSourceChannel(id) {
  try {
    const res = await fetch(`/api/channels/${id}/toggle`, { method: 'PATCH' });
    if (res.ok) {
      showToast('Status do canal atualizado.', 'success');
      loadChannels();
      fetchStatus();
    }
  } catch (e) {
    showToast('Erro ao alterar status do canal.', 'error');
  }
}

async function deleteSourceChannel(id, name) {
  name = name || (sourceChannelsCache.find((c) => c.id === id) || {}).name || `#${id}`;
  if (!confirm(`Remover o canal '${name}' e parar de monitorá-lo?`)) return;
  try {
    const res = await fetch(`/api/channels/${id}`, { method: 'DELETE' });
    if (res.ok) {
      showToast(`Canal '${name}' removido.`, 'success');
      loadChannels();
      fetchStatus();
    }
  } catch (e) {
    showToast('Erro ao remover canal.', 'error');
  }
}

/* ==========================================
   VIEW 7: DESTINATION CHANNELS & OAUTH
   ========================================== */

async function loadDestinations() {
  try {
    const [chRes, authRes] = await Promise.all([
      fetch('/api/destination-channels'),
      fetch('/api/auth/youtube/status'),
    ]);
    const channels = await chRes.json();
    const authInfo = await authRes.json();

    const banner = document.getElementById('oauthWarningBanner');
    if (banner) {
      banner.style.display = authInfo.client_secret_configured ? 'none' : 'flex';
    }

    const container = document.getElementById('destinationsList');
    const summaryBar = document.getElementById('weightsSummaryBar');
    if (!container) return;

    if (!channels.length) {
      if (summaryBar) summaryBar.style.display = 'none';
      container.innerHTML = '<div class="empty-state" style="grid-column:1/-1;"><h3>Nenhuma conta conectada</h3><p>Conecte sua conta do YouTube via OAuth 2.0 para publicar Shorts automaticamente.</p></div>';
      return;
    }

    if (summaryBar) summaryBar.style.display = 'block';
    container.innerHTML = channels.map((c) => `
      <article class="channel-card ${c.is_active ? '' : 'paused'}">
        <div class="channel-card-head">
          <div>
            <div style="font-weight:600; font-size:14px;">${esc(c.name)}</div>
            <div class="input-mono" style="font-size:11px; color:var(--text-muted); margin-top:2px;">
              ${esc(c.youtube_channel_id || 'Aguardando autorização OAuth')}
            </div>
          </div>
          <span class="badge ${c.has_token ? 'badge-ok' : 'badge-warn'}">
            ${c.has_token ? 'OAuth Ativo' : 'Sem Token'}
          </span>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:12px; color:var(--text-secondary);">
          <span>Meta: <strong style="color:var(--text-primary);">${esc(c.daily_post_limit)}/dia</strong></span>
          <span>Agendados: <strong class="input-mono" style="color:var(--text-primary);">${esc(c.scheduled_count)}</strong></span>
          <div style="display:flex; align-items:center; gap:6px;">
            <label for="weightInput-${c.id}" style="color:var(--text-secondary);">Peso:</label>
            <input
              data-channel-id="${c.id}"
              id="weightInput-${c.id}"
              type="number"
              min="0"
              max="100"
              value="${c.weight ?? 0}"
              oninput="updateWeightSummary()"
              class="input input-mono weight-input"
              style="width:68px; padding:4px 6px; text-align:center;"
            >
            <span class="input-mono">%</span>
          </div>
        </div>

        <div class="channel-card-foot">
          <button class="btn btn-sm" onclick="toggleDestination(${c.id})">
            ${c.is_active ? 'Pausar canal' : 'Ativar canal'}
          </button>
          <a class="btn btn-sm" href="/api/auth/youtube/login?channel_name=${encodeURIComponent(c.name)}">
            ${c.has_token ? 'Reconectar OAuth' : 'Conectar OAuth'}
          </a>
        </div>
      </article>
    `).join('');

    updateWeightSummary();
  } catch (e) {
    console.error(e);
  }
}

function updateWeightSummary() {
  const inputs = Array.from(document.querySelectorAll('.weight-input'));
  const total = inputs.reduce((acc, el) => acc + Math.max(0, Math.min(100, parseInt(el.value || '0', 10) || 0)), 0);
  const badge = document.getElementById('weightsTotalBadge');
  const hint = document.getElementById('weightsHint');
  const btn = document.getElementById('saveWeightsBtn');
  if (!badge || !btn) return;

  badge.textContent = `${total}% / 100%`;
  if (total === 100) {
    badge.className = 'badge badge-ok';
    if (hint) hint.style.display = 'none';
    btn.disabled = false;
  } else {
    badge.className = 'badge badge-err';
    if (hint) hint.style.display = 'inline';
    btn.disabled = true;
  }
}

async function saveAllWeights() {
  const inputs = Array.from(document.querySelectorAll('.weight-input'));
  const weights = {};
  for (const el of inputs) {
    weights[el.getAttribute('data-channel-id')] = Math.max(0, Math.min(100, parseInt(el.value || '0', 10) || 0));
  }
  try {
    const res = await fetch('/api/destination-channels/weights', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ weights }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar pesos.', 'error');
      return;
    }
    showToast('Distribuição de pesos salva (100%).', 'success');
    loadDestinations();
  } catch (e) {
    showToast('Erro de rede ao salvar pesos.', 'error');
  }
}

async function toggleDestination(channelId) {
  try {
    const res = await fetch(`/api/destination-channels/${channelId}/toggle`, { method: 'PATCH' });
    if (res.ok) {
      showToast('Status da conta atualizado.', 'success');
      loadDestinations();
      fetchStatus();
    }
  } catch (e) {
    showToast('Erro ao alterar status da conta.', 'error');
  }
}

/* ==========================================
   BOOT
   ========================================== */

document.addEventListener('DOMContentLoaded', () => {
  fetchStatus();
  loadClips();
  startRealtimeStream();
});

/* ==========================================
   ARRASTAR E SOLTAR (Pointer Events: mouse e toque)
   Ranks de streamers e de clipes + mover agendados entre horários, dias e canais.
   No toque, arraste pela alça (⋮⋮) dos ranks para a página continuar rolando.
   ========================================== */

const EDITABLE_POST_STATUSES = ['pending', 'failed', 'awaiting_oauth'];
let sourceChannelsCache = [];
let calendarDestChannels = [];
let dndActive = false;
let suppressNextClick = false;

document.addEventListener('click', (e) => {
  if (suppressNextClick) { e.stopPropagation(); e.preventDefault(); }
}, true);

function startPointerDrag(el, ev, { onMove, onDrop, onStart, onEnd } = {}) {
  const startX = ev.clientX;
  const startY = ev.clientY;
  const rect = el.getBoundingClientRect();
  const pid = ev.pointerId;
  let ghost = null;
  let active = false;

  const begin = () => {
    active = true;
    dndActive = true;
    ghost = el.cloneNode(true);
    ghost.removeAttribute('id');
    ghost.classList.add('drag-ghost');
    Object.assign(ghost.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px` });
    document.body.appendChild(ghost);
    el.classList.add('is-drag-source');
    document.body.classList.add('is-dragging');
    if (onStart) onStart();
  };
  const move = (e) => {
    if (e.pointerId !== pid) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!active) {
      if (Math.hypot(dx, dy) < 6) return;
      begin();
    }
    e.preventDefault();
    ghost.style.left = `${rect.left + dx}px`;
    ghost.style.top = `${rect.top + dy}px`;
    if (onMove) onMove(e.clientX, e.clientY);
  };
  const up = (e) => {
    if (e.pointerId !== pid) return;
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', up);
    if (!active) return;
    ghost.remove();
    el.classList.remove('is-drag-source');
    document.body.classList.remove('is-dragging');
    dndActive = false;
    suppressNextClick = true;
    setTimeout(() => { suppressNextClick = false; }, 60);
    if (e.type === 'pointerup' && onDrop) onDrop(e.clientX, e.clientY);
    if (onEnd) onEnd();
  };
  window.addEventListener('pointermove', move, { passive: false });
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
}

function makeSortable(list, onOrderChanged) {
  list.querySelectorAll('.rank-item').forEach((li) => {
    li.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || ev.target.closest('button, a, input, select')) return;
      if (ev.pointerType !== 'mouse' && !ev.target.closest('.drag-handle')) return;
      const before = sortableOrder(list).join(',');
      startPointerDrag(li, ev, {
        onMove: (x, y) => {
          const over = document.elementFromPoint(x, y)?.closest('.rank-item');
          if (!over || over === li || over.parentElement !== list) return;
          const r = over.getBoundingClientRect();
          list.insertBefore(li, y > r.top + r.height / 2 ? over.nextSibling : over);
        },
        onDrop: () => {
          if (sortableOrder(list).join(',') !== before) onOrderChanged(sortableOrder(list));
        },
      });
    });
  });
}

function sortableOrder(list) {
  return Array.from(list.querySelectorAll('.rank-item')).map((li) => Number(li.dataset.itemId));
}

function rankPos(idx) {
  return `<span class="rank-pos ${idx === 0 ? 'top1' : idx < 3 ? 'top3' : ''}">${idx + 1}</span>`;
}
const DRAG_HANDLE = '<span class="drag-handle" title="Arraste para reordenar" aria-hidden="true">⋮⋮</span>';

async function saveOrder(url, order, statusId, okMsg, reload) {
  const status = document.getElementById(statusId);
  if (status) status.textContent = 'Salvando...';
  try {
    const res = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order }) });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Não foi possível salvar a ordem.', 'error');
      if (status) status.textContent = '';
    } else {
      if (status) status.textContent = 'Salvo';
      showToast(okMsg, 'success');
    }
  } catch (e) {
    showToast('Erro de rede ao salvar a ordem.', 'error');
    if (status) status.textContent = '';
  }
  reload();
}

// ---- Rank de prioridade dos streamers ----
function renderPriorityList(channels) {
  const list = document.getElementById('priorityList');
  if (!list) return;
  if (!channels.length) {
    list.innerHTML = '<li class="rank-empty">Nenhum canal monitorado ainda.</li>';
    return;
  }
  list.innerHTML = channels.map((c, idx) => `
    <li class="rank-item priority-item" data-item-id="${Number(c.id)}">
      ${rankPos(idx)}
      ${DRAG_HANDLE}
      <span class="rank-name">${esc(c.name)} <span class="rank-meta">${esc(c.handle)}</span></span>
      <span class="rank-meta hide-mobile">${esc(c.video_count)} vídeos${c.is_active ? '' : ' · pausado'}</span>
    </li>
  `).join('');
  makeSortable(list, (order) => saveOrder('/api/channels/priority', order, 'priorityStatus', 'Rank de prioridade salvo!', loadChannels));
}

// ---- Fila de postagem (rank de clipes) ----
async function loadClipQueue() {
  const list = document.getElementById('clipQueueList');
  if (!list) return;
  try {
    const queue = await (await fetch('/api/clips/queue')).json();
    if (!queue.length) {
      list.innerHTML = '<li class="rank-empty">Nenhum clipe na fila. Clipes aguardando render ou prontos (ainda não agendados) aparecem aqui.</li>';
      return;
    }
    list.innerHTML = queue.map((c, idx) => `
      <li class="rank-item queue-item" data-item-id="${Number(c.id)}">
        ${rankPos(idx)}
        ${DRAG_HANDLE}
        <span class="rank-name">${esc(c.title)}</span>
        <span class="rank-meta hide-mobile">${esc(c.channel_name || '')}</span>
        <span class="badge ${c.is_ready ? 'badge-ok' : ''}">${c.is_ready ? 'pronto' : 'a renderizar'}</span>
        <span class="rank-meta hide-mobile">S${esc(c.score)}</span>
      </li>
    `).join('');
    makeSortable(list, (order) => saveOrder('/api/clips/queue', order, 'queueStatus', 'Ordem da fila salva!', loadClipQueue));
  } catch (e) {
    console.error(e);
  }
}

// ---- Agenda: arrastar entre horários, dias e canais ----
function wireCalendarDragDrop() {
  const grid = document.getElementById('calendarGrid');
  const zone = document.getElementById('channelDropZone');
  const chips = document.getElementById('channelDropChips');
  const sel = document.getElementById('calendarChannelSelect');
  if (!grid || !zone || !chips) return;
  const now = new Date();
  const current = sel ? Number(sel.value) : null;
  chips.innerHTML = calendarDestChannels.filter((ch) => ch.id !== current).map((ch) => `
    <span class="channel-chip" data-drop-channel="${Number(ch.id)}">${esc(ch.name)}</span>
  `).join('') || '<span class="rank-meta">(nenhum outro canal cadastrado)</span>';

  const isFuture = (slot) => new Date(slot.dataset.slotDt) > now;
  const findTarget = (x, y) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const chip = el.closest('.channel-chip');
    if (chip) return chip;
    const slot = el.closest('#calendarGrid .cal-slot');
    return slot && isFuture(slot) ? slot : null;
  };

  grid.querySelectorAll('.cal-slot').forEach((slot) => {
    if (!isFuture(slot)) slot.classList.add('past');
    if (slot.dataset.draggable !== 'true') return;
    slot.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || ev.target.closest('button')) return;
      const postId = Number(slot.dataset.postId);
      let over = null;
      startPointerDrag(slot, ev, {
        onStart: () => zone.classList.add('show'),
        onMove: (x, y) => {
          const t = findTarget(x, y);
          const next = t && t !== slot ? t : null;
          if (next === over) return;
          if (over) over.classList.remove('drop-target');
          over = next;
          if (over) over.classList.add('drop-target');
        },
        onDrop: (x, y) => {
          const t = findTarget(x, y);
          if (!t || t === slot) return;
          if (t.dataset.dropChannel) moveScheduledPost(postId, { destination_channel_id: Number(t.dataset.dropChannel) });
          else moveScheduledPost(postId, { scheduled_for: t.dataset.slotDt });
        },
        onEnd: () => {
          if (over) over.classList.remove('drop-target');
          zone.classList.remove('show');
        },
      });
    });
  });
}

async function moveScheduledPost(postId, body) {
  try {
    const res = await fetch(`/api/scheduled-posts/${postId}/move`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Não foi possível mover o agendamento.', 'error');
    } else {
      const when = (data.scheduled_for || '').replace('T', ' ').slice(0, 16);
      showToast(data.swapped_with
        ? `Trocado de lugar com o agendamento #${data.swapped_with}.`
        : `Movido para ${when} em ${data.destination_channel_name}.`, 'success');
    }
  } catch (e) {
    showToast('Erro de rede ao mover o agendamento.', 'error');
  }
  loadCalendar();
  fetchStatus();
}

// ---- Editor de agendamento (clique no horário) ----
async function openEditPostModal(postId) {
  if (!postId || Number(postId) <= 0) {
    showToast('Vídeo agendado diretamente no YouTube Studio.', 'info');
    return;
  }
  try {
    const res = await fetch(`/api/scheduled-posts/${postId}`);
    const p = await res.json();
    if (!res.ok) { showToast(p.detail || 'Agendamento não encontrado.', 'error'); return; }
    if (!calendarDestChannels.length) calendarDestChannels = await (await fetch('/api/destination-channels')).json();
    document.getElementById('editPostId').value = p.id;
    document.getElementById('editPostClipId').value = p.clip_id;
    document.getElementById('editPostModalTitle').textContent = `Agendamento #${p.id}`;
    document.getElementById('editPostClipTitle').textContent = p.clip_title || `Clipe #${p.clip_id}`;
    const statusTxt = p.is_simulated ? 'simulado (canal sem token OAuth: nada foi enviado ao YouTube)'
      : p.status === 'awaiting_oauth' ? 'aguardando conectar o canal (OAuth)' : p.status;
    document.getElementById('editPostStatus').textContent = p.editable
      ? `Status: ${statusTxt}`
      : 'Este vídeo já foi enviado ao YouTube. Altere ou remova pelo YouTube Studio.';
    const when = document.getElementById('editPostWhen');
    when.value = (p.scheduled_for || '').slice(0, 16);
    const chSel = document.getElementById('editPostChannel');
    chSel.innerHTML = calendarDestChannels.map((ch) => `<option value="${Number(ch.id)}">${esc(ch.name)}${ch.has_token ? '' : ' (sem token)'}</option>`).join('');
    chSel.value = String(p.destination_channel_id);
    [when, chSel].forEach((el) => { el.disabled = !p.editable; });
    document.getElementById('editPostSaveBtn').style.display = p.editable ? '' : 'none';
    document.getElementById('editPostRemoveBtn').style.display = p.editable ? '' : 'none';
    setPostModalError('');
    openModal('editPostModal');
  } catch (e) {
    showToast('Erro ao abrir agendamento.', 'error');
  }
}

function setPostModalError(msg) {
  const el = document.getElementById('editPostError');
  el.textContent = msg || '';
  el.style.display = msg ? '' : 'none';
}

async function saveScheduledPost() {
  const postId = Number(document.getElementById('editPostId').value);
  const body = {
    scheduled_for: document.getElementById('editPostWhen').value,
    destination_channel_id: Number(document.getElementById('editPostChannel').value),
  };
  const res = await fetch(`/api/scheduled-posts/${postId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) { setPostModalError(data.detail || 'Erro ao salvar.'); return; }
  closeModal('editPostModal');
  showToast('Agendamento atualizado!', 'success');
  loadCalendar();
  fetchStatus();
}

async function removeScheduledPostFromModal() {
  const postId = Number(document.getElementById('editPostId').value);
  closeModal('editPostModal');
  await removeScheduledPost(postId);
}

/* ==========================================
   OPUSCLIP PRO: LOCAL MP4 UPLOAD, 14 PRESETS GALLERY, BRAND KITS & COOKIES
   ========================================== */

async function uploadLocalVideoFile(inputEl) {
  const file = inputEl?.files?.[0];
  if (!file) return;
  const lbl = document.getElementById('btnUploadMp4Text');
  if (lbl) lbl.textContent = 'Enviando...';
  showToast(`Enviando '${file.name}' para transcrição e cortes...`, 'info');
  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('auto_process', 'true');
    const res = await fetch('/api/videos/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao enviar vídeo local.', 'error');
      return;
    }
    showToast(data.message || 'Vídeo enviado com sucesso!', 'success');
    fetchStatus();
    switchView('queue');
  } catch (e) {
    showToast('Erro de rede ao enviar arquivo.', 'error');
  } finally {
    if (lbl) lbl.textContent = 'Enviar MP4';
    if (inputEl) inputEl.value = '';
  }
}

async function loadOpusPresetsAndBrandKits() {
  try {
    const [prRes, bkRes] = await Promise.all([
      fetch('/api/caption-presets'),
      fetch('/api/brand-kits'),
    ]);
    const prData = await prRes.json();
    const bkData = await bkRes.json();
    opusPresetsCache = prData.presets || [];
    brandKitsCache = bkData.kits || [];
    renderOpusPresetsGallery();
    renderBrandKitsList();
  } catch (e) {
    console.error('Erro ao carregar presets/brand-kits:', e);
  }
}

function renderOpusPresetsGallery() {
  const el = document.getElementById('opusPresetsGallery');
  if (!el || !opusPresetsCache.length) return;
  el.innerHTML = opusPresetsCache.map((p) => {
    const isActive = p.id === selectedPresetId;
    const bgStyle = p.borderStyle === 3 ? `background:${esc(p.back)}; padding:2px 6px; border-radius:4px;` : '';
    return `
      <button type="button" class="preset-tile ${isActive ? 'active' : ''}" onclick="applyOpusPreset('${esc(p.id)}')">
        <span class="preset-tile-sample" style="color:${esc(p.primary)}; ${bgStyle}">
          ${p.upper ? 'OLHA' : 'Olha'} <span style="color:${esc(p.highlight)};">${p.upper ? 'ISSO' : 'isso'}</span>
        </span>
        <span class="preset-tile-meta">
          <strong>${esc(p.name)}</strong>
          <span>${esc(p.source)}</span>
        </span>
      </button>
    `;
  }).join('');
}

function applyOpusPreset(presetId) {
  const p = opusPresetsCache.find((x) => x.id === presetId);
  if (!p) return;
  selectedPresetId = p.id;
  setText('activePresetBadge', p.id);

  if (document.getElementById('presetPrimaryColor')) document.getElementById('presetPrimaryColor').value = p.primary;
  if (document.getElementById('presetHighlightColor')) document.getElementById('presetHighlightColor').value = p.highlight;
  if (document.getElementById('presetOutlineColor')) document.getElementById('presetOutlineColor').value = p.outline;
  if (document.getElementById('presetFontSize')) document.getElementById('presetFontSize').value = p.fontSize;
  if (document.getElementById('presetWordsPerLine')) document.getElementById('presetWordsPerLine').value = String(p.wordsPerLine);
  if (document.getElementById('presetUppercase')) document.getElementById('presetUppercase').checked = Boolean(p.upper);
  if (document.getElementById('presetOutlineWidth')) document.getElementById('presetOutlineWidth').value = p.outlinePx ?? 6;
  if (document.getElementById('presetStyleMode')) {
    document.getElementById('presetStyleMode').value = p.borderStyle === 3
      ? 'box_caption'
      : (p.pop ? 'karaoke_pop' : 'word_highlight');
  }
  renderOpusPresetsGallery();
  updatePresetPreview();
  showToast(`Template '${p.name}' aplicado ao preview. Clique em "Salvar preset do estúdio" para gravar.`, 'info');
}

function renderBrandKitsList() {
  const el = document.getElementById('brandKitsList');
  if (!el) return;
  if (!brandKitsCache.length) {
    el.innerHTML = '<div style="font-size:12px; color:var(--text-muted); grid-column:1/-1;">Nenhum Brand Kit salvo ainda.</div>';
    return;
  }
  el.innerHTML = brandKitsCache.map((k) => `
    <article class="channel-card">
      <div class="channel-card-head">
        <div>
          <div style="font-weight:600; font-size:13.5px;">${esc(k.name)}</div>
          <div class="input-mono" style="font-size:11px; color:var(--text-muted); margin-top:2px;">Preset: ${esc(k.default_preset)}</div>
        </div>
        <div style="display:flex; gap:4px; align-items:center;">
          <span title="Cor base" style="width:14px; height:14px; border-radius:3px; border:1px solid var(--border-strong); background:${esc(k.primary_color)};"></span>
          <span title="Cor destaque" style="width:14px; height:14px; border-radius:3px; border:1px solid var(--border-strong); background:${esc(k.highlight_color)};"></span>
          <span title="Contorno" style="width:14px; height:14px; border-radius:3px; border:1px solid var(--border-strong); background:${esc(k.secondary_color)};"></span>
        </div>
      </div>
      <div style="font-size:11.5px; color:var(--text-secondary);">
        Marca d'água: <strong>${esc(k.watermark_text || 'Nenhuma')}</strong> (${esc(k.watermark_position)})
      </div>
      <div class="channel-card-foot">
        <button class="btn btn-sm" onclick="applyBrandKitToStudio(${k.id})">Carregar no preview</button>
        <button class="btn btn-danger btn-sm" onclick="deleteBrandKit(${k.id})">Excluir</button>
      </div>
    </article>
  `).join('');
}

async function createBrandKitFromCurrent() {
  const nameInput = document.getElementById('newBrandKitName');
  const name = (nameInput?.value || '').trim();
  if (!name) {
    showToast('Digite um nome para o Brand Kit.', 'error');
    return;
  }
  const payload = {
    name,
    primary_color: document.getElementById('presetPrimaryColor')?.value || '#FFFFFF',
    highlight_color: document.getElementById('presetHighlightColor')?.value || '#04F827',
    secondary_color: document.getElementById('presetOutlineColor')?.value || '#000000',
    font_family: document.getElementById('presetFontName')?.value || 'auto',
    default_preset: selectedPresetId || 'karaoke',
    watermark_text: document.getElementById('presetWatermark')?.value.trim() || '',
    watermark_position: 'top-right',
    watermark_opacity: 0.75,
  };
  try {
    const res = await fetch('/api/brand-kits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao criar Brand Kit.', 'error');
      return;
    }
    if (nameInput) nameInput.value = '';
    showToast(`Brand Kit '${data.name}' salvo!`, 'success');
    loadOpusPresetsAndBrandKits();
  } catch (e) {
    showToast('Erro de rede ao salvar Brand Kit.', 'error');
  }
}

function applyBrandKitToStudio(kitId) {
  const k = brandKitsCache.find((x) => x.id === kitId);
  if (!k) return;
  if (k.default_preset) applyOpusPreset(k.default_preset);
  if (document.getElementById('presetPrimaryColor')) document.getElementById('presetPrimaryColor').value = k.primary_color;
  if (document.getElementById('presetHighlightColor')) document.getElementById('presetHighlightColor').value = k.highlight_color;
  if (document.getElementById('presetOutlineColor')) document.getElementById('presetOutlineColor').value = k.secondary_color;
  if (document.getElementById('presetWatermark')) document.getElementById('presetWatermark').value = k.watermark_text || '';
  updatePresetPreview();
  showToast(`Brand Kit '${k.name}' carregado no preview!`, 'success');
}

async function deleteBrandKit(kitId) {
  if (!confirm('Excluir este Brand Kit?')) return;
  try {
    const res = await fetch(`/api/brand-kits/${kitId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Brand Kit excluído.', 'success');
      loadOpusPresetsAndBrandKits();
    }
  } catch (e) {
    showToast('Erro ao excluir Brand Kit.', 'error');
  }
}

async function loadCookiesStatus() {
  try {
    const res = await fetch('/api/settings/cookies');
    const data = await res.json();
    const badge = document.getElementById('cookiesStatusBadge');
    if (badge) {
      badge.textContent = data.has_cookies ? `Ativo (${Math.round(data.size_bytes / 1024)} KB)` : 'Não configurado';
      badge.className = `badge ${data.has_cookies ? 'badge-ok' : ''}`;
    }
  } catch (e) { /* ignora */ }
}

async function saveCookiesTxt() {
  const el = document.getElementById('cookiesTxtInput');
  const txt = (el?.value || '').trim();
  if (txt.length < 32) {
    showToast('Cole o conteúdo completo do arquivo cookies.txt.', 'error');
    return;
  }
  try {
    const res = await fetch('/api/settings/cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cookies_text: txt }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar cookies.', 'error');
      return;
    }
    if (el) el.value = '';
    showToast(data.message || 'Cookies salvos!', 'success');
    loadCookiesStatus();
  } catch (e) {
    showToast('Erro de rede ao salvar cookies.', 'error');
  }
}

async function deleteCookiesTxt() {
  try {
    const res = await fetch('/api/settings/cookies', { method: 'DELETE' });
    if (res.ok) {
      showToast('Cookies removidos.', 'success');
      loadCookiesStatus();
    }
  } catch (e) {
    showToast('Erro ao remover cookies.', 'error');
  }
}

/* ==========================================
   LABORATÓRIO 7 DIAS (EXPERIMENTS LAB)
   ========================================== */
let activeExperimentId = null;

async function loadExperimentsView() {
  try {
    const [destRes, expRes] = await Promise.all([
      fetch('/api/destination-channels'),
      fetch('/api/experiments'),
    ]);
    const dests = destRes.ok ? await destRes.json() : [];
    const exps = expRes.ok ? await expRes.json() : [];

    setText('navCountExperiments', exps.length);

    const sel = document.getElementById('expTargetChannel');
    if (sel) {
      const prev = sel.value || 'all';
      const activeCount = dests.filter((d) => d.is_active).length;
      sel.innerHTML = `<option value="all">Todos os canais ativos (${activeCount})</option>` +
        dests.map((d) => `<option value="${d.id}">${esc(d.name)}${d.is_active ? '' : ' (Pausado)'}</option>`).join('');
      const hasPrev = Array.from(sel.options).some((o) => o.value === prev);
      sel.value = hasPrev ? prev : 'all';
    }

    const listEl = document.getElementById('experimentsCardsList');
    if (!listEl) return;

    if (!exps.length) {
      listEl.innerHTML = `
        <div class="empty-state" style="grid-column:1/-1;">
          Nenhum experimento de 7 dias rodando ainda. Clique em <strong>Rodar semana de teste (7 dias)</strong> acima para criar a matriz completa de variações em cada canal.
        </div>`;
      const det = document.getElementById('experimentDetailWrap');
      if (det) det.style.display = 'none';
      return;
    }

    if (!activeExperimentId || !exps.some((e) => e.id === activeExperimentId)) {
      activeExperimentId = exps[0].id;
    }

    listEl.innerHTML = exps.map((e) => `
      <div class="exp-card ${e.id === activeExperimentId ? 'active' : ''}" onclick="openExperimentDetail(${e.id})">
        <div class="exp-card-top">
          <div>
            <div class="exp-card-title">${esc(e.name)}</div>
            <div style="font-size:12px; color:var(--text-muted); margin-top:2px;">${esc(e.channel_name)}</div>
          </div>
          <div style="display:flex; gap:6px; align-items:center;">
            <span class="badge ${e.status === 'active' ? 'badge-ok' : ''}">${e.status === 'active' ? 'Ativo' : 'Concluído'}</span>
            <button class="btn btn-ghost btn-sm" style="padding:2px 6px;" title="Excluir experimento" onclick="event.stopPropagation(); deleteExperimentById(${e.id})">×</button>
          </div>
        </div>
        <div class="exp-card-meta">
          <span><strong>${e.filled_slots}/${e.total_slots}</strong> slots preenchidos</span>
          <span><strong>${e.days}d</strong> × ${e.posts_per_day}/dia</span>
          <span><strong>${e.total_views}</strong> views</span>
        </div>
      </div>
    `).join('');

    await openExperimentDetail(activeExperimentId);
  } catch (err) {
    showToast('Erro ao carregar Laboratório 7 Dias.', 'error');
  }
}

async function launchWeeklyExperiment() {
  const chVal = document.getElementById('expTargetChannel')?.value || 'all';
  const postsPerDay = Number(document.getElementById('expPostsPerDay')?.value || 4);
  const btn = document.getElementById('btnLaunchExperiment');
  if (btn) { btn.disabled = true; btn.textContent = 'Gerando matriz...'; }
  try {
    const res = await fetch('/api/experiments/launch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel_id: chVal === 'all' ? 'all' : Number(chVal),
        days: 7,
        posts_per_day: postsPerDay,
        auto_fill: true,
        apply_variants: true,
        include_analyzed: false,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao iniciar semana de teste.', 'error');
      return;
    }
    const first = data.experiments?.[0];
    if (first?.id) activeExperimentId = first.id;
    showToast(`Semana de teste iniciada em ${data.count} canal(is)!`, 'success');
    await loadExperimentsView();
  } catch (e) {
    showToast('Erro de rede ao lançar experimento.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Rodar semana de teste (7 dias)'; }
  }
}

async function openExperimentDetail(expId) {
  activeExperimentId = expId;
  document.querySelectorAll('.exp-card').forEach((card) => {
    card.classList.toggle('active', card.getAttribute('onclick')?.includes(`(${expId})`));
  });
  const wrap = document.getElementById('experimentDetailWrap');
  if (!wrap) return;
  try {
    const res = await fetch(`/api/experiments/${expId}`);
    if (!res.ok) return;
    const report = await res.json();
    wrap.style.display = 'block';

    setText('expDetailTitle', `${report.name}`);
    setText(
      'expDetailSubtitle',
      `${report.filled_slots}/${report.total_slots} slots com clipe • ${report.measured_posts} medidos • ${report.total_views} views totais • ${report.total_likes} likes`
    );

    const winnersEl = document.getElementById('expWinnersGrid');
    const dims = [
      { key: 'time_bucket', title: 'Melhor Horário' },
      { key: 'edit_style', title: 'Melhor Edição' },
      { key: 'seo_style', title: 'Melhor SEO' },
      { key: 'scene_mode', title: 'Melhor Cena / Layout' },
      { key: 'pacing_target', title: 'Melhor Duração' },
    ];
    if (winnersEl) {
      winnersEl.innerHTML = dims.map((d) => {
        const w = report.winners?.[d.key];
        return `
          <div class="exp-winner-card">
            <div class="exp-winner-label">${d.title}</div>
            <div class="exp-winner-val">${w ? esc(w.label) : 'Aguardando métricas'}</div>
            <div class="exp-winner-sub">${w ? `Média ${w.avg_views} views • Ret. ${w.avg_retention_pct}% • Score ${w.avg_score}` : 'Sincronize YT ou lance views'}</div>
          </div>`;
      }).join('');
    }

    const matrixEl = document.getElementById('expMatrixGrid');
    if (matrixEl) {
      matrixEl.innerHTML = (report.days_matrix || []).map((day) => `
        <div class="exp-day-col">
          <div class="exp-day-head">
            <strong>Dia ${day.day_index} — ${esc(day.day_name)}</strong>
            <span class="badge">${esc(day.date)}</span>
          </div>
          ${day.slots.map((s) => `
            <div class="exp-slot-card">
              <div class="exp-slot-top">
                <span class="exp-slot-time">${esc(s.time)} • ${esc(s.time_bucket_label.split(' ')[0])}</span>
                <span class="badge ${s.post_id ? 'badge-ok' : ''}">${s.post_id ? esc(s.post_status) : 'Vago'}</span>
              </div>
              ${s.clip
                ? `<div class="exp-slot-clip">${esc(s.clip.title)} <span style="color:var(--text-muted);">(${s.clip.duration}s)</span></div>`
                : `<div class="exp-slot-empty">Aguardando próximo clipe renderizado (${esc(s.pacing_target_label)})</div>`}
              <div class="exp-badges">
                <span class="exp-badge" title="Estilo de Edição">Edição: ${esc(s.edit_style_label)}</span>
                <span class="exp-badge" title="Ângulo de SEO">SEO: ${esc(s.seo_style_label)}</span>
                <span class="exp-badge" title="Cena / Enquadramento">Cena: ${esc(s.scene_mode_label)}</span>
                <span class="exp-badge" title="Duração alvo">${esc(s.pacing_target_label)}</span>
              </div>
              ${s.post_id ? `
                <div class="exp-metrics-row">
                  <span>👁 ${s.views_count} • 👍 ${s.likes_count} • 💬 ${s.comments_count} • ⏱ ${s.avg_retention_pct}%</span>
                  <button class="btn btn-ghost btn-sm" style="padding:2px 6px; font-size:11px;" onclick="editExperimentPostMetrics(${s.post_id}, ${s.views_count}, ${s.likes_count}, ${s.comments_count}, ${s.avg_retention_pct})">Métricas</button>
                </div>` : ''}
            </div>
          `).join('')}
        </div>
      `).join('');
    }
  } catch (e) {
    showToast('Erro ao abrir matriz do experimento.', 'error');
  }
}

async function fillActiveExperimentSlots() {
  if (!activeExperimentId) return;
  try {
    const res = await fetch(`/api/experiments/${activeExperimentId}/fill`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apply_variants: true, include_analyzed: false }),
    });
    const data = await res.json();
    showToast(`${data.newly_assigned || 0} novo(s) clipe(s) alocado(s) (${data.filled_slots}/${data.total_slots}).`, 'success');
    await loadExperimentsView();
  } catch (e) {
    showToast('Erro ao preencher slots do experimento.', 'error');
  }
}

async function syncActiveExperimentMetrics() {
  if (!activeExperimentId) return;
  try {
    const res = await fetch(`/api/experiments/${activeExperimentId}/sync-metrics`, { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Métricas sincronizadas.', 'success');
    await openExperimentDetail(activeExperimentId);
  } catch (e) {
    showToast('Erro ao sincronizar métricas do YouTube.', 'error');
  }
}

async function editExperimentPostMetrics(postId, curViews, curLikes, curComments, curRet) {
  const raw = prompt(
    'Informe as métricas separadas por vírgula: views, likes, comentarios, retencao%\nExemplo: 12500, 840, 42, 94.5',
    `${curViews || 0}, ${curLikes || 0}, ${curComments || 0}, ${curRet || 0}`
  );
  if (raw === null) return;
  const parts = raw.split(',').map((x) => Number(x.trim()) || 0);
  try {
    const res = await fetch(`/api/scheduled-posts/${postId}/metrics`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        views_count: parts[0] || 0,
        likes_count: parts[1] || 0,
        comments_count: parts[2] || 0,
        avg_retention_pct: parts[3] || 0,
      }),
    });
    if (res.ok) {
      showToast('Métricas atualizadas! Ranking recalculado.', 'success');
      await openExperimentDetail(activeExperimentId);
    }
  } catch (e) {
    showToast('Erro ao salvar métricas.', 'error');
  }
}

async function applyActiveExperimentWinner() {
  if (!activeExperimentId) return;
  try {
    const res = await fetch(`/api/experiments/${activeExperimentId}/apply-winner`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao aplicar fórmula vencedora.', 'error');
      return;
    }
    showToast(`Fórmula vencedora aplicada em ${data.channel_name}!`, 'success');
    await loadExperimentsView();
  } catch (e) {
    showToast('Erro ao aplicar fórmula vencedora.', 'error');
  }
}

async function deleteExperimentById(expId) {
  if (!confirm('Remover este experimento da lista? Os vídeos já publicados não são apagados.')) return;
  try {
    const res = await fetch(`/api/experiments/${expId}`, { method: 'DELETE' });
    if (res.ok) {
      if (activeExperimentId === expId) activeExperimentId = null;
      showToast('Experimento removido.', 'success');
      await loadExperimentsView();
    }
  } catch (e) {
    showToast('Erro ao remover experimento.', 'error');
  }
}

/* ==========================================
   ANALYTICS, BGM, GOOGLE DRIVE, PALAVRAS & EXPORT ZIP
   ========================================== */
let wordsEditorClipId = null;
let wordsEditorBuffer = [];

async function exportClipsBatchZip() {
  showToast('Gerando pacote .zip com MP4 + Thumb + SEO...', 'info');
  try {
    const res = await fetch('/api/clips/export-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Nenhum clipe renderizado disponível para exportar.', 'error');
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'clipper_shorts_pack.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast('Pacote .zip baixado! Pronto para TikTok, Reels e Kwai.', 'success');
  } catch (e) {
    showToast('Erro de rede ao exportar lote .zip.', 'error');
  }
}

const WORDS_SOURCE_LABEL = {
  custom: 'Editadas por você',
  video_transcript: 'Transcrição do vídeo',
  none: 'Sem transcrição',
};

function wordsSourceLabel(src, lang) {
  let label = WORDS_SOURCE_LABEL[src] || (String(src || '').includes('whisper') ? 'Whisper automático' : (src || '—'));
  if (lang && lang !== 'auto') label += ` · ${String(lang).toUpperCase()}`;
  return label;
}

function renderWordsEditorList() {
  const listEl = document.getElementById('wordsEditorList');
  if (!listEl) return;
  if (!wordsEditorBuffer.length) {
    listEl.innerHTML = '<div class="empty-state">Nenhuma palavra transcrita nesta janela ainda. Clique em "Transcrever de novo" ou renderize o clipe (a transcrição é automática no render).</div>';
  } else {
    listEl.innerHTML = wordsEditorBuffer.map((w, i) => `
      <div style="display:flex; flex-direction:column; gap:2px; background:#141418; border:1px solid #26262e; border-radius:8px; padding:6px;">
        <input type="text" class="input" style="width:110px; max-width:100%; padding:4px 6px; font-size:12px;" value="${esc(w.word)}" oninput="wordsEditorBuffer[${i}].word = this.value">
        <span style="font-size:10px; color:var(--text-muted); font-family:'JetBrains Mono',monospace;">${Number(w.start).toFixed(1)}s</span>
      </div>
    `).join('');
  }
  setText('wordsEditorTitle', `Legendas Palavra por Palavra — Clipe #${wordsEditorClipId} (${wordsEditorBuffer.length} palavras)`);
}

async function openWordsEditorForEditClip() {
  const clipId = Number(document.getElementById('editClipId')?.value || 0);
  if (!clipId) return;
  wordsEditorClipId = clipId;
  try {
    const res = await fetch(`/api/clips/${clipId}/words`);
    const data = await res.json();
    wordsEditorBuffer = Array.isArray(data.words) ? data.words : [];
    setText('wordsEditorSource', wordsSourceLabel(data.source, data.language));
    const langSel = document.getElementById('wordsEditorLanguage');
    if (langSel) langSel.value = '';
    renderWordsEditorList();
    openModal('wordsEditorModal');
  } catch (e) {
    showToast('Erro ao carregar palavras do clipe.', 'error');
  }
}

async function retranscribeWordsEditor() {
  if (!wordsEditorClipId) return;
  const btn = document.getElementById('wordsRetranscribeBtn');
  const language = document.getElementById('wordsEditorLanguage')?.value || '';
  if (btn) { btn.disabled = true; btn.textContent = 'Transcrevendo...'; }
  try {
    const res = await fetch(`/api/clips/${wordsEditorClipId}/transcribe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(language ? { language } : {}),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Não consegui transcrever o clipe.', 'error');
      return;
    }
    wordsEditorBuffer = Array.isArray(data.words) ? data.words : [];
    setText('wordsEditorSource', wordsSourceLabel(data.source, data.language));
    renderWordsEditorList();
    showToast(`${wordsEditorBuffer.length} palavras transcritas. Revise e clique em salvar para usar no vídeo.`, 'success');
  } catch (e) {
    showToast('Erro de rede ao transcrever.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Transcrever de novo'; }
  }
}

async function saveEditedWords(reRender) {
  if (!wordsEditorClipId) return;
  try {
    const res = await fetch(`/api/clips/${wordsEditorClipId}/words`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ words: wordsEditorBuffer, re_render: Boolean(reRender) }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar palavras.', 'error');
      return;
    }
    closeModal('wordsEditorModal');
    showToast(reRender ? `${data.words_count} palavras salvas! Re-renderizando em segundo plano...` : `${data.words_count} palavras salvas!`, 'success');
  } catch (e) {
    showToast('Erro de rede ao salvar palavras.', 'error');
  }
}

async function loadAnalyticsAndStorageView() {
  try {
    const [anRes, bgmRes, stRes, kpRes] = await Promise.all([
      fetch('/api/analytics/overview'),
      fetch('/api/bgm'),
      fetch('/api/storage/status'),
      fetch('/api/settings/key-pool'),
    ]);
    const an = anRes.ok ? await anRes.json() : {};
    const bgms = bgmRes.ok ? await bgmRes.json() : [];
    const st = stRes.ok ? await stRes.json() : {};
    const kp = kpRes.ok ? await kpRes.json() : [];

    const kpiEl = document.getElementById('analyticsKpiGrid');
    if (kpiEl) {
      kpiEl.innerHTML = `
        <div class="exp-winner-card"><div class="exp-winner-label">Views Totais (YouTube)</div><div class="exp-winner-val">${Number(an.total_views || 0).toLocaleString('pt-BR')}</div><div class="exp-winner-sub">${Number(an.total_subscribers || 0).toLocaleString('pt-BR')} inscritos • ${an.total_posts || 0} na grade</div></div>
        <div class="exp-winner-card"><div class="exp-winner-label">Produção Local</div><div class="exp-winner-val">${an.rendered_clips || 0}/${an.total_clips || 0} MP4</div><div class="exp-winner-sub">${Number(an.total_likes || 0).toLocaleString('pt-BR')} likes • ${Number(an.total_comments || 0).toLocaleString('pt-BR')} comentários</div></div>
        <div class="exp-winner-card"><div class="exp-winner-label">Retenção Média</div><div class="exp-winner-val">${an.avg_retention_pct || 0}%</div><div class="exp-winner-sub">Alimenta o Lab 7 Dias</div></div>
        <div class="exp-winner-card"><div class="exp-winner-label">Disco (Lives Brutas)</div><div class="exp-winner-val">${st.raw_size_mb || 0} MB</div><div class="exp-winner-sub">${st.raw_files_count || 0} arquivos locais • ${st.archived_count || 0} no Drive</div></div>
      `;
    }

    const bdEl = document.getElementById('analyticsBreakdowns');
    if (bdEl) {
      const chRows = (an.by_channel || []).map((c) => `
        <div class="exp-metrics-row">
          <span>
            <strong>${esc(c.channel_name)}</strong>
            <span class="badge ${c.oauth_connected ? 'badge-ok' : ''}" style="margin-left:4px;">${c.oauth_connected ? 'OAuth OK' : 'Sem OAuth'}</span>
            <span style="color:var(--text-muted); margin-left:4px;">(${c.posts} posts • ${c.weight || 0}%)</span>
          </span>
          <span>👥 ${Number(c.subscribers || 0).toLocaleString('pt-BR')} • 👁 ${Number(c.channel_views || c.views || 0).toLocaleString('pt-BR')}</span>
        </div>
      `).join('');
      const scRows = (an.by_scene || []).map((s) => `<div class="exp-metrics-row"><span>Cena: <strong>${esc(s.scene)}</strong> (${s.clips || s.posts || 0} clipes)</span><span>👁 ${Number(s.views || 0).toLocaleString('pt-BR')} • 👍 ${s.likes || 0}</span></div>`).join('');
      bdEl.innerHTML = (chRows || '<div class="empty-state">Nenhuma conta YouTube cadastrada ainda.</div>') + (scRows ? `<div style="margin-top:10px; padding-top:8px; border-top:1px solid var(--border);">${scRows}</div>` : '');
    }

    const topEl = document.getElementById('analyticsTopShorts');
    if (topEl) {
      const items = (an.top_shorts || []).slice(0, 8);
      topEl.innerHTML = items.length
        ? items.map((s) => `
          <div class="exp-metrics-row">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:64%;" title="${esc(s.title)}">
              ${s.youtube_url ? `<a href="${esc(s.youtube_url)}" target="_blank" rel="noopener" style="color:var(--text-main);"><strong>${esc(s.title)}</strong></a>` : `<strong>${esc(s.title)}</strong>`}
              <span style="color:var(--text-muted);">(${esc(s.channel_name || s.streamer)})</span>
            </span>
            <span>👁 ${Number(s.views || 0).toLocaleString('pt-BR')} • 👍 ${s.likes || 0}${s.retention_pct ? ` • ⏱ ${s.retention_pct}%` : ''}</span>
          </div>
        `).join('')
        : '<div class="empty-state">Clique em <strong>Sincronizar YouTube agora</strong> (para contas OAuth) ou lance views no <strong>Lab 7 Dias</strong>.</div>';
    }

    const bgmEl = document.getElementById('bgmTracksList');
    if (bgmEl) {
      bgmEl.innerHTML = bgms.length
        ? bgms.map((t) => `
          <div class="exp-metrics-row">
            <span>🎵 <strong>${esc(t.filename)}</strong> (${t.size_kb} KB)</span>
            <button class="btn btn-ghost btn-sm" onclick="deleteBgmTrack('${esc(t.filename)}')">Remover</button>
          </div>
        `).join('')
        : '<div class="empty-state">Nenhuma trilha BGM enviada ainda. Clique em "Enviar BGM" para adicionar .mp3/.wav.</div>';
    }

    const drvEl = document.getElementById('driveStorageBox');
    if (drvEl) {
      const archivedEntries = Object.entries(st.archived_videos || {});
      const archivable = st.archivable_videos || [];
      drvEl.innerHTML = `
        <div class="exp-metrics-row" style="margin-bottom:8px;">
          <span>Status Google Drive: <span class="badge ${st.gdrive_oauth_connected ? 'badge-ok' : ''}">${st.gdrive_oauth_connected ? 'Conectado (OAuth)' : 'Desconectado'}</span></span>
          <a href="/api/auth/gdrive/login" class="btn btn-primary btn-sm">${st.gdrive_oauth_connected ? 'Reconectar Drive' : 'Conectar Google Drive (OAuth)'}</a>
        </div>
        <div style="display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap;">
          <input id="gdriveFolderInput" type="text" class="input" style="flex:1; min-width:180px;" placeholder="Cole o link ou ID da pasta no Google Drive" value="${esc(st.drive_folder_id || '')}">
          <button class="btn btn-secondary btn-sm" onclick="saveDriveConfig()">Salvar pasta</button>
        </div>
        <div class="exp-metrics-row">
          <span>Lives em disco local: <strong>${st.raw_files_count || 0}</strong> (${st.raw_size_mb || 0} MB) • Clipes: <strong>${st.clips_size_mb || 0} MB</strong></span>
          <button class="btn btn-danger btn-sm" id="btnCleanMedia" onclick="cleanVideosAndClipsOnly()" title="Apaga apenas vídeos/clipes locais e do banco. Mantém contas, APIs, Drive e configurações intactos.">Limpar vídeos e clipes</button>
        </div>
        ${archivable.length ? `<div style="margin-top:8px; font-size:11.5px; font-weight:600; color:var(--text-muted);">Lives locais prontas para arquivar no Drive:</div>` + archivable.slice(0, 8).map((v) => `
          <div class="exp-metrics-row">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:66%;" title="${esc(v.title)}">💾 ${esc(v.title)} (${v.size_mb} MB)</span>
            <button class="btn btn-secondary btn-sm" id="btnArch-${esc(v.video_id)}" onclick="archiveVideoToDrive('${esc(v.video_id)}')">Arquivar no Drive</button>
          </div>
        `).join('') : ''}
        ${archivedEntries.length ? `<div style="margin-top:8px; font-size:11.5px; font-weight:600; color:var(--text-muted);">Arquivadas no Google Drive (${archivedEntries.length}):</div>` + archivedEntries.map(([vid, info]) => `
          <div class="exp-metrics-row">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:60%;">☁️ ${info.drive_url ? `<a href="${esc(info.drive_url)}" target="_blank" rel="noopener" style="color:var(--text-main);">${esc(info.title || vid)}</a>` : esc(info.title || vid)} (${info.size_mb || 0} MB)</span>
            <button class="btn btn-secondary btn-sm" onclick="restoreVideoFromDrive('${esc(vid)}')">Restaurar do Drive</button>
          </div>
        `).join('') : ''}
      `;
    }

    const kpEl = document.getElementById('keyPoolStatusBox');
    if (kpEl) {
      kpEl.innerHTML = `<div style="font-size:12px; font-weight:600; margin-bottom:6px;">Pool Multi-Keys de IA (Failover 429)</div>` +
        kp.map((p) => `<div class="exp-metrics-row"><span>${esc(p.provider.toUpperCase())}</span><span>${p.healthy_keys}/${p.total_keys} chave(s) ativas${p.cooling_keys ? ` (${p.cooling_keys} em cooldown)` : ''}</span></div>`).join('') +
        `<p class="section-desc" style="margin-top:6px;">Dica: em IA & Configurações, cole várias chaves separadas por vírgula ou quebra de linha para rotacionar automaticamente sem cair em erro 429.</p>`;
    }
  } catch (e) {
    showToast('Erro ao carregar Analytics & Drive.', 'error');
  }
}

async function uploadBgmTrack(ev) {
  const file = ev.target?.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/bgm/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao enviar BGM.', 'error');
      return;
    }
    showToast(`Trilha ${data.filename} enviada para a biblioteca BGM!`, 'success');
    loadAnalyticsAndStorageView();
  } catch (e) {
    showToast('Erro de rede ao enviar BGM.', 'error');
  } finally {
    if (ev.target) ev.target.value = '';
  }
}

async function deleteBgmTrack(filename) {
  try {
    const res = await fetch(`/api/bgm/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Trilha removida.', 'success');
      loadAnalyticsAndStorageView();
    }
  } catch (e) {
    showToast('Erro ao remover trilha.', 'error');
  }
}

async function restoreVideoFromDrive(videoId) {
  showToast('Restaurando live do Google Drive...', 'info');
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(videoId)}/restore-drive`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao restaurar do Drive.', 'error');
      return;
    }
    showToast('Live restaurada para o disco local! Pronta para novos cortes/compilados.', 'success');
    loadAnalyticsAndStorageView();
  } catch (e) {
    showToast('Erro de rede ao restaurar do Drive.', 'error');
  }
}

/* ==========================================
   REGENERAÇÃO INDIVIDUAL & EM LOTE DE SEO POR SELEÇÃO
   ========================================== */
function updateSelectedClipsCounter() {
  setText('selectedClipsCount', `${selectedClipIds.size} selecionado(s)`);
}

function toggleClipSelection(clipId) {
  if (selectedClipIds.has(clipId)) selectedClipIds.delete(clipId);
  else selectedClipIds.add(clipId);
  renderClipsGrid();
}

function selectAllVisibleClips() {
  const filtered = clipsCache.filter((c) => {
    if (currentClipFilter === 'all') return true;
    if (currentClipFilter === 'liked') return Boolean(c.liked);
    return c.status === currentClipFilter;
  });
  filtered.forEach((c) => selectedClipIds.add(c.id));
  renderClipsGrid();
}

function clearClipSelection() {
  selectedClipIds.clear();
  renderClipsGrid();
}

async function regenerateSingleClipSeo(clipId) {
  const btn = document.getElementById(`btnSeo-${clipId}`);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    const angle = document.getElementById('batchSeoAngle')?.value || '';
    const res = await fetch(`/api/clips/${clipId}/generate-seo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        style_angle: angle === 'rotate' ? '' : angle,
        prefer_llm: true,
        variation_nonce: Math.floor(Math.random() * 999999),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao regenerar SEO.', 'error');
      return;
    }
    showToast(`Novo título e SEO gerados para o clipe #${clipId}!`, 'success');
    await loadClips();
  } catch (e) {
    showToast('Erro de rede ao regenerar SEO.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ SEO'; }
  }
}

async function regenerateSelectedClipsSeo() {
  const ids = Array.from(selectedClipIds);
  if (!ids.length) {
    showToast('Marque a caixinha de 1 ou mais clipes primeiro.', 'error');
    return;
  }
  const btn = document.getElementById('btnBatchSeo');
  const angle = document.getElementById('batchSeoAngle')?.value || 'rotate';
  if (btn) { btn.disabled = true; btn.textContent = `Regenerando (${ids.length})...`; }
  try {
    const res = await fetch('/api/clips/generate-seo-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_ids: ids, style_angle: angle, prefer_llm: true }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro no multi-regeneramento de SEO.', 'error');
      return;
    }
    showToast(`SEO regenerado em lote para ${data.updated} clipe(s)!`, 'success');
    await loadClips();
  } catch (e) {
    showToast('Erro de rede no multi-regeneramento de SEO.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Regenerar SEO dos selecionados'; }
  }
}

async function exportSelectedClipsZip() {
  const ids = Array.from(selectedClipIds);
  if (!ids.length) {
    return exportClipsBatchZip();
  }
  showToast(`Gerando .zip com ${ids.length} clipe(s) selecionado(s)...`, 'info');
  try {
    const res = await fetch('/api/clips/export-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_ids: ids }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Nenhum dos clipes selecionados possui MP4 renderizado.', 'error');
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `clipper_selecionados_${ids.length}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast(`Pacote .zip com ${ids.length} clipe(s) baixado!`, 'success');
  } catch (e) {
    showToast('Erro de rede ao exportar selecionados.', 'error');
  }
}

async function syncAnalyticsYoutube() {
  const btn = document.getElementById('btnSyncYtAnalytics');
  if (btn) { btn.disabled = true; btn.textContent = 'Sincronizando...'; }
  try {
    const res = await fetch('/api/analytics/sync-youtube', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao sincronizar YouTube Analytics.', 'error');
      return;
    }
    showToast(data.message || 'YouTube sincronizado!', data.synced_channels > 0 ? 'success' : 'info');
    await loadAnalyticsAndStorageView();
  } catch (e) {
    showToast('Erro de rede ao sincronizar YouTube.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Sincronizar YouTube agora'; }
  }
}

async function saveDriveConfig() {
  const folder = document.getElementById('gdriveFolderInput')?.value.trim() || '';
  try {
    const res = await fetch('/api/storage/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ drive_folder_id: folder }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao salvar pasta do Drive.', 'error');
      return;
    }
    showToast(`Pasta do Google Drive salva (${data.drive_folder_id || 'raiz do Drive'})!`, 'success');
    loadAnalyticsAndStorageView();
  } catch (e) {
    showToast('Erro de rede ao salvar pasta do Drive.', 'error');
  }
}

async function archiveVideoToDrive(videoId) {
  const btn = document.getElementById(`btnArch-${videoId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Enviando...'; }
  showToast('Enviando live para o Google Drive (só libera o disco após confirmar)...', 'info');
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(videoId)}/archive-drive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete_local_after: true }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao arquivar no Google Drive.', 'error');
      return;
    }
    showToast(`Live arquivada no Drive! ${data.freed_mb || 0} MB liberados no disco local.`, 'success');
    loadAnalyticsAndStorageView();
  } catch (e) {
    showToast('Erro de rede ao arquivar live no Drive.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Arquivar no Drive'; }
  }
}

async function cleanVideosAndClipsOnly() {
  if (!confirm('Limpar TODOS os vídeos processados, clipes gerados e arquivos locais (downloads/clips/thumbnails/transcrições)?\n\nSuas chaves de API, contas YouTube, Google Drive, canais monitorados, Brand Kits, BGM e configurações serão 100% PRESERVADOS.')) {
    return;
  }
  const btn = document.getElementById('btnCleanMedia');
  if (btn) { btn.disabled = true; btn.textContent = 'Limpando...'; }
  try {
    const res = await fetch('/api/storage/clean-media', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archive_to_drive_first: false }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Erro ao limpar vídeos e clipes.', 'error');
      return;
    }
    showToast(`Limpeza concluída: ${data.deleted_videos} vídeo(s), ${data.deleted_clips} clipe(s) e ${data.files_removed} arquivo(s) (${data.freed_mb} MB). Contas, APIs e Drive intactos!`, 'success');
    await Promise.all([fetchStatus(), loadAnalyticsAndStorageView()]);
  } catch (e) {
    showToast('Erro de rede ao limpar vídeos e clipes.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Limpar vídeos e clipes'; }
  }
}

/* Clipper Studio v2 — Frontend Controller (Zero console errors, full SSE sync) */

const VIEW_TITLES = {
  clips: 'Clipes 9:16',
  queue: 'Fila & Esteira',
  calendar: 'Agenda Semanal (4/dia × 7 dias)',
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
let opusPresetsCache = [];
let brandKitsCache = [];
let selectedPresetId = 'think-media';
let knownBootId = null;
let lastSnapshot = '';
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

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    applyStatusData(data);
  } catch (e) {
    console.error('Status fetch failed:', e);
  }
}

function startRealtimeStream() {
  const es = new EventSource('/api/events/stream');
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.boot_id) {
        if (knownBootId && knownBootId !== data.boot_id) {
          showToast('Nova versão do servidor detectada. Atualizando painel...', 'info');
          setTimeout(() => window.location.reload(), 600);
          return;
        }
        knownBootId = data.boot_id;
      }
      applyStatusData(data);

      const snap = `${data.videos_tracked}-${data.clips_total}-${data.clips_rendered}-${data.scheduled_posts}-${data.destination_channels}-${data.monitored_channels}`;
      if (lastSnapshot && lastSnapshot !== snap && !dndActive && !document.getElementById('editPostModal')?.classList.contains('open')) {
        refreshCurrentView();
      }
      lastSnapshot = snap;
    } catch (e) {
      console.error('SSE parse error:', e);
    }
  };
  es.onerror = () => {
    // EventSource reconecta automaticamente
  };
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
    const statusBadge = c.is_ready
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

    return `
      <article class="clip-card">
        ${mediaBlock}
        <div class="clip-body">
          <div class="clip-meta-top">
            <span>${esc(c.channel_name)}</span>
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
  setText('editClipEmojisStatus', '');

  openModal('editClipModal');
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
    showToast('Alterações OpusClip do clipe salvas!', 'success');
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
    document.getElementById('exportCaptionText').value = data.caption || '';
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

async function copyExportCaption() {
  const txt = document.getElementById('exportCaptionText').value;
  try {
    await navigator.clipboard.writeText(txt);
    showToast('Legenda e hashtags copiadas!', 'success');
  } catch {
    showToast('Selecione e copie o texto manualmente.', 'info');
  }
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
            const stLabel = s.is_simulated ? 'SIMULADO'
              : s.status === 'scheduled_on_youtube' ? 'NO YOUTUBE'
              : s.status === 'awaiting_oauth' ? 'SEM OAUTH'
              : s.status === 'pending' ? 'AGENDADO'
              : s.status === 'failed' ? 'ERRO'
              : 'VAGO';
            const movable = s.is_filled && (EDITABLE_POST_STATUSES.includes(s.status) || s.is_simulated);
            const stCls = s.status === 'scheduled_on_youtube' ? 'badge-ok'
              : s.status === 'awaiting_oauth' ? 'badge-warn'
              : s.status === 'failed' ? 'badge-err'
              : s.is_filled ? 'badge-info' : '';

            return `
              <div class="cal-slot ${s.is_filled ? 'filled' : ''} ${movable ? 'draggable' : ''}" data-slot-dt="${esc(s.datetime)}"
                   ${s.is_filled ? `data-post-id="${Number(s.post_id)}" onclick="openEditPostModal(${Number(s.post_id)})" title="Clique para editar${movable ? ', arraste para mover' : ''}"` : ''}
                   ${movable ? 'data-draggable="true"' : ''}>
                <div class="cal-slot-top">
                  <span>${esc(s.is_filled && s.scheduled_for ? s.scheduled_for.slice(11, 16) : s.time)}</span>
                  <span class="badge ${stCls}" style="font-size:9.5px; padding:1px 5px;">${stLabel}</span>
                </div>
                ${s.is_filled && s.clip ? `
                  <div class="cal-slot-title" title="${esc(s.clip.title)}">${esc(s.clip.title)}</div>
                  <div class="cal-slot-foot">
                    <span>${esc(s.clip.duration)}s · S${esc(s.clip.score)}</span>
                    <button class="btn btn-ghost btn-sm" style="height:20px; padding:0 5px; font-size:10.5px;" onclick="event.stopPropagation(); removeScheduledPost(${Number(s.post_id)})">Remover</button>
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
      updatePresetPreview();
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
  sub.style.display = enableSubs ? 'block' : 'none';
  // Escala de 1920px -> 480px (fator 0.25)
  sub.style.bottom = `${Math.round(marginV * 0.25)}px`;
  sub.style.fontSize = `${Math.max(12, Math.round(fontSize * 0.27))}px`;
  sub.style.color = primary;
  sub.style.textTransform = uppercase ? 'uppercase' : 'none';
  sub.style.background = styleMode === 'box_caption' ? 'rgba(0,0,0,0.72)' : 'transparent';
  sub.style.padding = styleMode === 'box_caption' ? '6px 10px' : '0';
  sub.style.borderRadius = styleMode === 'box_caption' ? '6px' : '0';
  sub.style.textShadow = styleMode === 'clean_minimal'
    ? `0 2px 4px ${outline}`
    : `-2px -2px 0 ${outline}, 2px -2px 0 ${outline}, -2px 2px 0 ${outline}, 2px 2px 0 ${outline}, 0 3px 6px rgba(0,0,0,0.85)`;

  const activeWord = document.getElementById('previewActiveWord');
  if (activeWord) {
    activeWord.style.color = (styleMode === 'word_highlight' || styleMode === 'karaoke_pop') ? highlight : primary;
    activeWord.style.display = 'inline-block';
    activeWord.style.transform = styleMode === 'karaoke_pop' ? 'scale(1.12)' : 'scale(1)';
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
      outline_width: 6,
      shadow_depth: 3,
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

/* Clipper Studio — Melhores Momentos (detecção por áudio, fala e visual)
   Usa os helpers globais do app.js: esc, showToast, openModal, closeModal, setText, switchView. */

const MOMENT_KIND_LABELS = { audio: 'Áudio', speech: 'Fala', visual: 'Visual', mixed: 'Misto' };
const MOMENT_CATEGORY_LABELS = {
  funny: 'Engraçado', rage: 'Raiva', scare: 'Susto', clutch: 'Jogada', fail: 'Falha', highlight: 'Destaque',
};

const momentsState = {
  videoId: '',
  data: null,
  kind: 'all',
  status: 'suggested',
  selected: new Set(),
  pollTimer: null,
  editing: null,
  stopAt: null,
};

function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(r).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function scoreTier(score) {
  if (score >= 75) return 'hi';
  if (score >= 50) return 'mid';
  return 'lo';
}

async function loadMomentsView() {
  try {
    const res = await fetch('/api/moments/videos');
    const videos = await res.json();
    const sel = document.getElementById('momentsVideoSelect');
    if (!sel) return;
    if (!Array.isArray(videos) || !videos.length) {
      sel.innerHTML = '<option value="">Nenhum vídeo baixado ou transcrito ainda</option>';
      momentsState.videoId = '';
      momentsState.data = null;
      renderMomentsHeat();
      renderMoments();
      return;
    }
    const prev = momentsState.videoId || sel.value;
    sel.innerHTML = videos.map((v) => {
      const n = v.moments?.suggested || 0;
      const tag = n ? ` · ${n} sugest${n === 1 ? 'ão' : 'ões'}` : '';
      return `<option value="${esc(v.id)}">${esc(String(v.title || '').slice(0, 60))} (${esc(v.channel_name)})${tag}</option>`;
    }).join('');
    let pick = prev && videos.some((v) => v.id === prev) ? prev : '';
    if (!pick) {
      const withSug = videos.find((v) => (v.moments?.suggested || 0) > 0);
      pick = (withSug || videos[0]).id;
    }
    sel.value = pick;
    await loadMomentsForVideo();
  } catch (e) {
    console.error(e);
    showToast('Não foi possível carregar os vídeos.', 'error');
  }
}

async function loadMomentsForVideo() {
  const sel = document.getElementById('momentsVideoSelect');
  const vid = sel?.value || '';
  if (vid !== momentsState.videoId) momentsState.selected.clear();
  momentsState.videoId = vid;
  if (!vid) return;
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(vid)}/moments`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (momentsState.videoId !== vid) return;
    momentsState.data = data;
    applyMomentsSettings(data.settings);
    renderMomentsJob(data);
    renderMomentsHeat();
    renderMoments();
    if (data.job?.state === 'running') scheduleMomentsPoll();
  } catch (e) {
    console.error(e);
    showToast('Erro ao carregar os momentos.', 'error');
  }
}

function applyMomentsSettings(settings) {
  if (!settings) return;
  const auto = document.getElementById('momentsAutoClip');
  const vis = document.getElementById('momentsVisualMode');
  if (auto) auto.checked = Boolean(settings.auto_clip);
  if (vis && settings.visual_mode) vis.value = settings.visual_mode;
}

async function saveMomentsSettings() {
  const payload = {
    auto_clip: Boolean(document.getElementById('momentsAutoClip')?.checked),
    visual_mode: document.getElementById('momentsVisualMode')?.value || 'auto',
  };
  try {
    const res = await fetch('/api/moments/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'erro');
    showToast(payload.auto_clip
      ? 'Modo automático ligado: a esteira cria os clipes dos melhores momentos.'
      : 'Modo manual: a esteira só sugere, você escolhe aqui.', 'success');
  } catch (e) {
    showToast(`Não foi possível salvar: ${e.message}`, 'error');
  }
}

function renderMomentsJob(data) {
  const el = document.getElementById('momentsJobStatus');
  const btn = document.getElementById('btnDetectMoments');
  const btn2 = document.getElementById('btnRedetectMoments');
  const job = data?.job || {};
  const running = job.state === 'running';
  if (btn) {
    btn.disabled = running || !data?.can_detect;
    btn.textContent = running ? 'Analisando...' : (data?.moments?.length ? 'Detectar de novo' : 'Detectar agora');
  }
  if (btn2) btn2.disabled = running || !data?.can_detect;
  if (!el) return;
  el.classList.toggle('is-running', running);
  el.classList.toggle('is-error', job.state === 'error');
  if (running) {
    el.textContent = job.message || 'Analisando...';
  } else if (job.state === 'error') {
    el.textContent = `Falhou: ${job.message || 'erro desconhecido'}`;
  } else if (!data?.can_detect) {
    el.textContent = 'Baixe ou processe o vídeo na Fila para analisar.';
  } else if (data?.curves) {
    const sig = (data.curves.signals || []).map((k) => MOMENT_KIND_LABELS[k] || k).join(', ');
    el.textContent = `${fmtClock(data.duration)} analisados · sinais: ${sig || 'nenhum'}`;
  } else {
    el.textContent = 'Ainda não analisado.';
  }
}

function scheduleMomentsPoll() {
  clearTimeout(momentsState.pollTimer);
  momentsState.pollTimer = setTimeout(async () => {
    if (currentView !== 'moments' || !momentsState.videoId) return;
    const vid = momentsState.videoId;
    try {
      const res = await fetch(`/api/videos/${encodeURIComponent(vid)}/moments`);
      const data = await res.json();
      if (momentsState.videoId !== vid) return;
      const wasRunning = momentsState.data?.job?.state === 'running';
      momentsState.data = data;
      renderMomentsJob(data);
      if (data.job?.state === 'running') {
        scheduleMomentsPoll();
      } else {
        renderMomentsHeat();
        renderMoments();
        if (wasRunning && data.job?.state === 'done') {
          showToast(data.job.message || 'Momentos detectados!', 'success');
          loadMomentsView();
        }
      }
    } catch (e) {
      scheduleMomentsPoll();
    }
  }, 2000);
}

async function detectMomentsNow(force) {
  const vid = momentsState.videoId;
  if (!vid) {
    showToast('Escolha um vídeo primeiro.', 'error');
    return;
  }
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(vid)}/moments/detect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: Boolean(force) }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || 'Não foi possível iniciar a análise.', 'error');
      return;
    }
    if (momentsState.data) momentsState.data.job = data.job || { state: 'running' };
    renderMomentsJob(momentsState.data || { job: data.job, can_detect: true });
    showToast(force ? 'Reanalisando áudio, fala e vídeo do zero...' : 'Procurando os melhores momentos...', 'info');
    scheduleMomentsPoll();
  } catch (e) {
    showToast('Erro de rede ao iniciar a análise.', 'error');
  }
}

/* ---------- Mapa de intensidade (SVG escala em qualquer largura) ---------- */

function lanePath(values) {
  const n = values.length;
  if (!n) return '';
  const w = 1000;
  const h = 100;
  const step = n > 1 ? w / (n - 1) : w;
  let d = `M0 ${h}`;
  values.forEach((v, i) => {
    const y = h - Math.max(0, Math.min(1, Number(v) || 0)) * (h - 4);
    d += ` L${(i * step).toFixed(1)} ${y.toFixed(1)}`;
  });
  d += ` L${w} ${h} Z`;
  return d;
}

function renderMomentsHeat() {
  const box = document.getElementById('momentsHeat');
  if (!box) return;
  const data = momentsState.data;
  const curves = data?.curves;
  const total = Number(data?.duration) || Number(curves?.duration) || 0;
  if (!curves || !total) {
    box.innerHTML = '<div class="moments-heat-empty">Sem análise ainda. Clique em <strong>Detectar agora</strong>.</div>';
    return;
  }
  const lanes = [
    ['audio', 'Áudio'],
    ['speech', 'Fala'],
    ['visual', 'Visual'],
  ];
  const lanesHtml = lanes.map(([key, label]) => {
    const vals = curves[key] || [];
    const body = vals.length
      ? `<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true"><path class="lane-${key}" d="${lanePath(vals)}"/></svg>`
      : '<span class="lane-off">sem sinal</span>';
    return `<div class="heat-lane"><span class="heat-label">${label}</span><div class="heat-track">${body}</div></div>`;
  }).join('');

  const pct = (t) => Math.max(0, Math.min(100, (t / total) * 100));
  const used = (data.used_segments || []).map((u) => `<span class="heat-used" style="left:${pct(u.start)}%;width:${Math.max(0.4, pct(u.end) - pct(u.start))}%"></span>`).join('');
  const marks = (data.moments || []).filter((m) => m.status !== 'dismissed').map((m) => `
    <button class="heat-mark st-${esc(m.status)}" style="left:${pct(m.start)}%;width:${Math.max(0.6, pct(m.end) - pct(m.start))}%"
      title="${esc(fmtClock(m.start))}–${esc(fmtClock(m.end))} · score ${m.score} · ${esc(m.kind_label)}"
      aria-label="Ir para o momento em ${esc(fmtClock(m.start))}" onclick="focusMomentCard(${Number(m.id)})"></button>
  `).join('');

  box.innerHTML = `
    ${lanesHtml}
    <div class="heat-lane heat-lane-marks"><span class="heat-label">Cortes</span><div class="heat-track heat-marks">${used}${marks}</div></div>
    <div class="heat-axis"><span>00:00</span><span>${esc(fmtClock(total / 2))}</span><span>${esc(fmtClock(total))}</span></div>
  `;
}

function focusMomentCard(id) {
  let card = document.getElementById(`moment-${id}`);
  if (!card) {
    const m = (momentsState.data?.moments || []).find((x) => x.id === id);
    if (m) {
      momentsState.kind = 'all';
      momentsState.status = m.status === 'covered' ? 'all' : m.status;
      document.querySelectorAll('[data-moment-kind]').forEach((b) => b.classList.toggle('active', b.dataset.momentKind === 'all'));
      document.querySelectorAll('[data-moment-status]').forEach((b) => b.classList.toggle('active', b.dataset.momentStatus === momentsState.status));
      renderMoments();
      card = document.getElementById(`moment-${id}`);
    }
  }
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('is-flash');
    setTimeout(() => card.classList.remove('is-flash'), 1400);
  }
}

/* ---------- Lista de cartões ---------- */

function setMomentKind(kind, btn) {
  momentsState.kind = kind;
  document.querySelectorAll('[data-moment-kind]').forEach((b) => b.classList.toggle('active', b === btn));
  renderMoments();
}

function setMomentStatus(status, btn) {
  momentsState.status = status;
  document.querySelectorAll('[data-moment-status]').forEach((b) => b.classList.toggle('active', b === btn));
  renderMoments();
}

function visibleMoments() {
  const all = momentsState.data?.moments || [];
  const sort = document.getElementById('momentsSort')?.value || 'score';
  const list = all.filter((m) => {
    if (momentsState.kind !== 'all' && m.kind !== momentsState.kind) return false;
    if (momentsState.status === 'all') return true;
    if (momentsState.status === 'suggested') return m.status === 'suggested' || m.status === 'covered';
    return m.status === momentsState.status;
  });
  list.sort((a, b) => (sort === 'time' ? a.start - b.start : (b.score - a.score) || (a.start - b.start)));
  return list;
}

function subBar(label, val, key) {
  const v = Math.max(0, Math.min(100, Number(val) || 0));
  return `<div class="msub"><span class="msub-label">${label}</span><span class="msub-track"><span class="msub-fill fill-${key}" style="width:${v}%"></span></span><span class="msub-val">${v}</span></div>`;
}

function renderMoments() {
  const grid = document.getElementById('momentsGrid');
  if (!grid) return;
  const data = momentsState.data;
  if (!momentsState.videoId || !data) {
    grid.innerHTML = '<div class="empty-state"><h3>Nenhum vídeo para analisar</h3><p>Importe uma URL no topo ou processe um vídeo na Fila. A análise roda sozinha depois da transcrição.</p></div>';
    updateMomentsSelectedCount();
    return;
  }
  const list = visibleMoments();
  // Seleção só vale para os que ainda podem virar clipe
  const selectable = new Set(list.filter((m) => m.status === 'suggested').map((m) => m.id));
  [...momentsState.selected].forEach((id) => { if (!selectable.has(id)) momentsState.selected.delete(id); });

  if (!list.length) {
    const none = !(data.moments || []).length;
    grid.innerHTML = none
      ? '<div class="empty-state"><h3>Nenhum momento ainda</h3><p>Clique em Detectar agora. A análise de uma live de 2h leva alguns minutos (o vídeo é lido uma vez e fica em cache).</p></div>'
      : '<div class="empty-state"><h3>Nada neste filtro</h3><p>Troque o tipo ou a situação acima.</p></div>';
    updateMomentsSelectedCount();
    return;
  }

  const hasSource = Boolean(data.has_source_video);
  grid.innerHTML = list.map((m) => {
    const tier = scoreTier(m.score);
    const checked = momentsState.selected.has(m.id) ? 'checked' : '';
    const reasons = (m.reasons || []).slice(0, 4).map((r) => `<li>${esc(r)}</li>`).join('');
    let statusBadge = '';
    let actions = '';
    if (m.status === 'accepted') {
      statusBadge = `<span class="badge badge-ok">Clipe #${Number(m.clip_id)}</span>`;
      actions = `<button class="btn btn-sm" onclick="openMomentClip(${Number(m.clip_id)})">Abrir clipe</button>`;
    } else if (m.status === 'dismissed') {
      statusBadge = '<span class="badge">Descartado</span>';
      actions = `<button class="btn btn-sm" onclick="restoreMoment(${m.id})">Restaurar</button>`;
    } else if (m.status === 'covered') {
      statusBadge = '<span class="badge badge-warn">Já coberto por um clipe</span>';
      actions = `${hasSource ? `<button class="btn btn-sm" onclick="openMomentModal(${m.id})">Ver trecho</button>` : ''}
        <button class="btn btn-ghost btn-sm" onclick="dismissMoment(${m.id})">Descartar</button>`;
    } else {
      actions = `<button class="btn btn-sm" onclick="openMomentModal(${m.id})">${hasSource ? 'Ver e ajustar' : 'Ajustar'}</button>
        <button class="btn btn-primary btn-sm" onclick="acceptMoment(${m.id})">Virar clipe</button>
        <button class="btn btn-ghost btn-sm" onclick="dismissMoment(${m.id})">Descartar</button>`;
    }
    const check = m.status === 'suggested'
      ? `<label class="moment-check" title="Selecionar"><input type="checkbox" ${checked} onchange="toggleMomentSelection(${m.id}, this.checked)" aria-label="Selecionar momento"></label>`
      : '';
    return `
      <article class="moment-card kind-${esc(m.kind)} st-${esc(m.status)}" id="moment-${m.id}">
        <div class="moment-head">
          ${check}
          <span class="moment-score tier-${tier}" title="Relevância">${Number(m.score)}</span>
          <div class="moment-time">
            <span class="input-mono">${esc(fmtClock(m.start))} – ${esc(fmtClock(m.end))}</span>
            <small>${Math.round(m.duration)}s · pico ${esc(fmtClock(m.peak))}</small>
          </div>
          <span class="badge kind-badge kb-${esc(m.kind)}">${esc(m.kind_label)}</span>
        </div>
        <h4 class="moment-title">${esc(m.label)}</h4>
        ${m.hook_text && m.hook_text !== m.label ? `<p class="moment-hook">“${esc(m.hook_text)}”</p>` : ''}
        <div class="moment-tags">
          <span class="badge">${esc(MOMENT_CATEGORY_LABELS[m.category] || m.category)}</span>
          ${statusBadge}
        </div>
        <div class="moment-subs">
          ${subBar('Áudio', m.audio_score, 'audio')}
          ${subBar('Fala', m.speech_score, 'speech')}
          ${subBar('Visual', m.visual_score, 'visual')}
        </div>
        ${reasons ? `<ul class="moment-reasons">${reasons}</ul>` : ''}
        <div class="moment-actions">${actions}</div>
      </article>
    `;
  }).join('');
  updateMomentsSelectedCount();
}

function toggleMomentSelection(id, on) {
  if (on) momentsState.selected.add(id);
  else momentsState.selected.delete(id);
  updateMomentsSelectedCount();
}

function selectAllMoments() {
  visibleMoments().filter((m) => m.status === 'suggested').forEach((m) => momentsState.selected.add(m.id));
  renderMoments();
}

function updateMomentsSelectedCount() {
  const n = momentsState.selected.size;
  setText('momentsSelectedCount', `${n} selecionado(s)`);
  const btn = document.getElementById('btnAcceptMomentsBatch');
  if (btn) btn.disabled = n === 0;
}

/* ---------- Ações ---------- */

async function postMomentAction(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* sem corpo */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function acceptMoment(id, overrides) {
  try {
    const data = await postMomentAction(`/api/moments/${id}/accept`, overrides || {});
    showToast(data.message || 'Momento virou clipe!', 'success');
    momentsState.selected.delete(id);
    await loadMomentsForVideo();
    if (typeof fetchStatus === 'function') fetchStatus();
    return true;
  } catch (e) {
    showToast(e.message, 'error');
    return false;
  }
}

async function acceptSelectedMoments() {
  const ids = [...momentsState.selected];
  if (!ids.length) return;
  const btn = document.getElementById('btnAcceptMomentsBatch');
  if (btn) btn.disabled = true;
  try {
    const data = await postMomentAction('/api/moments/accept-batch', { ids });
    const ok = (data.created || []).length;
    const bad = (data.failed || []).length;
    showToast(bad ? `${ok} clipe(s) criados, ${bad} bateram em cortes já existentes.` : `${ok} clipe(s) criados!`, bad ? 'info' : 'success');
    momentsState.selected.clear();
    await loadMomentsForVideo();
    if (typeof fetchStatus === 'function') fetchStatus();
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    updateMomentsSelectedCount();
  }
}

async function dismissMoment(id) {
  try {
    await postMomentAction(`/api/moments/${id}/dismiss`);
    momentsState.selected.delete(id);
    await loadMomentsForVideo();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function restoreMoment(id) {
  try {
    await postMomentAction(`/api/moments/${id}/restore`);
    await loadMomentsForVideo();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function dismissRemainingMoments() {
  const vid = momentsState.videoId;
  if (!vid) return;
  const left = (momentsState.data?.moments || []).filter((m) => m.status === 'suggested' || m.status === 'covered').length;
  if (!left) {
    showToast('Não há sugestões pendentes neste vídeo.', 'info');
    return;
  }
  try {
    const data = await postMomentAction(`/api/videos/${encodeURIComponent(vid)}/moments/dismiss-rest`);
    showToast(`${data.dismissed || 0} sugestão(ões) descartada(s).`, 'success');
    momentsState.selected.clear();
    await loadMomentsForVideo();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function openMomentClip(clipId) {
  switchView('clips');
  if (typeof openEditClipModal === 'function') setTimeout(() => openEditClipModal(clipId), 300);
}

/* ---------- Modal: ver, ajustar e virar clipe ---------- */

function openMomentModal(id) {
  const m = (momentsState.data?.moments || []).find((x) => x.id === id);
  if (!m) return;
  momentsState.editing = m;
  document.getElementById('momentEditTitle').value = m.label || '';
  document.getElementById('momentEditStart').value = m.start;
  document.getElementById('momentEditEnd').value = m.end;
  document.getElementById('momentEditCrop').value = '';
  const err = document.getElementById('momentEditError');
  err.hidden = true;
  err.textContent = '';
  const canAccept = m.status === 'suggested';
  const acceptBtn = document.getElementById('btnMomentModalAccept');
  acceptBtn.hidden = !canAccept;
  setText('momentModalTitle', canAccept ? 'Ajustar momento e virar clipe' : 'Ver momento');

  const wrap = document.getElementById('momentPreviewWrap');
  const missing = document.getElementById('momentPreviewMissing');
  const video = document.getElementById('momentPreviewVideo');
  const url = momentsState.data?.source_stream_url;
  if (url) {
    wrap.hidden = false;
    missing.hidden = true;
    if (video.dataset.src !== url) {
      video.src = url;
      video.dataset.src = url;
    }
    const seek = () => { try { video.currentTime = m.start; } catch (e) { /* ainda carregando */ } };
    if (video.readyState >= 1) seek();
    else video.addEventListener('loadedmetadata', seek, { once: true });
  } else {
    wrap.hidden = true;
    missing.hidden = false;
  }
  updateMomentDuration();
  openModal('momentModal');
}

function closeMomentModal() {
  const video = document.getElementById('momentPreviewVideo');
  if (video) video.pause();
  momentsState.stopAt = null;
  momentsState.editing = null;
  closeModal('momentModal');
}

function updateMomentDuration() {
  const s = parseFloat(document.getElementById('momentEditStart').value);
  const e = parseFloat(document.getElementById('momentEditEnd').value);
  const el = document.getElementById('momentEditDuration');
  if (!el) return;
  if (Number.isFinite(s) && Number.isFinite(e) && e > s) {
    const d = e - s;
    el.textContent = `${fmtClock(s)} – ${fmtClock(e)} · ${d.toFixed(1)}s${d > 60 ? ' (passa de 60s, o Shorts não aceita)' : ''}`;
    el.classList.toggle('form-error', d > 60);
  } else {
    el.textContent = 'O fim precisa ser depois do início.';
    el.classList.add('form-error');
  }
}

function playMomentRange() {
  const video = document.getElementById('momentPreviewVideo');
  const s = parseFloat(document.getElementById('momentEditStart').value);
  const e = parseFloat(document.getElementById('momentEditEnd').value);
  if (!video || !Number.isFinite(s)) return;
  momentsState.stopAt = Number.isFinite(e) ? e : null;
  try { video.currentTime = s; } catch (err) { /* ignora */ }
  video.play().catch(() => {});
}

function setMomentEdgeFromVideo(edge) {
  const video = document.getElementById('momentPreviewVideo');
  if (!video) return;
  const t = Math.round(video.currentTime * 2) / 2;
  document.getElementById(edge === 'start' ? 'momentEditStart' : 'momentEditEnd').value = t;
  updateMomentDuration();
}

async function acceptMomentFromModal() {
  const m = momentsState.editing;
  if (!m) return;
  const err = document.getElementById('momentEditError');
  const s = parseFloat(document.getElementById('momentEditStart').value);
  const e = parseFloat(document.getElementById('momentEditEnd').value);
  if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) {
    err.textContent = 'Confira início e fim.';
    err.hidden = false;
    return;
  }
  const body = {
    start_time: s,
    end_time: e,
    title: document.getElementById('momentEditTitle').value.trim(),
  };
  const crop = document.getElementById('momentEditCrop').value;
  if (crop) body.crop_mode = crop;
  try {
    const data = await postMomentAction(`/api/moments/${m.id}/accept`, body);
    showToast(data.message || 'Momento virou clipe!', 'success');
    closeMomentModal();
    await loadMomentsForVideo();
    if (typeof fetchStatus === 'function') fetchStatus();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const video = document.getElementById('momentPreviewVideo');
  if (video) {
    video.addEventListener('timeupdate', () => {
      if (momentsState.stopAt != null && video.currentTime >= momentsState.stopAt) {
        video.pause();
        momentsState.stopAt = null;
      }
    });
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && document.getElementById('momentModal')?.classList.contains('open')) closeMomentModal();
  });
});

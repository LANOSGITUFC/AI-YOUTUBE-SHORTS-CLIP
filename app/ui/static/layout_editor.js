/* Clipper Studio — Editor visual de layout do Short (1080×1920)
 *
 * Painel 1 (vídeo original): o usuário marca a área da câmera/facecam e a área da gameplay.
 * Painel 2 (Short 9:16): arrasta e redimensiona câmera, gameplay e legenda no quadro final.
 *
 * Arrastar/redimensionar usa Pointer Events (mouse, caneta e toque com o mesmo código).
 * O layout é salvo em editor_config.layout e o render (app/services/layout_engine.py)
 * reproduz exatamente o que a pré-visualização desenha: cada recorte "cobre" sua caixa.
 */
(function () {
  'use strict';

  const CW = 1080;
  const CH = 1920;
  const MIN_SRC = 0.05;
  const MIN_DST = 64;
  const MIN_CAP_W = 200;
  const MIN_CAP_H = 60;
  const SNAP = 14; // unidades do quadro 1080×1920

  const LABELS = { cam: 'Câmera', game: 'Gameplay', caption: 'Legenda' };

  const PRESET_NAMES = {
    cam_top: 'Câmera em cima',
    cam_bottom: 'Câmera embaixo',
    cam_float: 'Câmera flutuante',
    game_full: 'Só gameplay (cheia)',
    game_fit: 'Gameplay inteira',
  };

  const state = {
    clip: null,
    layout: null,
    savedLayouts: [],
    selected: { space: 'dst', el: 'game' },
    img: null,
    imgAspect: 16 / 9,
    keepAspect: false,
    drag: null,
    root: null,
  };

  /* ---------- utilidades ---------- */

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const round4 = (v) => Math.round(v * 10000) / 10000;
  const even = (v) => Math.max(2, Math.round(v / 2) * 2);
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const $ = (sel) => state.root.querySelector(sel);

  function toast(msg, type) {
    if (typeof window.showToast === 'function') window.showToast(msg, type || 'info');
  }

  function escHtml(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------- presets ---------- */

  // Recorte 9:16 central da fonte (gameplay em tela cheia)
  function centerVerticalSrc() {
    const w = clamp((9 / 16) / state.imgAspect, MIN_SRC, 1);
    return { x: round4((1 - w) / 2), y: 0, w: round4(w), h: 1 };
  }

  function defaultLayout() {
    return {
      name: 'Câmera em cima',
      enabled: true,
      has_cam: true,
      background: 'blur',
      cam: { src: { x: 0.7, y: 0, w: 0.3, h: 0.4 }, dst: { x: 0, y: 0, w: 1080, h: 760 } },
      game: { src: { x: 0.1875, y: 0, w: 0.625, h: 1 }, dst: { x: 0, y: 760, w: 1080, h: 1160 } },
      caption: { x: 60, y: 1380, w: 960, h: 220 },
    };
  }

  const PRESETS = {
    cam_top: (L) => {
      L.name = PRESET_NAMES.cam_top;
      L.has_cam = true;
      L.cam.dst = { x: 0, y: 0, w: 1080, h: 760 };
      L.game.dst = { x: 0, y: 760, w: 1080, h: 1160 };
      L.caption = { x: 60, y: 1380, w: 960, h: 220 };
    },
    cam_bottom: (L) => {
      L.name = PRESET_NAMES.cam_bottom;
      L.has_cam = true;
      L.game.dst = { x: 0, y: 0, w: 1080, h: 1160 };
      L.cam.dst = { x: 0, y: 1160, w: 1080, h: 760 };
      L.caption = { x: 60, y: 880, w: 960, h: 220 };
    },
    cam_float: (L) => {
      L.name = PRESET_NAMES.cam_float;
      L.has_cam = true;
      L.game.src = centerVerticalSrc();
      L.game.dst = { x: 0, y: 0, w: 1080, h: 1920 };
      L.cam.dst = { x: 620, y: 140, w: 400, h: 400 };
      L.caption = { x: 60, y: 1380, w: 960, h: 220 };
    },
    game_full: (L) => {
      L.name = PRESET_NAMES.game_full;
      L.has_cam = false;
      L.game.src = centerVerticalSrc();
      L.game.dst = { x: 0, y: 0, w: 1080, h: 1920 };
      L.caption = { x: 60, y: 1380, w: 960, h: 220 };
    },
    game_fit: (L) => {
      L.name = PRESET_NAMES.game_fit;
      L.has_cam = false;
      L.game.src = { x: 0, y: 0, w: 1, h: 1 };
      const h = even(1080 / state.imgAspect);
      L.game.dst = { x: 0, y: Math.round((CH - h) / 2), w: 1080, h };
      L.caption = { x: 60, y: Math.min(CH - 260, L.game.dst.y + h + 40), w: 960, h: 220 };
    },
  };

  /* ---------- normalização (espelha layout_engine.py) ---------- */

  function normSrc(r) {
    const w = clamp(+r.w || 0, MIN_SRC, 1);
    const h = clamp(+r.h || 0, MIN_SRC, 1);
    return { x: round4(clamp(+r.x || 0, 0, 1 - w)), y: round4(clamp(+r.y || 0, 0, 1 - h)), w: round4(w), h: round4(h) };
  }

  function normDst(r, minW, minH) {
    const w = even(clamp(+r.w || 0, minW, CW));
    const h = even(clamp(+r.h || 0, minH, CH));
    return { x: Math.round(clamp(+r.x || 0, 0, CW - w)), y: Math.round(clamp(+r.y || 0, 0, CH - h)), w, h };
  }

  function normalize(L) {
    const d = defaultLayout();
    const src = L && typeof L === 'object' ? L : {};
    const out = {
      name: String(src.name || '').trim().slice(0, 80) || 'Layout personalizado',
      enabled: true,
      has_cam: src.has_cam !== false,
      background: src.background === 'black' ? 'black' : 'blur',
      cam: {
        src: normSrc(Object.assign({}, d.cam.src, (src.cam || {}).src)),
        dst: normDst(Object.assign({}, d.cam.dst, (src.cam || {}).dst), MIN_DST, MIN_DST),
      },
      game: {
        src: normSrc(Object.assign({}, d.game.src, (src.game || {}).src)),
        dst: normDst(Object.assign({}, d.game.dst, (src.game || {}).dst), MIN_DST, MIN_DST),
      },
      caption: normDst(Object.assign({}, d.caption, src.caption), MIN_CAP_W, MIN_CAP_H),
    };
    return out;
  }

  /* ---------- acesso aos retângulos ---------- */

  function getRect(space, el) {
    const L = state.layout;
    if (el === 'caption') return L.caption;
    return L[el][space];
  }

  function setRect(space, el, r) {
    const L = state.layout;
    if (el === 'caption') L.caption = r;
    else L[el][space] = r;
  }

  function bounds(space) {
    return space === 'src' ? { W: 1, H: 1 } : { W: CW, H: CH };
  }

  function minSize(space, el) {
    if (space === 'src') return { w: MIN_SRC, h: MIN_SRC };
    if (el === 'caption') return { w: MIN_CAP_W, h: MIN_CAP_H };
    return { w: MIN_DST, h: MIN_DST };
  }

  /* ---------- montagem do DOM ---------- */

  function buildModal() {
    const root = document.createElement('div');
    root.className = 'le-overlay';
    root.id = 'layoutEditorModal';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.innerHTML = `
      <div class="le-card">
        <div class="le-head">
          <h3 id="leTitle">Editor de layout</h3>
          <button class="btn btn-ghost btn-sm" data-act="close" aria-label="Fechar">✕</button>
        </div>
        <div class="le-toolbar">
          <div class="le-presets" role="group" aria-label="Modelos de layout">
            <button class="btn btn-sm" data-preset="cam_top">Câmera em cima</button>
            <button class="btn btn-sm" data-preset="cam_bottom">Câmera embaixo</button>
            <button class="btn btn-sm" data-preset="cam_float">Câmera flutuante</button>
            <button class="btn btn-sm" data-preset="game_full">Só gameplay (cheia)</button>
            <button class="btn btn-sm" data-preset="game_fit">Gameplay inteira</button>
          </div>
          <div class="le-options" style="width:100%; justify-content:space-between; padding-top:6px; border-top:1px solid var(--border-subtle);">
            <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
              <label class="le-check">Nome do layout
                <input type="text" id="leLayoutName" class="input" style="width:185px; height:28px; padding:2px 8px; font-size:12px;" placeholder="Ex: Alanzoka Topo">
              </label>
              <button class="btn btn-sm" data-act="save-template" title="Salva este layout com nome para reutilizar em outros clipes">Salvar modelo</button>
              <select id="leSavedLayouts" class="select" style="width:185px; height:28px; padding:2px 8px; font-size:12px;">
                <option value="">Meus layouts salvos...</option>
              </select>
              <button class="btn btn-danger btn-sm" data-act="delete-template" id="leDeleteTemplateBtn" style="display:none;" title="Excluir modelo selecionado">Excluir</button>
            </div>
            <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
              <label class="le-check"><input type="checkbox" id="leHasCam"> Vídeo tem câmera</label>
              <label class="le-check"><input type="checkbox" id="leKeepAspect"> Manter proporção</label>
              <label class="le-check">Fundo
                <select id="leBackground"><option value="blur">Desfocado</option><option value="black">Preto</option></select>
              </label>
              <button class="btn btn-sm" data-act="detect" id="leDetectBtn">Detectar facecam</button>
            </div>
          </div>
        </div>
        <div class="le-body">
          <section class="le-pane le-pane-src">
            <div class="le-pane-title"><span class="le-step">1</span> Vídeo original: marque a câmera e a gameplay</div>
            <div class="le-stage le-src" id="leSrcStage" data-space="src">
              <img id="leSrcImg" alt="Quadro do vídeo original" draggable="false">
              <div class="le-src-empty" id="leSrcEmpty">Sem quadro do vídeo original (não baixado). Você ainda pode ajustar as áreas.</div>
            </div>
            <div class="le-time">
              <label for="leTime">Quadro em</label>
              <input type="range" id="leTime" min="0" max="1" step="0.1" value="0">
              <span id="leTimeLabel">0:00</span>
            </div>
          </section>
          <section class="le-pane le-pane-dst">
            <div class="le-pane-title"><span class="le-step">2</span> Short 1080×1920: arraste e redimensione</div>
            <div class="le-stage le-dst" id="leDstStage" data-space="dst">
              <canvas id="leCanvas" width="540" height="960"></canvas>
              <div class="le-guide le-guide-v" id="leGuideV"></div>
              <div class="le-guide le-guide-h" id="leGuideH"></div>
            </div>
          </section>
          <aside class="le-pane le-inspector">
            <div class="le-pane-title">Elemento selecionado</div>
            <div class="le-sel-tabs" role="tablist">
              <button class="le-tab" data-select="game">Gameplay</button>
              <button class="le-tab" data-select="cam">Câmera</button>
              <button class="le-tab" data-select="caption">Legenda</button>
            </div>
            <div class="le-space-tabs" role="tablist" id="leSpaceTabs">
              <button class="le-tab" data-space-select="dst">Posição no Short</button>
              <button class="le-tab" data-space-select="src">Área no vídeo original</button>
            </div>
            <div class="le-sel-name" id="leSelName"></div>
            <div class="le-fields">
              <label>X <input type="number" id="leFx" step="1"></label>
              <label>Y <input type="number" id="leFy" step="1"></label>
              <label>Largura <input type="number" id="leFw" step="1"></label>
              <label>Altura <input type="number" id="leFh" step="1"></label>
            </div>
            <div class="le-size-row" id="leSizeRow">
              <label for="leSize">Tamanho</label>
              <input type="range" id="leSize" min="10" max="100" step="1">
              <span id="leSizeLabel"></span>
            </div>
            <div class="le-quick">
              <button class="btn btn-sm" data-act="center-x">Centralizar</button>
              <button class="btn btn-sm" data-act="full-width">Largura total</button>
              <button class="btn btn-sm" data-act="fit-src">Proporção da fonte</button>
            </div>
            <p class="le-hint">Dica: toque ou clique num elemento para selecionar; arraste para mover e use as alças dos cantos para redimensionar. Setas do teclado movem 1 px (Shift: 10 px).</p>
          </aside>
        </div>
        <div class="le-foot">
          <button class="btn btn-ghost btn-sm" data-act="auto">Usar layout automático</button>
          <span class="le-spacer"></span>
          <button class="btn btn-sm" data-act="close">Cancelar</button>
          <button class="btn btn-sm" data-act="save">Salvar layout</button>
          <button class="btn btn-primary btn-sm" data-act="save-render">Salvar e renderizar</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    state.root = root;

    // caixas arrastáveis
    ['game', 'cam'].forEach((el) => {
      $('#leSrcStage').appendChild(makeBox('src', el));
    });
    ['game', 'cam', 'caption'].forEach((el) => {
      $('#leDstStage').appendChild(makeBox('dst', el));
    });

    root.addEventListener('click', onClick);
    root.addEventListener('keydown', onKey);
    $('#leHasCam').addEventListener('change', (e) => {
      state.layout.has_cam = e.target.checked;
      if (!state.layout.has_cam && state.selected.el === 'cam') select('dst', 'game');
      refresh();
    });
    $('#leKeepAspect').addEventListener('change', (e) => { state.keepAspect = e.target.checked; });
    $('#leBackground').addEventListener('change', (e) => { state.layout.background = e.target.value; refresh(); });
    $('#leLayoutName').addEventListener('input', (e) => { state.layout.name = e.target.value.trim() || 'Layout personalizado'; });
    $('#leSavedLayouts').addEventListener('change', (e) => {
      const id = e.target.value;
      const delBtn = $('#leDeleteTemplateBtn');
      if (delBtn) delBtn.style.display = id ? 'inline-flex' : 'none';
      if (!id) return;
      const found = state.savedLayouts.find((x) => x.id === id);
      if (found && found.layout) {
        state.layout = normalize(clone(found.layout));
        state.layout.name = found.name;
        toast(`Layout '${found.name}' carregado!`, 'success');
        select('dst', state.layout.has_cam ? 'cam' : 'game');
      }
    });
    $('#leTime').addEventListener('input', () => { $('#leTimeLabel').textContent = fmt(+$('#leTime').value); });
    $('#leTime').addEventListener('change', () => loadFrame(+$('#leTime').value));
    ['leFx', 'leFy', 'leFw', 'leFh'].forEach((id) => $('#' + id).addEventListener('change', onFieldChange));
    $('#leSize').addEventListener('input', onSizeSlider);

    const img = $('#leSrcImg');
    img.addEventListener('load', () => {
      state.imgAspect = img.naturalWidth / img.naturalHeight || 16 / 9;
      $('#leSrcStage').style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
      $('#leSrcEmpty').hidden = true;
      img.hidden = false;
      state.img = img;
      refresh();
    });
    img.addEventListener('error', () => {
      state.img = null;
      img.hidden = true;
      $('#leSrcEmpty').hidden = false;
      refresh();
    });

    if (window.ResizeObserver) {
      new ResizeObserver(() => refresh()).observe($('#leDstStage'));
    }
  }

  function makeBox(space, el) {
    const box = document.createElement('div');
    box.className = `le-box le-${el}`;
    box.dataset.space = space;
    box.dataset.el = el;
    box.tabIndex = 0;
    box.setAttribute('aria-label', `${LABELS[el]} (${space === 'src' ? 'área no vídeo original' : 'posição no Short'})`);
    let inner = `<span class="le-box-label">${LABELS[el]}</span>`;
    if (el === 'caption') inner += '<div class="le-caption-text"></div>';
    ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach((h) => {
      inner += `<span class="le-handle le-h-${h}" data-handle="${h}"></span>`;
    });
    box.innerHTML = inner;
    box.addEventListener('pointerdown', onPointerDown);
    return box;
  }

  /* ---------- arrastar / redimensionar (Pointer Events) ---------- */

  function onPointerDown(e) {
    if (e.button !== undefined && e.button !== 0) return;
    const box = e.currentTarget;
    const space = box.dataset.space;
    const el = box.dataset.el;
    const handle = e.target.dataset && e.target.dataset.handle ? e.target.dataset.handle : null;
    select(space, el);
    const stage = box.parentElement.getBoundingClientRect();
    const { W, H } = bounds(space);
    state.drag = {
      id: e.pointerId,
      box,
      space,
      el,
      handle,
      startX: e.clientX,
      startY: e.clientY,
      scaleX: W / stage.width,
      scaleY: H / stage.height,
      start: Object.assign({}, getRect(space, el)),
      aspect: null,
    };
    const r = state.drag.start;
    state.drag.aspect = (r.w * (space === 'src' ? state.imgAspect : 1)) / (r.h || 1);
    try { box.setPointerCapture(e.pointerId); } catch (_) { /* ok */ }
    box.addEventListener('pointermove', onPointerMove);
    box.addEventListener('pointerup', onPointerUp);
    box.addEventListener('pointercancel', onPointerUp);
    box.classList.add('le-dragging');
    e.preventDefault();
    e.stopPropagation();
  }

  function onPointerMove(e) {
    const d = state.drag;
    if (!d || e.pointerId !== d.id) return;
    const dx = (e.clientX - d.startX) * d.scaleX;
    const dy = (e.clientY - d.startY) * d.scaleY;
    const { W, H } = bounds(d.space);
    const min = minSize(d.space, d.el);
    const s = d.start;
    let r;
    let guides = { v: null, h: null };

    if (!d.handle) {
      r = { x: clamp(s.x + dx, 0, W - s.w), y: clamp(s.y + dy, 0, H - s.h), w: s.w, h: s.h };
      if (d.space === 'dst') guides = snapMove(r, d.el);
    } else {
      r = resizeRect(s, d.handle, dx, dy, W, H, min, state.keepAspect || e.shiftKey, d.aspect, d.space);
    }
    setRect(d.space, d.el, finalize(d.space, r));
    showGuides(guides);
    refresh();
  }

  function onPointerUp(e) {
    const d = state.drag;
    if (!d || e.pointerId !== d.id) return;
    d.box.removeEventListener('pointermove', onPointerMove);
    d.box.removeEventListener('pointerup', onPointerUp);
    d.box.removeEventListener('pointercancel', onPointerUp);
    d.box.classList.remove('le-dragging');
    try { d.box.releasePointerCapture(e.pointerId); } catch (_) { /* ok */ }
    state.drag = null;
    showGuides({ v: null, h: null });
    refresh();
  }

  function resizeRect(s, handle, dx, dy, W, H, min, keep, aspect, space) {
    let x1 = s.x;
    let y1 = s.y;
    let x2 = s.x + s.w;
    let y2 = s.y + s.h;
    if (handle.includes('w')) x1 = clamp(s.x + dx, 0, x2 - min.w);
    if (handle.includes('e')) x2 = clamp(x2 + dx, x1 + min.w, W);
    if (handle.includes('n')) y1 = clamp(s.y + dy, 0, y2 - min.h);
    if (handle.includes('s')) y2 = clamp(y2 + dy, y1 + min.h, H);

    if (keep && aspect > 0) {
      // aspecto em "pixels reais" (na fonte, x é fração da largura e y da altura)
      const k = space === 'src' ? state.imgAspect : 1;
      let w = x2 - x1;
      let h = y2 - y1;
      const horizontalOnly = handle === 'e' || handle === 'w';
      const verticalOnly = handle === 'n' || handle === 's';
      if (horizontalOnly) h = (w * k) / aspect;
      else if (verticalOnly) w = (h * aspect) / k;
      else if ((w * k) / h > aspect) w = (h * aspect) / k;
      else h = (w * k) / aspect;
      // limita ao quadro mantendo a proporção
      const maxW = handle.includes('w') ? x2 : W - x1;
      const maxH = handle.includes('n') ? y2 : H - y1;
      const f = Math.min(1, maxW / w, maxH / h);
      w *= f; h *= f;
      if (handle.includes('w')) x1 = x2 - w; else x2 = x1 + w;
      if (handle.includes('n')) y1 = y2 - h; else y2 = y1 + h;
      if (verticalOnly) { const cx = s.x + s.w / 2; x1 = clamp(cx - w / 2, 0, W - w); x2 = x1 + w; }
      if (horizontalOnly) { const cy = s.y + s.h / 2; y1 = clamp(cy - h / 2, 0, H - h); y2 = y1 + h; }
    }
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  }

  function finalize(space, r) {
    if (space === 'src') {
      return { x: round4(r.x), y: round4(r.y), w: round4(r.w), h: round4(r.h) };
    }
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
  }

  function snapMove(r, el) {
    const L = state.layout;
    const xs = [0, CW / 2, CW];
    const ys = [0, CH / 2, CH];
    ['game', 'cam', 'caption'].forEach((o) => {
      if (o === el || (o === 'cam' && !L.has_cam)) return;
      const q = getRect('dst', o);
      xs.push(q.x, q.x + q.w, q.x + q.w / 2);
      ys.push(q.y, q.y + q.h, q.y + q.h / 2);
    });
    const guides = { v: null, h: null };
    let best = SNAP + 1;
    [[0, r.x], [r.w / 2, r.x + r.w / 2], [r.w, r.x + r.w]].forEach(([off, val]) => {
      xs.forEach((t) => {
        const dd = Math.abs(val - t);
        if (dd < best) { best = dd; r.x = clamp(t - off, 0, CW - r.w); guides.v = t; }
      });
    });
    best = SNAP + 1;
    [[0, r.y], [r.h / 2, r.y + r.h / 2], [r.h, r.y + r.h]].forEach(([off, val]) => {
      ys.forEach((t) => {
        const dd = Math.abs(val - t);
        if (dd < best) { best = dd; r.y = clamp(t - off, 0, CH - r.h); guides.h = t; }
      });
    });
    return guides;
  }

  function showGuides(g) {
    const gv = $('#leGuideV');
    const gh = $('#leGuideH');
    gv.hidden = g.v === null;
    gh.hidden = g.h === null;
    if (g.v !== null) gv.style.left = `${(g.v / CW) * 100}%`;
    if (g.h !== null) gh.style.top = `${(g.h / CH) * 100}%`;
  }

  /* ---------- seleção, inspetor e teclado ---------- */

  function select(space, el) {
    if (el === 'caption') space = 'dst';
    state.selected = { space, el };
    refresh();
  }

  function onFieldChange() {
    const { space, el } = state.selected;
    const f = space === 'src' ? 100 : 1; // na fonte os campos são em %
    const r = {
      x: (+$('#leFx').value || 0) / f,
      y: (+$('#leFy').value || 0) / f,
      w: (+$('#leFw').value || 0) / f,
      h: (+$('#leFh').value || 0) / f,
    };
    applyRect(space, el, r);
  }

  function applyRect(space, el, r) {
    const { W, H } = bounds(space);
    const min = minSize(space, el);
    const w = clamp(r.w, min.w, W);
    const h = clamp(r.h, min.h, H);
    setRect(space, el, finalize(space, { x: clamp(r.x, 0, W - w), y: clamp(r.y, 0, H - h), w, h }));
    refresh();
  }

  // Slider de tamanho: escala o elemento a partir do centro, mantendo a proporção
  function onSizeSlider() {
    const { space, el } = state.selected;
    const { W, H } = bounds(space);
    const r = getRect(space, el);
    const pct = +$('#leSize').value / 100;
    const ratio = r.h / r.w;
    let w = W * pct;
    let h = w * ratio;
    if (h > H) { h = H; w = h / ratio; }
    const cx = r.x + r.w / 2;
    const cy = r.y + r.h / 2;
    applyRect(space, el, { x: cx - w / 2, y: cy - h / 2, w, h });
  }

  function onKey(e) {
    if (e.key === 'Escape') { close(); return; }
    const t = e.target;
    if (!t.classList || !t.classList.contains('le-box')) return;
    const steps = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (!steps[e.key]) return;
    e.preventDefault();
    const space = t.dataset.space;
    const el = t.dataset.el;
    select(space, el);
    const mult = (e.shiftKey ? 10 : 1) * (space === 'src' ? 0.005 : 1);
    const r = Object.assign({}, getRect(space, el));
    r.x += steps[e.key][0] * mult;
    r.y += steps[e.key][1] * mult;
    applyRect(space, el, r);
  }

  /* ---------- ações ---------- */

  async function onClick(e) {
    if (e.target === state.root) { close(); return; }
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.dataset.preset) {
      PRESETS[btn.dataset.preset](state.layout);
      state.layout = normalize(state.layout);
      select('dst', state.layout.has_cam ? 'cam' : 'game');
      return;
    }
    if (btn.dataset.spaceSelect) {
      if (state.selected.el !== 'caption') select(btn.dataset.spaceSelect, state.selected.el);
      return;
    }
    if (btn.dataset.select) {
      const el = btn.dataset.select;
      if (el === 'cam' && !state.layout.has_cam) {
        state.layout.has_cam = true;
      }
      select(state.selected.space === 'src' && el !== 'caption' ? 'src' : 'dst', el);
      return;
    }
    const act = btn.dataset.act;
    const { space, el } = state.selected;
    const { W } = bounds(space);
    const r = Object.assign({}, getRect(space, el));
    switch (act) {
      case 'close': close(); break;
      case 'detect': await detectFacecam(); break;
      case 'center-x': applyRect(space, el, Object.assign(r, { x: (W - r.w) / 2 })); break;
      case 'full-width': applyRect(space, el, Object.assign(r, { x: 0, w: W })); break;
      case 'fit-src': fitToSource(); break;
      case 'save-template': await saveNamedTemplate(); break;
      case 'delete-template': await deleteNamedTemplate(); break;
      case 'auto': await save(false, false); break;
      case 'save': await save(false, true); break;
      case 'save-render': await save(true, true); break;
      default: break;
    }
  }

  async function loadSavedLayouts() {
    try {
      const res = await fetch('/api/layouts');
      if (!res.ok) return;
      const data = await res.json();
      state.savedLayouts = data.layouts || [];
      const sel = $('#leSavedLayouts');
      if (sel) {
        const prev = sel.value;
        sel.innerHTML = '<option value="">Meus layouts salvos...</option>' +
          state.savedLayouts.map((item) => `<option value="${escHtml(item.id)}">${escHtml(item.name)}</option>`).join('');
        if (prev && state.savedLayouts.some((x) => x.id === prev)) sel.value = prev;
      }
      const delBtn = $('#leDeleteTemplateBtn');
      if (delBtn && sel) delBtn.style.display = sel.value ? 'inline-flex' : 'none';
    } catch (_) { /* ok */ }
  }

  async function saveNamedTemplate() {
    const input = $('#leLayoutName');
    const name = String(input && input.value ? input.value : state.layout.name || '').trim();
    if (!name) {
      toast('Digite um nome para salvar o modelo de layout.', 'error');
      if (input) input.focus();
      return;
    }
    state.layout.name = name;
    try {
      const res = await fetch('/api/layouts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, layout: normalize(state.layout) }),
      });
      const data = await res.json();
      if (!res.ok) { toast(data.detail || 'Erro ao salvar modelo.', 'error'); return; }
      toast(`Modelo '${data.name}' salvo! Agora você pode reutilizar em qualquer clipe.`, 'success');
      await loadSavedLayouts();
      const sel = $('#leSavedLayouts');
      if (sel && data.id) {
        sel.value = data.id;
        const delBtn = $('#leDeleteTemplateBtn');
        if (delBtn) delBtn.style.display = 'inline-flex';
      }
    } catch (_) {
      toast('Erro de rede ao salvar modelo.', 'error');
    }
  }

  async function deleteNamedTemplate() {
    const sel = $('#leSavedLayouts');
    const id = sel ? sel.value : '';
    if (!id) return;
    if (!confirm('Excluir este modelo de layout salvo?')) return;
    try {
      const res = await fetch(`/api/layouts/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (res.ok) {
        toast('Modelo de layout excluído.', 'success');
        if (sel) sel.value = '';
        await loadSavedLayouts();
      }
    } catch (_) {
      toast('Erro ao excluir modelo.', 'error');
    }
  }

  // Ajusta a caixa no Short para a proporção do recorte da fonte (sem cortar nada)
  function fitToSource() {
    const el = state.selected.el;
    if (el === 'caption') return;
    const src = state.layout[el].src;
    const ratio = (src.h) / (src.w * state.imgAspect); // altura/largura em pixels reais
    const r = state.layout[el].dst;
    let w = r.w;
    let h = w * ratio;
    if (h > CH) { h = CH; w = h / ratio; }
    applyRect('dst', el, { x: r.x + (r.w - w) / 2, y: r.y, w, h });
  }

  async function detectFacecam() {
    const btn = $('#leDetectBtn');
    btn.disabled = true;
    btn.textContent = 'Detectando...';
    try {
      const res = await fetch(`/api/clips/${state.clip.id}/detect-facecam`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) { toast(data.detail || 'Não foi possível detectar a facecam.', 'error'); return false; }
      state.layout.cam.src = normSrc(data.src);
      state.layout.has_cam = true;
      select('src', 'cam');
      const engineInfo = data.engine ? ` (${data.corner || 'webcam'} · ${data.engine})` : '';
      toast(
        data.detected
          ? `Facecam detectada com sucesso${engineInfo}!`
          : 'Nenhum rosto encontrado neste trecho: marquei o canto padrão, ajuste à mão.',
        data.detected ? 'success' : 'info'
      );
      return data.detected;
    } catch (_) {
      toast('Erro de rede ao detectar a facecam.', 'error');
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Detectar facecam';
    }
  }

  async function save(renderAfter, enabled) {
    const layout = enabled ? normalize(state.layout) : Object.assign(normalize(state.layout), { enabled: false });
    try {
      const res = await fetch(`/api/clips/${state.clip.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ editor_config: { layout } }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(data.detail || 'Erro ao salvar o layout.', 'error'); return; }
      toast(enabled ? 'Layout salvo.' : 'Layout personalizado desligado: o clipe volta ao layout automático.', 'success');
      const id = state.clip.id;
      close();
      if (typeof window.loadClips === 'function') await window.loadClips();
      if (renderAfter && typeof window.renderClip === 'function') await window.renderClip(id);
    } catch (_) {
      toast('Erro de rede ao salvar o layout.', 'error');
    }
  }

  /* ---------- desenho ---------- */

  function fmt(sec) {
    const s = Math.max(0, Math.floor(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function placeBox(box, r, space) {
    const { W, H } = bounds(space);
    box.style.left = `${(r.x / W) * 100}%`;
    box.style.top = `${(r.y / H) * 100}%`;
    box.style.width = `${(r.w / W) * 100}%`;
    box.style.height = `${(r.h / H) * 100}%`;
  }

  // desenha o recorte src cobrindo a caixa dst (igual ao FFmpeg: scale increase + crop central)
  function drawCover(ctx, src, dst, k) {
    const img = state.img;
    const iw = img.naturalWidth;
    const ih = img.naturalHeight;
    let sx = src.x * iw;
    let sy = src.y * ih;
    let sw = src.w * iw;
    let sh = src.h * ih;
    const dAspect = dst.w / dst.h;
    if (sw / sh > dAspect) { const nw = sh * dAspect; sx += (sw - nw) / 2; sw = nw; } else { const nh = sw / dAspect; sy += (sh - nh) / 2; sh = nh; }
    ctx.drawImage(img, sx, sy, sw, sh, dst.x * k, dst.y * k, dst.w * k, dst.h * k);
  }

  function drawCanvas() {
    const canvas = $('#leCanvas');
    const stage = $('#leDstStage');
    const dpr = window.devicePixelRatio || 1;
    const cssW = stage.clientWidth || 270;
    const pxW = Math.max(180, Math.round(cssW * dpr));
    const pxH = Math.round(pxW * CH / CW);
    if (canvas.width !== pxW) { canvas.width = pxW; canvas.height = pxH; }
    const ctx = canvas.getContext('2d');
    const k = pxW / CW;
    const L = state.layout;
    ctx.save();
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, pxW, pxH);
    if (state.img) {
      if (L.background === 'blur') {
        ctx.save();
        if ('filter' in ctx) ctx.filter = `blur(${Math.round(18 * k)}px) brightness(0.8)`;
        drawCover(ctx, L.game.src, { x: -40, y: -40, w: CW + 80, h: CH + 80 }, k);
        ctx.restore();
      }
      drawCover(ctx, L.game.src, L.game.dst, k);
      if (L.has_cam) drawCover(ctx, L.cam.src, L.cam.dst, k);
    } else {
      const paint = (r, color) => { ctx.fillStyle = color; ctx.fillRect(r.x * k, r.y * k, r.w * k, r.h * k); };
      if (L.background === 'blur') paint({ x: 0, y: 0, w: CW, h: CH }, '#1b1f27');
      paint(L.game.dst, '#1d3b66');
      if (L.has_cam) paint(L.cam.dst, '#5a2140');
    }
    ctx.restore();
  }

  function refresh() {
    if (!state.root || !state.layout) return;
    const L = state.layout;
    const sel = state.selected;

    state.root.querySelectorAll('.le-box').forEach((box) => {
      const space = box.dataset.space;
      const el = box.dataset.el;
      const hidden = el === 'cam' && !L.has_cam;
      box.hidden = hidden;
      if (hidden) return;
      placeBox(box, getRect(space, el), space);
      box.classList.toggle('le-selected', sel.space === space && sel.el === el);
      box.classList.toggle('le-linked', sel.el === el && sel.space !== space);
    });

    const capBox = state.root.querySelector('.le-box.le-caption');
    const stageW = $('#leDstStage').clientWidth || 270;
    capBox.querySelector('.le-caption-text').style.fontSize = `${(72 * stageW) / CW}px`;

    $('#leHasCam').checked = L.has_cam;
    $('#leBackground').value = L.background;
    const nameInput = $('#leLayoutName');
    if (nameInput && document.activeElement !== nameInput) {
      nameInput.value = L.name || 'Layout personalizado';
    }
    state.root.querySelectorAll('.le-tab[data-select]').forEach((t) => {
      t.classList.toggle('active', t.dataset.select === sel.el);
    });
    state.root.querySelectorAll('.le-tab[data-space-select]').forEach((t) => {
      t.classList.toggle('active', t.dataset.spaceSelect === sel.space);
      t.disabled = sel.el === 'caption' && t.dataset.spaceSelect === 'src';
    });

    const r = getRect(sel.space, sel.el);
    const f = sel.space === 'src' ? 100 : 1;
    const fields = { leFx: r.x, leFy: r.y, leFw: r.w, leFh: r.h };
    Object.keys(fields).forEach((id) => {
      const input = $('#' + id);
      if (document.activeElement !== input) input.value = sel.space === 'src' ? (fields[id] * f).toFixed(1) : Math.round(fields[id]);
    });
    $('#leSelName').textContent = sel.space === 'src'
      ? `${LABELS[sel.el]}: área no vídeo original (valores em %)`
      : `${LABELS[sel.el]}: posição no Short (px de 1080×1920)`;
    const { W } = bounds(sel.space);
    const pct = Math.round((r.w / W) * 100);
    if (document.activeElement !== $('#leSize')) $('#leSize').value = pct;
    $('#leSizeLabel').textContent = `${pct}%`;

    drawCanvas();
  }

  /* ---------- carregamento ---------- */

  function loadFrame(t) {
    const img = $('#leSrcImg');
    const q = t !== undefined && t !== null ? `?t=${encodeURIComponent(t.toFixed(2))}` : '';
    img.src = `/api/clips/${state.clip.id}/frame${q}`;
  }

  function close() {
    if (state.root) state.root.classList.remove('open');
    document.body.classList.remove('le-noscroll');
    state.drag = null;
  }

  async function openLayoutEditor(clipId) {
    if (!state.root) buildModal();
    let clip;
    try {
      const res = await fetch(`/api/clips/${clipId}`);
      clip = await res.json();
      if (!res.ok) { toast(clip.detail || 'Clipe não encontrado.', 'error'); return; }
    } catch (_) {
      toast('Erro de rede ao abrir o editor de layout.', 'error');
      return;
    }
    state.clip = clip;
    const saved = clip.editor_config && clip.editor_config.layout;
    const hasSaved = saved && typeof saved === 'object' && saved.cam;
    state.layout = normalize(hasSaved ? clone(saved) : defaultLayout());
    state.selected = { space: 'dst', el: 'game' };
    state.img = null;

    $('#leTitle').textContent = `Editor de layout: clipe #${clip.id}`;
    const words = String(clip.hook || clip.title || 'Sua legenda aparece aqui').split(/\s+/).slice(0, 5).join(' ');
    state.root.querySelector('.le-caption-text').textContent = words.toUpperCase();

    const tr = $('#leTime');
    tr.min = clip.start;
    tr.max = clip.end;
    const t0 = Math.min(clip.end, clip.start + Math.min(2, (clip.end - clip.start) / 2));
    tr.value = t0;
    $('#leTimeLabel').textContent = fmt(t0);

    $('#leSrcEmpty').hidden = !!clip.has_source_video;
    $('#leSrcImg').hidden = true;
    $('#leDetectBtn').disabled = !clip.has_source_video;

    state.root.classList.add('open');
    document.body.classList.add('le-noscroll');
    loadSavedLayouts();
    refresh();
    if (clip.has_source_video) {
      loadFrame(t0);
      if (!hasSaved) detectFacecam();
    }
  }

  window.openLayoutEditor = openLayoutEditor;
  window.LayoutEditor = { open: openLayoutEditor, close, _state: state, _normalize: normalize };
})();

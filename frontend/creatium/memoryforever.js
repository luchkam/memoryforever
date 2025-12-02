const API_BASE = 'https://memoryforever.onrender.com';
const MIN_PHOTOS = 1;
const MAX_PHOTOS = 2;
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 30;

let catalog = null;
let currentJobId = null;
let pollTimer = null;
let pollAttempts = 0;

// Элементы (инициализируются после DOMContentLoaded)
let sceneSelect;
let formatSelect;
let backgroundSelect;
let musicSelect;
let photosInput;
let photosStatusEl;
let renderBtn;
let statusTextEl;
let progressFillEl;
let progressLabelEl;
let videoEl;
let videoSourceEl;
let videoPlaceholderEl;
let videoLinkWrapEl;
let videoUrlAnchorEl;
let startFrameImgEl;
let downloadBtn;
let modalOverlay;
let modalTitleEl;
let modalBodyEl;
let modalActionsEl;
let modalCloseBtn;
let modalInitialised = false;
let videoStatus = 'idle'; // idle | rendering | ready | error
let videoUrl = null;
let uploadedPhotoUrls = [];
let uploadedPhotoNames = [];
let currentStartFrameUrl = null;
let sceneMetaMap = {};
let pendingPayment = null;
let paymentStatusTimer = null;
let lastJobId = null;
let lastResultUrl = null;
let webUserId = null;
let selectedLocalFiles = [];
let paymentPollAttempts = 0;
const PAYMENT_MAX_POLL_ATTEMPTS = 30;
let paymentPollAttempts = 0;
const PAYMENT_MAX_POLL_ATTEMPTS = 30;

const selectedState = {
  sceneKey: '',
  formatKey: '',
  backgroundKey: '',
  musicKey: ''
};
const SKY_SCENE_KEY = '🕊️ Уходит в небеса 10с - 100 рублей';
const TALL_FORMAT_KEY = '🧍 В рост';
const SCENE_PHOTO_RULES = {
  '🫂 Объятия 10с - 100 рублей': 2,
  '💏 Поцелуй 10с - 100 рублей': 2,
  '👫 Объятия 5с - БЕСПЛАТНО': 2,
  '👋 Прощание 10с - 100 рублей': 1,
  '🕊️ Уходит в небеса 10с - 100 рублей': 1
};

function safeLog(message, details) {
  try {
    var logs = (window.MF_DEBUG_LOGS = window.MF_DEBUG_LOGS || []);
    logs.push({ ts: new Date().toISOString(), message: String(message), details: details !== undefined ? details : null });
  } catch (e) {}
}

// Утилиты

function setStatus(text, variant) {
  statusTextEl.textContent = text;
  statusTextEl.classList.toggle('mf-status-text--error', variant === 'error');
}

function setRenderError(text) {
  setStatus(text, 'error');
  enableRenderButton(true);
}

function getWebUserId() {
  if (webUserId) return webUserId;
  try {
    const key = 'mf_web_uid';
    const stored = localStorage.getItem(key);
    if (stored) {
      webUserId = stored;
    } else if (window.crypto && window.crypto.randomUUID) {
      webUserId = 'web_' + window.crypto.randomUUID();
      localStorage.setItem(key, webUserId);
    } else {
      webUserId = 'web_' + String(Date.now());
      localStorage.setItem(key, webUserId);
    }
    console.log('[FREE_LIMIT] web user_id =', webUserId);
  } catch (_e) {
    webUserId = 'web_' + String(Date.now());
  }
  return webUserId;
}

function setProgress(percent) {
  const v = Math.max(0, Math.min(100, percent));
  progressFillEl.style.width = v + '%';
  progressLabelEl.textContent = v + '%';
}

function updateRenderProgress(percent, message) {
  if (typeof message === 'string') {
    setStatus(message);
  }
  setProgress(percent);
}

function enableRenderButton(enabled) {
  renderBtn.disabled = !enabled;
}

function resetDownload() {
  if (downloadBtn) {
    downloadBtn.hidden = true;
    downloadBtn.onclick = null;
  }
}

function resetRenderState(msg) {
  videoStatus = 'idle';
  currentJobId = null;
  pendingPayment = null;
  clearPollTimer();
  updateRenderProgress(0, msg || 'Выберите фото и запустите рендер.');
  enableRenderButton(true);
}

function finalizeRender(data, message) {
  clearPollTimer();
  updateRenderProgress(80, 'Видео генерируется…');
  setTimeout(function () {
    updateRenderProgress(100, message || 'Готово! Видео сгенерировано.');
    if (data && data.result && data.result.start_frame_url) {
      showStartFrame(data.result.start_frame_url);
    }
    if (data && data.result && data.result.video_url) {
      showFinalVideo(data.result.video_url);
      lastResultUrl = data.result.video_url;
      lastJobId = currentJobId || lastJobId;
    }
    videoStatus = 'ready';
    enableRenderButton(true);
    renderBtn.textContent = 'Сделать видео';
    renderBtn.dataset.mode = 'render';
  }, 300);
}

function setupDownload(fullUrl, enabled) {
  if (!downloadBtn) return;
  if (!enabled || !fullUrl) {
    resetDownload();
    return;
  }
  downloadBtn.hidden = false;
  downloadBtn.onclick = function () {
    try {
      const downloadHref = lastJobId ? API_BASE + '/v1/render/download/' + lastJobId : fullUrl;
      fetch(downloadHref, { mode: 'cors', credentials: 'omit' })
        .then(function (res) {
          if (!res.ok) throw new Error('download HTTP ' + res.status);
          return res.blob();
        })
        .then(function (blob) {
          const a = document.createElement('a');
          const objUrl = URL.createObjectURL(blob);
          a.href = objUrl;
          a.download = 'memory_forever_video.mp4';
          a.style.display = 'none';
          document.body.appendChild(a);
          a.click();
          setTimeout(function () {
            URL.revokeObjectURL(objUrl);
            document.body.removeChild(a);
          }, 1000);
        })
        .catch(function () {
          window.open(lastResultUrl || fullUrl, '_blank');
        });
    } catch (e) {
      window.open(lastResultUrl || fullUrl, '_blank');
    }
  };
}

function showVideo(url, isFinal) {
  const fullUrl = url.startsWith('http') ? url : API_BASE + url;

  // показываем видео-плеер
  videoEl.removeAttribute('hidden');
  videoEl.style.display = 'block';
  videoPlaceholderEl.style.display = 'none';
  try {
    videoEl.src = fullUrl;
    videoEl.load();
    videoEl.play().catch(function () {});
    videoEl.controls = true;
  } catch (e) {
    // silent
  }

  videoSourceEl.src = fullUrl;
  videoUrlAnchorEl.href = fullUrl;
  videoLinkWrapEl.hidden = false;
  setupDownload(fullUrl, isFinal);
}

function showFinalVideo(url) {
  videoStatus = 'ready';
  videoUrl = url;
  lastResultUrl = url;
  showVideo(url, true);
}

function showExampleVideo(url) {
  videoStatus = 'idle';
  videoUrl = null;
  showVideo(url, false);
}

function showStartFrame(url) {
  if (!url) return;
  const fullUrl = url.startsWith('http') ? url : API_BASE + url;
  currentStartFrameUrl = fullUrl;
  if (!startFrameImgEl) {
    startFrameImgEl = document.createElement('img');
    startFrameImgEl.id = 'mf-startframe';
    startFrameImgEl.style.maxWidth = '100%';
    startFrameImgEl.style.maxHeight = '100%';
    startFrameImgEl.style.objectFit = 'contain';
    startFrameImgEl.style.borderRadius = '12px';
    startFrameImgEl.alt = 'Превью';
    videoPlaceholderEl.innerHTML = '';
    videoPlaceholderEl.appendChild(startFrameImgEl);
  }
  startFrameImgEl.src = fullUrl;
  videoEl.style.display = 'none';
  videoPlaceholderEl.style.display = 'flex';
  videoLinkWrapEl.hidden = true;
  resetDownload();
}

function resetVideo() {
  videoEl.pause();
  videoSourceEl.src = '';
  videoEl.load();
  videoEl.style.display = 'none';
  videoPlaceholderEl.style.display = 'flex';
  videoLinkWrapEl.hidden = true;
  if (startFrameImgEl) {
    startFrameImgEl.src = '';
  }
  videoStatus = 'idle';
  videoUrl = null;
  lastResultUrl = null;
  currentStartFrameUrl = null;
  resetDownload();
}

function setPhotosStatus(text, variant) {
  photosStatusEl.textContent = text;
  photosStatusEl.classList.toggle('mf-photos-status--error', variant === 'error');
  photosStatusEl.classList.toggle('mf-photos-status--success', variant === 'success');
}

function requiredPhotosCount() {
  return SCENE_PHOTO_RULES[selectedState.sceneKey] || MIN_PHOTOS;
}

function maxPhotosAllowed() {
  return SCENE_PHOTO_RULES[selectedState.sceneKey] === 1 ? 1 : MAX_PHOTOS;
}

function resetToStartFramePhase(reason) {
  videoStatus = 'idle';
  videoUrl = null;
  currentStartFrameUrl = null;
  pendingPayment = null;
  renderBtn.textContent = 'Сгенерировать старт-кадр';
  renderBtn.dataset.mode = 'start';
  resetDownload();
  resetVideo();
  updatePhotosUi();

  const count = uploadedPhotoUrls.length;
  const required = requiredPhotosCount();
  if (count === 0) {
    setStatus('Заполните настройки и загрузите фото для старт-кадра.');
  } else if (count < required) {
    setStatus('Добавьте ещё фото для сюжета (' + count + '/' + required + ').');
  } else {
    setStatus('Фото загружены. Сгенерируйте старт-кадр.');
  }

  if (reason) {
    safeLog('[MF_WEB] resetToStartFramePhase', reason);
  }
}

function updatePhotosUi(fileNames) {
  const count = uploadedPhotoUrls.length;
  const required = requiredPhotosCount();
  const maxAllowed = maxPhotosAllowed();
  let text = '';
  let variant = null;

  if (count === 0) {
    text = 'Фото ещё не загружены';
  } else if (count < required) {
    text = 'Загружено ' + count + ' фото. Для сюжета нужно ' + required + '.';
    variant = 'error';
  } else if (count > maxAllowed) {
    text = 'Для сюжета допускается максимум ' + maxAllowed + ' фото.';
    variant = 'error';
  } else {
    text = '✅ Фото загружены: ' + count;
  }

  const namesToShow = uploadedPhotoNames.length ? uploadedPhotoNames : fileNames || [];
  if (namesToShow && namesToShow.length) {
    text += '\n' + namesToShow.join('\n');
  }

  setPhotosStatus(text, variant);
  enableRenderButton(count >= required);
}

function updateSelectedState() {
  selectedState.sceneKey = sceneSelect ? sceneSelect.value : '';
  selectedState.formatKey = formatSelect ? formatSelect.value : '';
  selectedState.backgroundKey = backgroundSelect ? backgroundSelect.value : '';
  selectedState.musicKey = musicSelect ? musicSelect.value : '';
}

function getSceneMeta(sceneKey) {
  return sceneMetaMap[sceneKey] || {};
}

function isPaidScene(sceneKey) {
  const meta = getSceneMeta(sceneKey || selectedState.sceneKey);
  return (meta.price_rub || 0) > 0;
}

function applySceneFormatRules() {
  if (selectedState.sceneKey === SKY_SCENE_KEY) {
    lockFormatToTall();
  } else {
    unlockFormats();
  }
  const maxAllowed = maxPhotosAllowed();
  if (maxAllowed === 1 && uploadedPhotoUrls.length > 1) {
    uploadedPhotoUrls = uploadedPhotoUrls.slice(0, 1);
    uploadedPhotoNames = uploadedPhotoNames.slice(0, 1);
  }
  updatePhotosUi();
}

function ensureElements() {
  safeLog('[MF_WEB] ensureElements call');
  sceneSelect = document.getElementById('mf-scene');
  formatSelect = document.getElementById('mf-format');
  backgroundSelect = document.getElementById('mf-background');
  musicSelect = document.getElementById('mf-music');
  photosInput = document.getElementById('mf-photos-input') || document.getElementById('mf-photo-input');
  photosStatusEl = document.getElementById('mf-photos-status');
  renderBtn = document.getElementById('mf-render-btn');
  statusTextEl = document.getElementById('mf-status-text');
  progressFillEl = document.getElementById('mf-progress-fill');
  progressLabelEl = document.getElementById('mf-progress-label');
  videoEl = document.getElementById('mf-video');
  videoSourceEl = document.getElementById('mf-video-source');
  videoPlaceholderEl = document.getElementById('mf-video-placeholder');
  videoLinkWrapEl = document.getElementById('mf-video-link');
  videoUrlAnchorEl = document.getElementById('mf-video-url-anchor');
  downloadBtn = document.getElementById('mf-download-btn');
  modalOverlay = document.getElementById('mf-modal-overlay');
  modalTitleEl = document.getElementById('mf-modal-title');
  modalBodyEl = document.getElementById('mf-modal-body');
  modalActionsEl = document.getElementById('mf-modal-actions');
  modalCloseBtn = document.getElementById('mf-modal-close');

  const allElementsFound =
    sceneSelect &&
    formatSelect &&
    backgroundSelect &&
    musicSelect &&
    photosInput &&
    photosStatusEl &&
    renderBtn &&
    statusTextEl &&
    progressFillEl &&
    progressLabelEl &&
    videoEl &&
    videoSourceEl &&
    videoPlaceholderEl &&
    videoLinkWrapEl &&
    videoUrlAnchorEl &&
    modalOverlay && modalTitleEl && modalBodyEl && modalActionsEl && modalCloseBtn;

  if (!allElementsFound) {
    safeLog('[MF_WEB] Не найдены элементы формы при инициализации');
    return false;
  }

  // гарантируем multiple и accept=image/* даже если Creatium что-то подменил
  try {
    photosInput.setAttribute('multiple', 'multiple');
    photosInput.setAttribute('accept', 'image/*');
  } catch (_e) {
    /* ignore */
  }

  // Ensure modal closed/cleared on init
  modalOverlay.hidden = true;
  modalTitleEl.textContent = '';
  modalBodyEl.textContent = '';
  modalActionsEl.innerHTML = '';
  modalInitialised = true;

  // Hide download button initially
  resetDownload();
  videoStatus = 'idle';
  videoUrl = null;

  return true;
}

// Каталог

async function loadCatalog() {
  try {
    setStatus('Загружаем каталог сцен и настроек…');
    safeLog('[MF_WEB] Запрашиваем каталог');

    const resp = await fetch(API_BASE + '/v1/catalog');
    if (!resp.ok) {
      throw new Error('Ошибка загрузки каталога: ' + resp.status);
    }
    catalog = await resp.json();
    window.MF_CATALOG = catalog;
    safeLog('[MF_WEB] Каталог получен', catalog);

    sceneMetaMap = {};
    (catalog.scenes || []).forEach(function (sc) {
      sceneMetaMap[sc.key] = sc;
    });

    fillSelect(sceneSelect, catalog.scenes || [], { allowEmpty: false });
    fillSelect(formatSelect, catalog.formats || [], { allowEmpty: false });
    fillSelect(backgroundSelect, catalog.backgrounds || [], { allowEmpty: false });
    fillSelect(musicSelect, catalog.music || [], { allowEmpty: true, emptyLabel: 'Без музыки' });

    updateSelectedState();
    applySceneFormatRules();

    setStatus('Каталог загружен. Загрузите фото и нажмите «Сгенерировать старт-кадр».');
  } catch (err) {
    safeLog('[MF_WEB] Каталог не загрузился', err && err.message ? err.message : err);
    setStatus('Не удалось загрузить каталог. Попробуйте обновить страницу.', 'error');
  }
}

function fillSelect(selectEl, items, options) {
  if (!selectEl) return;
  const opts = options || {};
  selectEl.innerHTML = '';

  if (opts.allowEmpty) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = opts.emptyLabel || '—';
    selectEl.appendChild(opt);
  }

  if (!items || !Array.isArray(items) || items.length === 0) {
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = 'Нет данных';
    selectEl.appendChild(opt);
    return;
  }

  items.forEach(function (item) {
    const opt = document.createElement('option');
    const key = item.key || item.id || '';
    const label = item.title || item.name || item.key || item.id || '—';
    opt.value = key;
    opt.textContent = label;
    selectEl.appendChild(opt);
  });

  if (selectEl.options.length > 0) {
    const initialIndex = opts.allowEmpty ? 1 : 0;
    selectEl.selectedIndex = Math.min(initialIndex, selectEl.options.length - 1);
  }
}

// Фото

async function uploadPhotos(files) {
  console.log('[DEBUG] uploadPhotos called with', files && files.length, 'files');
  const newFiles = (files || []).slice();
  const maxAllowed = maxPhotosAllowed();
  safeLog('[MF_WEB] upload change files', { selected: newFiles.length, existing: 0, max: maxAllowed });

  if (!newFiles || newFiles.length === 0) {
    setPhotosStatus('Выберите 1–2 фотографии.', 'error');
    enableRenderButton(false);
    if (photosInput) photosInput.value = '';
    return;
  }

  if (newFiles.length > maxAllowed) {
    const msg = maxAllowed === 1 ? 'Для этого сюжета допускается только 1 фото.' : 'Можно загрузить только 1–2 фотографии.';
    setPhotosStatus(msg, 'error');
    if (photosInput) photosInput.value = '';
    return;
  }

  const formData = new FormData();
  for (let i = 0; i < newFiles.length; i++) {
    formData.append('files', newFiles[i]);
  }

  setPhotosStatus('Загружаем фото…', null);
  enableRenderButton(false);

  try {
    safeLog('[MF_WEB] Загружаем фото, файлов', newFiles.length);
    const resp = await fetch(API_BASE + '/v1/upload', {
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      body: formData
    });

    if (!resp.ok) {
      let bodyText = '';
      try {
        bodyText = await resp.text();
      } catch (e) {
        bodyText = '<no body>';
      }
      const msg = 'HTTP ' + resp.status + ' ' + resp.statusText + ' — ' + bodyText.slice(0, 200);
      safeLog('[MF_WEB] upload non-ok', msg);
      throw new Error(msg);
    }

    const data = await resp.json();
    if (!data.files || !Array.isArray(data.files) || data.files.length === 0) {
      throw new Error('Сервер не вернул пути загруженных файлов.');
    }

    // пересоздаем списки (без накопления)
    const spaceLeft = Math.max(0, maxAllowed);
    uploadedPhotoUrls = data.files.slice(0, spaceLeft);
    uploadedPhotoNames = newFiles
      .map(function (f) {
        return f.name;
      })
      .slice(0, spaceLeft);

    updatePhotosUi();
    resetToStartFramePhase('photos-updated');
    const required = requiredPhotosCount();
    if (uploadedPhotoUrls.length < required) {
      setStatus('Добавьте ещё фото для сюжета (' + uploadedPhotoUrls.length + '/' + required + ').');
    } else {
      setStatus('Фото загружены. Сгенерируйте старт-кадр.');
    }
    photosInput.value = '';
  } catch (err) {
    safeLog('[MF_WEB] upload error', err && err.message ? err.message : err);
    setPhotosStatus('Ошибка при загрузке фото. Попробуйте ещё раз.', 'error');
    enableRenderButton(uploadedPhotoUrls.length >= requiredPhotosCount());
    setStatus('Ошибка при загрузке фото: ' + (err && err.message ? err.message : 'неизвестная ошибка'), 'error');
    photosInput.value = '';
  }
}

// Старт-кадр и рендер

async function generateStartFrame() {
  const required = requiredPhotosCount();
  if (!uploadedPhotoUrls || uploadedPhotoUrls.length < required) {
    setStatus('Для выбранного сюжета нужно ' + required + ' фото.', 'error');
    return;
  }
  updateSelectedState();
  applySceneFormatRules();
  setStatus('Генерируем старт-кадр…');
  setProgress(10);
  enableRenderButton(false);
  resetDownload();
  videoStatus = 'idle';
  resetDownload();

  const payload = {
    scene_key: selectedState.sceneKey,
    format_key: selectedState.formatKey,
    background_key: selectedState.backgroundKey,
    photos: uploadedPhotoUrls
  };

  try {
    safeLog('[MF_WEB] start-frame payload', payload);
    const resp = await fetch(API_BASE + '/v1/start-frame', {
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      let bodyText = '';
      try {
        bodyText = await resp.text();
      } catch (_e) {
        bodyText = '<no body>';
      }
      const msg = 'HTTP ' + resp.status + ' ' + resp.statusText + ' — ' + bodyText.slice(0, 200);
      throw new Error(msg);
    }
    const data = await resp.json();
    if (data.start_frame_url) {
      showStartFrame(data.start_frame_url);
      setStatus('Старт-кадр готов. Нажмите «Сделать видео».');
      renderBtn.textContent = 'Сделать видео';
      renderBtn.dataset.mode = 'render';
    }
    enableRenderButton(true);
    setProgress(40);
  } catch (err) {
    safeLog('[MF_WEB] start-frame error', err && err.message ? err.message : err);
    setStatus('Ошибка при генерации старт-кадра: ' + (err && err.message ? err.message : ''), 'error');
    enableRenderButton(true);
  }
}

async function startRender() {
  if (!catalog) {
    setRenderError('Каталог ещё не загружен.');
    return;
  }
  const required = requiredPhotosCount();
  if (!uploadedPhotoUrls || uploadedPhotoUrls.length < required) {
    setRenderError('Для выбранного сюжета нужно ' + required + ' фото.');
    return;
  }
  updateSelectedState();
  applySceneFormatRules();

  startPaidRender();
}

async function startPaidRender() {
  if (!uploadedPhotoUrls || uploadedPhotoUrls.length === 0) {
    updateRenderProgress(0, 'Сначала загрузите 1–2 фото для видео.');
    return;
  }

  const payload = {
    format_key: selectedState.formatKey,
    scene_key: selectedState.sceneKey,
    background_key: selectedState.backgroundKey,
    music_key: selectedState.musicKey || '',
    title: '',
    subtitle: '',
    photos: uploadedPhotoUrls,
    user: getWebUserId()
  };

  updateRenderProgress(5, 'Отправляем запрос на рендер…');
  enableRenderButton(false);
  clearPollTimer();
  resetVideo();
  pollAttempts = 0;
  videoStatus = 'rendering';
  videoUrl = null;

  pendingPayment = null;

  console.log('[DEBUG] start_paid payload.photos =', payload.photos);
    window.MF_DEBUG_LOGS.push({ ts: new Date().toISOString(), message: '[MF_WEB] render start_paid → request', details: payload });
  try {
    const resp = await fetch(API_BASE + '/v1/render/start_paid', {
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      let body = {};
      let rawText = '';
      try {
        rawText = await resp.text();
        body = rawText ? JSON.parse(rawText) : {};
      } catch (_e) {
        body = {};
      }
      const isLimit =
        resp.status === 429 &&
        (body.status === 'free_limit_reached' || body.error_code === 'FREE_HUGS_LIMIT_REACHED');
      if (isLimit) {
        const limitMsg =
          'Лимит бесплатных видео исчерпан. Вы уже сделали 2 бесплатных видео на этом устройстве. Чтобы продолжить, выберите платный сюжет.';
        updateRenderProgress(0, limitMsg);
        setStatus(limitMsg, 'error');
        setRenderError(limitMsg);
        videoStatus = 'idle';
        renderBtn.textContent = 'Сделать видео';
        renderBtn.dataset.mode = 'render';
        enableRenderButton(true);
        return;
      }
      window.MF_DEBUG_LOGS.push({
        ts: new Date().toISOString(),
        message: '[MF_WEB] render start_paid HTTP error',
        details: { status: resp.status, body: rawText || null }
      });
      const errMsg = 'Не удалось запустить рендер: HTTP ' + resp.status;
      updateRenderProgress(0, errMsg);
      setRenderError(errMsg);
      videoStatus = 'idle';
      enableRenderButton(true);
      currentJobId = null;
      return;
    }

    let data;
    try {
      data = await resp.json();
    } catch (e) {
      window.MF_DEBUG_LOGS.push({
        ts: new Date().toISOString(),
        message: '[MF_WEB] render start_paid JSON parse error',
        details: { error: String(e) }
      });
      setRenderError('Не удалось запустить рендер: Load failed');
      return;
    }

    window.MF_DEBUG_LOGS.push({
      ts: new Date().toISOString(),
      message: '[MF_WEB] render start_paid → response',
      details: data
    });

    const status = data.status;
    safeLog('[MF_WEB] start_paid status', status);
    safeLog('[MF_WEB] start_paid status', status);

    if (status === 'need_payment') {
      const paymentObj = data.payment || {};
      const ctxRaw = paymentObj['@context'];
      const ctx = typeof ctxRaw === 'string' ? ctxRaw.toLowerCase() : null;
      let paymentUrl = data.payment_url || paymentObj.url || paymentObj.paymentLink || '';
      if (ctx) {
        paymentUrl = paymentUrl || paymentObj.paymentLink || paymentObj.url;
      }

      window.MF_DEBUG_LOGS.push({
        ts: new Date().toISOString(),
        message: '[MF_WEB] start_paid need_payment',
        details: { payment_key: data.payment_key, url: paymentUrl }
      });
      pendingPayment = {
        payment_url: paymentUrl,
        payment_id: data.payment_id || paymentObj.id || null,
        payment_key: data.payment_key || null,
        payload: payload
      };

      openPaymentModal({ url: paymentUrl, payload: payload });
      updateRenderProgress(10, 'Оплата создана. После оплаты видео начнётся автоматически.');
      enableRenderButton(true);
      if (data.payment_key) {
        startPaymentStatusPolling(data.payment_key);
      }
      return;
    }

    if (status === 'done' && data.result && data.result.video_url) {
      finalizeRender(data, 'Готово! Видео сгенерировано.');
      uploadedPhotoUrls = [];
      uploadedPhotoNames = [];
      selectedLocalFiles = [];
      if (photosInput) {
        try {
          photosInput.value = '';
        } catch (_e) {}
      }
      updatePhotosUi();
      return;
    }

    if (status === 'render_started') {
      safeLog('[MF_WEB] start_paid render_started', data.job_id);
      currentJobId = data.job_id;
      lastJobId = data.job_id;
      pollAttempts = 0;
      updateRenderProgress(40, 'Оплата получена. Отправляем на генерацию…');
      pendingPayment = null;
      pollStatus(currentJobId);
      return;
    }

    if (status === 'pending_payment') {
      if (data.payment_key) {
        startPaymentStatusPolling(data.payment_key);
      }
      setStatus('Оплата ещё не подтверждена. После оплаты рендер стартует автоматически.');
      enableRenderButton(true);
      return;
    }

    if (status === 'error') {
      setRenderError('Не удалось запустить рендер: ' + (data.message || 'Ошибка сервера'));
      return;
    }

    setRenderError('Не удалось запустить рендер: неизвестный статус');
  } catch (err) {
    window.MF_DEBUG_LOGS.push({
      ts: new Date().toISOString(),
      message: '[MF_WEB] render start_paid network error',
      details: { error: String(err) }
    });
    const msg = 'Не удалось запустить рендер: Load failed';
    updateRenderProgress(0, msg);
    setRenderError(msg);
    videoStatus = 'idle';
    currentJobId = null;
  }
}

// Опрос статуса

function clearPollTimer() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (paymentStatusTimer) {
    clearTimeout(paymentStatusTimer);
    paymentStatusTimer = null;
  }
}

function mapRenderError(message, code) {
  if (code === 'NO_PHOTOS_PROVIDED' || (message && message.indexOf('No photos provided') !== -1)) {
    return 'Не удалось прочитать фото. Попробуйте заново выбрать 1–2 файла и запустить рендер.';
  }
  return message || 'Неизвестная ошибка при рендере.';
}

async function pollStatus(jobId) {
  if (!jobId) return;

  try {
    if (pollAttempts >= MAX_POLL_ATTEMPTS) {
      setStatus('Не удалось дождаться результата. Попробуйте позже.', 'error');
      enableRenderButton(true);
      return;
    }

    pollAttempts += 1;
    safeLog('[MF_WEB] Опрос статуса, попытка ' + pollAttempts);
    const resp = await fetch(API_BASE + '/v1/render/status/' + jobId);
    if (!resp.ok) {
      throw new Error('Ошибка статуса: ' + resp.status);
    }

    const data = await resp.json();

    if (data.status === 'queued' || data.status === 'processing') {
      if (data.start_frame_url) {
        showStartFrame(data.start_frame_url);
      }
      let p = 0;
      if (typeof data.progress === 'number') {
        p = Math.min(95, Math.max(60, data.progress));
      } else {
        const dynamic = Math.min(80, Math.max(60, Math.round((pollAttempts + 1) * 2 + 60)));
        p = dynamic;
      }
      updateRenderProgress(p, 'Видео генерируется…');

      pollTimer = setTimeout(function () {
        pollStatus(jobId);
      }, POLL_INTERVAL_MS);
      return;
    }

    if (data.status === 'done') {
      finalizeRender(data, 'Готово! Видео сгенерировано.');
      return;
    }

    if (data.status === 'error') {
      const friendly = mapRenderError(data.error, data.error_code || data.code);
      setStatus(friendly, 'error');
      videoStatus = 'error';
      videoUrl = null;
      clearPollTimer();
      enableRenderButton(true);
      return;
    }

    setStatus('Неожиданный статус: ' + data.status);
    enableRenderButton(true);
  } catch (err) {
    safeLog('[MF_WEB] Ошибка при опросе статуса', err && err.message ? err.message : err);
    setStatus('Ошибка при получении статуса: ' + (err && err.message ? err.message : ''), 'error');
    enableRenderButton(true);
  }
}

function startPaymentStatusPolling(paymentKey) {
  if (!paymentKey) return;
  paymentPollAttempts = 0;
  const poll = async function () {
    try {
      paymentPollAttempts += 1;
      if (paymentPollAttempts > PAYMENT_MAX_POLL_ATTEMPTS) {
        const timeoutMsg = 'Оплата не подтвердилась. Попробуйте ещё раз.';
        updateRenderProgress(0, timeoutMsg);
        setStatus(timeoutMsg, 'error');
        resetRenderState(timeoutMsg);
        return;
      }
      const resp = await fetch(API_BASE + '/v1/render/status_by_payment/' + paymentKey);
      if (!resp.ok) {
        paymentStatusTimer = setTimeout(function () {
          poll();
        }, POLL_INTERVAL_MS);
        return;
      }
      const data = await resp.json();
      safeLog('[MF_WEB] payment poll status', data.status);
      if (data.status === 'pending_payment' || data.status === 'need_payment') {
        updateRenderProgress(20, 'Ожидаем подтверждение оплаты…');
        paymentStatusTimer = setTimeout(function () {
          poll();
        }, POLL_INTERVAL_MS);
        return;
      }
      if (data.status === 'render_started' && data.job_id) {
        safeLog('[MF_WEB] payment poll render_started', data.job_id);
        updateRenderProgress(40, 'Оплата получена. Отправляем на генерацию…');
        clearPollTimer();
        pollStatus(data.job_id);
        return;
      }
      if (data.status === 'done' && data.result && data.result.video_url && data.job_id) {
        finalizeRender(data, 'Готово! Видео сгенерировано.');
        return;
      }
      if (data.status === 'error') {
        const msg = data.message || 'Оплата не подтвердилась. Попробуйте ещё раз.';
        updateRenderProgress(0, msg);
        setStatus(msg, 'error');
        resetRenderState(msg);
        return;
      }
      updateRenderProgress(10, 'Ожидаем подтверждение оплаты…');
      paymentStatusTimer = setTimeout(function () {
        poll();
      }, POLL_INTERVAL_MS);
    } catch (_e) {
      paymentStatusTimer = setTimeout(function () {
        poll();
      }, POLL_INTERVAL_MS);
    }
  };
  poll();
}

// Модалки

function openModal(title, bodyHtml, actionsBuilder) {
  if (!modalOverlay) return;
  if (!modalInitialised) {
    modalOverlay.hidden = true;
    modalTitleEl.textContent = '';
    modalBodyEl.textContent = '';
    modalActionsEl.innerHTML = '';
    modalInitialised = true;
  }
  modalTitleEl.textContent = title;
  modalBodyEl.innerHTML = bodyHtml;
  modalActionsEl.innerHTML = '';
  if (actionsBuilder) {
    actionsBuilder(modalActionsEl);
  } else {
    const btn = document.createElement('button');
    btn.textContent = 'Закрыть';
    btn.className = 'mf-button mf-button--ghost';
    btn.onclick = closeModal;
    modalActionsEl.appendChild(btn);
  }
  modalOverlay.hidden = false;
}

function closeModal() {
  if (!modalOverlay) return;
  modalOverlay.hidden = true;
}

function handleEscClose(evt) {
  if (evt.key === 'Escape') {
    closeModal();
  }
}

function openPaymentModal(opts) {
  safeLog('[MF_WEB] openPaymentModal called', { paymentInfo: opts });
  const paymentUrl = opts && opts.url ? opts.url : '';
  const payload = opts && opts.payload ? opts.payload : null;
  const sceneKeyForPrice = payload && payload.scene_key ? payload.scene_key : selectedState.sceneKey;
  const price = (getSceneMeta(sceneKeyForPrice).price_rub || 0);
  openModal('Оплата сюжета', '', function (actionsEl) {
    const body = document.createElement('div');
    body.innerHTML = `<p>Вы выбрали платный сюжет. Стоимость: <b>${price} ₽</b>.</p><p>После оплаты генерация видео начнётся автоматически.</p>`;
    modalBodyEl.innerHTML = '';
    modalBodyEl.appendChild(body);

    actionsEl.innerHTML = '';
    const payBtn = document.createElement('button');
    payBtn.className = 'mf-button';
    payBtn.textContent = 'Оплата картой / СБП';
    payBtn.setAttribute('data-mf-payment-open', '1');
    payBtn.onclick = function () {
      if (paymentUrl) {
        window.open(paymentUrl, '_blank');
      }
    };

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'mf-button mf-button--ghost';
    cancelBtn.textContent = 'Закрыть';
    cancelBtn.setAttribute('data-mf-payment-close', '1');
    cancelBtn.onclick = closeModal;

    actionsEl.appendChild(payBtn);
    actionsEl.appendChild(cancelBtn);
  });
}

function buildSupportModal() {
  const body = document.createElement('div');
  const msgLabel = document.createElement('label');
  msgLabel.textContent = 'Сообщение';
  const msgArea = document.createElement('textarea');
  msgArea.className = 'mf-modal__textarea';
  msgArea.rows = 4;

  const contactLabel = document.createElement('label');
  contactLabel.textContent = 'Контакт для ответа (email/телеграм)';
  const contactInput = document.createElement('input');
  contactInput.type = 'text';
  contactInput.className = 'mf-modal__input';

  body.appendChild(msgLabel);
  body.appendChild(msgArea);
  body.appendChild(contactLabel);
  body.appendChild(contactInput);
  modalBodyEl.innerHTML = '';
  modalBodyEl.appendChild(body);

  modalActionsEl.innerHTML = '';
  const sendBtn = document.createElement('button');
  sendBtn.textContent = 'Отправить';
  sendBtn.className = 'mf-button';
  sendBtn.onclick = async function () {
    const text = msgArea.value || '';
    const contact = contactInput.value || '';
    if (!text.trim()) {
      alert('Введите сообщение.');
      return;
    }
    safeLog('[MF_WEB] support send start');
    try {
      const resp = await fetch(API_BASE + '/v1/support', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.trim(), user_contact: contact.trim() })
      });
      if (!resp.ok) {
        const t = await resp.text();
        alert('Ошибка отправки: ' + resp.status + ' ' + t);
        return;
      }
      safeLog('[MF_WEB] support send ok');
      alert('Сообщение отправлено, мы ответим вам в ближайшее время');
      closeModal();
    } catch (err) {
      safeLog('[MF_WEB] support send error', err && err.message ? err.message : err);
      alert('Ошибка отправки: ' + (err && err.message ? err.message : err));
    }
  };
  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Закрыть';
  cancelBtn.className = 'mf-button mf-button--ghost';
  cancelBtn.onclick = closeModal;
  modalActionsEl.appendChild(cancelBtn);
  modalActionsEl.appendChild(sendBtn);
}

function openExampleVideo() {
  resetDownload();
  videoStatus = 'idle';
  videoUrl = null;
  showVideo('/assets/examples/example1.mp4', false);
  setStatus('Пример видео загружен.');
}

// События

function handlePhotosChange(evt) {
  console.log('[DEBUG] handlePhotosChange, new FileList:', evt.target.files);
  const files = evt.target.files ? Array.from(evt.target.files) : [];
  // сбрасываем прежние фото
  uploadedPhotoUrls = [];
  uploadedPhotoNames = [];
  selectedLocalFiles = files.slice();
  updatePhotosUi();
  if (selectedLocalFiles.length === 0) {
    return;
  }
  uploadPhotos(selectedLocalFiles.slice());
}

function handleSelectChange(evt) {
  updateSelectedState();
  applySceneFormatRules();
  if (selectedState.sceneKey === SKY_SCENE_KEY) {
    setStatus('Для сцены «Уходит в небеса» формат фиксирован: «🧍 В рост».');
  }
  const targetId = evt && evt.target ? evt.target.id : '';
  if (targetId === 'mf-scene' || targetId === 'mf-format' || targetId === 'mf-background') {
    if (uploadedPhotoUrls.length > 0 || currentStartFrameUrl || videoUrl) {
      resetToStartFramePhase('selection-changed');
    }
  }
}

function lockFormatToTall() {
  let hasTarget = false;
  for (let i = 0; i < formatSelect.options.length; i++) {
    const opt = formatSelect.options[i];
    if (opt.value === TALL_FORMAT_KEY) {
      hasTarget = true;
      opt.disabled = false;
      opt.selected = true;
    } else {
      opt.disabled = true;
    }
  }
  if (!hasTarget && formatSelect.options.length > 0) {
    formatSelect.selectedIndex = 0;
  }
  selectedState.formatKey = formatSelect.value;
}

function unlockFormats() {
  for (let i = 0; i < formatSelect.options.length; i++) {
    formatSelect.options[i].disabled = false;
  }
}

function initToolbar() {
  const toolbar = document.querySelector('.mf-toolbar');
  if (!toolbar) return;
  toolbar.addEventListener('click', function (evt) {
    const btn = evt.target.closest('button[data-action]');
    if (!btn) return;
    const act = btn.dataset.action;
    if (act === 'price') {
      openModal('Стоимость', `
        <p>💲 <b>Стоимость</b></p>
        <p>• <b>5 сек</b> — <b>бесплатно</b> (до 2 раз на пользователя)</p>
        <p>• <b>10 сек</b> — <b>100 ₽</b> за каждый выбранный сюжет</p>
        <p>• <b>Объединение сюжетов</b> — сумма цен всех выбранных сюжетов</p>
        <p>🧩 <b>Опции</b></p>
        <p>• Загрузить свой фон — 50 ₽</p>
        <p>• Загрузить свою музыку — 50 ₽</p>
        <p>• Свои финальные титры — 50 ₽ (до 60 символов)</p>
        <p>• Вторая вариация (другой сервис генерации) — +50% к итоговой стоимости</p>
        <p><i>Опции применяются ко всему ролику и добавляются к итоговой цене.</i></p>
      `);
    } else if (act === 'offer') {
      window.open('https://memoryforever.ru/oferta', '_blank');
    } else if (act === 'policy') {
      window.open('https://memoryforever.ru/privacy', '_blank');
    } else if (act === 'support') {
      openModal('Техподдержка', '', null);
      buildSupportModal();
    } else if (act === 'guide') {
      openModal('Инструкция', `
        <p><b>ВАЖНО!</b> Для пары — похожий масштаб людей. Чем ближе масштаб на фото, тем качественнее будет видео.</p>
        <p><b>Как сделать видео</b></p>
        <p>1) Выберите формат кадра.</p>
        <p>2) Выберите сюжет.</p>
        <p>3) Выберите фон и музыку.</p>
        <p>4) Загрузите фото: 1 фото — одиночная сцена, 2 фото — для пары.</p>
        <p>5) Согласуйте старт-кадр и запустите рендер.</p>
        <p>Советы: фото светлое, анфас; фон 9:16 без лишних деталей; похожая ширина плеч у пары.</p>
      `);
    } else if (act === 'example') {
      openExampleVideo();
    }
  });
}

function init() {
  if (!ensureElements()) return;

  setProgress(0);
  resetVideo();
  updatePhotosUi();
  renderBtn.textContent = 'Сгенерировать старт-кадр';
  renderBtn.dataset.mode = 'start';

  sceneSelect.addEventListener('change', handleSelectChange);
  formatSelect.addEventListener('change', handleSelectChange);
  backgroundSelect.addEventListener('change', handleSelectChange);
  musicSelect.addEventListener('change', handleSelectChange);
  photosInput.addEventListener('change', handlePhotosChange);
  modalCloseBtn.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', function (evt) {
    if (evt.target === modalOverlay) closeModal();
  });
  document.addEventListener('keydown', handleEscClose);
  renderBtn.addEventListener('click', function () {
    if (renderBtn.dataset.mode === 'start') {
      generateStartFrame();
    } else {
      startPaidRender();
    }
  });

  initToolbar();
  loadCatalog();
}

document.addEventListener('DOMContentLoaded', init);

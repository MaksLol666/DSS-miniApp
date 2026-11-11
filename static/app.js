(function () {
'use strict';
let tg = null;
let initData = '';
function captureTelegram() {
  tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  initData = tg && tg.initData ? tg.initData : '';
  return !!tg;
}
function waitForTelegram(timeoutMs) {
  if (captureTelegram()) return Promise.resolve(true);
  return new Promise(resolve => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (captureTelegram()) { clearInterval(timer); resolve(true); }
      else if (Date.now() - started >= timeoutMs) { clearInterval(timer); resolve(false); }
    }, 50);
  });
}
const content = document.getElementById('content');
const state = {
  user: null, page: 'catalog', categories: [], products: [], cart: {items: [], promos: []},
  profile: null, club: null, clubActivities: null, season: null, adminClubTasks: [], adminClubConfig: null, adminClubCosmetics: [], orders: [], promos: [], favorites: [], top: [], topref: [], search: '', category: 0,
  delivery: 'pickup', reviewOrder: 0, leaderKind: 'buyers', adminTab: 'orders',
  adminProducts: [], adminOrders: [], adminUsers: [], adminPromos: [], adminReviews: [], adminStats: null, adminQuery: '', galleryProductId: 0, galleryIndex: 0
};
let toastTimer = 0;
let captchaPromise = null;
let captchaResolve = null;
let captchaReject = null;
let captchaWidget = null;
let captchaSiteKey = '';
let captchaScriptLoaded = false;

function esc(value) {
  return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function money(value) {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(Number(value || 0)) + ' ₽';
}
function dateText(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}
function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2700);
}
function haptic(type) {
  try { if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred(type || 'light'); } catch (_e) {}
}
function setupTelegram() {
  try {
    if (!tg) return;
    tg.ready();
    tg.expand();
    tg.setHeaderColor('#0b0b10');
    tg.setBackgroundColor('#0b0b10');
  } catch (_e) {}
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs || 15000);
  try {
    return await fetch(url, Object.assign({}, options || {}, { signal: controller.signal }));
  } catch (error) {
    if (controller.signal.aborted || (error && error.name === 'AbortError')) {
      throw new Error('Сервер не ответил за 15 секунд. Проверь соединение и попробуй ещё раз.');
    }
    throw new Error('Не удалось соединиться с сервером. Проверь интернет и попробуй ещё раз.');
  } finally {
    clearTimeout(timeout);
  }
}

async function api(path, options, allowCaptchaRetry) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {});
  if (initData) headers['X-Init-Data'] = initData;
  if (opts.body && !(opts.body instanceof FormData) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetchWithTimeout(path, Object.assign({}, opts, { headers, credentials: 'include' }), 15000);
  const body = await response.json().catch(() => ({ detail: 'Сервер вернул неожиданный ответ.' }));
  if (response.status === 428 && body.detail === 'CAPTCHA_REQUIRED' && allowCaptchaRetry !== false) {
    await showCaptcha();
    return api(path, opts, false);
  }
  if (!response.ok) throw new Error(body.detail || ('Ошибка запроса (' + response.status + ').'));
  return body;
}

function showCaptcha() {
  if (captchaPromise) return captchaPromise;
  captchaPromise = new Promise((resolve, reject) => {
    captchaResolve = resolve;
    captchaReject = reject;
    const root = document.getElementById('modal-root');
    root.innerHTML = '<div class="modal-backdrop"><div class="modal"><h2>Проверка безопасности</h2><p>Для продолжения пройди проверку DDoS-Guard.</p><div id="captcha-widget"></div><div id="captcha-status"></div><div class="row"><button class="secondary" id="captcha-retry">Обновить</button><button class="secondary" id="captcha-close">Закрыть</button></div></div></div>';
    document.getElementById('captcha-close').onclick = function () {
      document.getElementById('modal-root').innerHTML = '';
      if (captchaReject) captchaReject(new Error('Проверка CAPTCHA отменена.'));
    };
    document.getElementById('captcha-retry').onclick = function () {
      const sdk = getCaptchaSdk();
      try {
        if (sdk && typeof sdk.reset === 'function' && captchaWidget !== null) {
          sdk.reset(captchaWidget);
        } else {
          mountCaptcha();
        }
      } catch (_e) { mountCaptcha(); }
    };
    fetchWithTimeout('/api/captcha/config', {}, 10000).then(r => r.json()).then(config => {
      if (!config.enabled || !config.site_key) {
        document.getElementById('captcha-status').textContent = 'CAPTCHA не настроена на сервере. Обратись к администратору.';
        return;
      }
      captchaSiteKey = config.site_key;
      window.initCaptcha = function () { captchaScriptLoaded = true; mountCaptcha(); };
      if (captchaScriptLoaded) mountCaptcha();
      else if (!document.getElementById('ddos-captcha-sdk')) {
        const script = document.createElement('script');
        script.id = 'ddos-captcha-sdk';
        script.src = 'https://captcha.ddos-guard.net/static/api.js?render=explicit&onload=initCaptcha';
        script.async = true;
        script.defer = true;
        script.onerror = function () {
          const status = document.getElementById('captcha-status');
          if (status) status.textContent = 'Не получилось загрузить проверку CAPTCHA. Попробуй снова.';
        };
        document.head.appendChild(script);
      }
    }).catch(() => {
      const status = document.getElementById('captcha-status');
      if (status) status.textContent = 'Не удалось получить настройки CAPTCHA.';
    });
  }).finally(() => {
    captchaPromise = null;
    captchaResolve = null;
    captchaReject = null;
  });
  return captchaPromise;
}
function getCaptchaSdk() {
  const apiObject = window.ddgcaptcha;
  if (apiObject && typeof apiObject.ready === 'function' && typeof apiObject.render === 'function') {
    return apiObject;
  }
  return null;
}

function mountCaptcha() {
  const target = document.getElementById('captcha-widget');
  if (!target) return;
  const sdk = getCaptchaSdk();
  const status = document.getElementById('captcha-status');
  if (!sdk) {
    if (status) status.textContent = 'API dCAPTCHA не найден. Проверь загрузку https://captcha.ddos-guard.net/static/api.js.';
    return;
  }

  sdk.ready(() => {
    const currentTarget = document.getElementById('captcha-widget');
    if (!currentTarget) return;
    try {
      currentTarget.innerHTML = '';
      const onSuccess = function (token) {
        api('/api/captcha/verify', { method: 'POST', body: JSON.stringify({ token }) }, false).then(() => {
          const done = captchaResolve;
          document.getElementById('modal-root').innerHTML = '';
          if (done) done(true);
        }).catch(error => {
          const currentStatus = document.getElementById('captcha-status');
          if (currentStatus) currentStatus.textContent = error.message || 'Проверка не пройдена.';
          try { if (captchaWidget !== null) sdk.reset(captchaWidget); } catch (_e) {}
        });
      };
      captchaWidget = sdk.render('captcha-widget', {
        sitekey: captchaSiteKey,
        callback: onSuccess
      });
    } catch (error) {
      if (status) status.textContent = 'Не удалось показать CAPTCHA: ' + error.message;
    }
  });
}

function renderHeader() {
  const name = state.user ? (state.user.username || ('ID ' + state.user.user_id)) : 'DSS STORE';
  const initial = String(name).replace('@', '').trim().charAt(0).toUpperCase() || 'D';
  return '<div class="brand"><div class="logo">DSS</div><div><div class="brand-name">DEAD SMOKE STORE</div><div class="brand-sub">Магазин · Mini App</div></div></div>' +
    '<button class="user-chip" data-action="go" data-page="profile"><span class="avatar">' + esc(initial) + '</span><span class="user-name">' + esc(name) + '</span></button>';
}
function renderNav() {
  const tabs = [['catalog', '⌂', 'Каталог'], ['cart', '▣', 'Корзина'], ['orders', '◷', 'Заказы'], ['profile', '◉', 'Профиль']];
  let html = '<div class="nav-inner">';
  for (const tab of tabs) {
    html += '<button class="nav-button ' + (state.page === tab[0] ? 'active' : '') + '" data-action="go" data-page="' + tab[0] + '"><span class="nav-icon">' + tab[1] + '</span><span>' + tab[2] + '</span>' +
      (tab[0] === 'cart' && state.cart.items && state.cart.items.length ? '<span class="nav-badge">' + state.cart.items.reduce((sum, item) => sum + Number(item.quantity || 1), 0) + '</span>' : '') + '</button>';
  }
  return html + '</div>';
}
function render() {
  document.getElementById('topbar').innerHTML = renderHeader();
  const nav = document.getElementById('bottomnav');
  nav.innerHTML = renderNav();
  nav.hidden = false;
  if (!state.user) { content.innerHTML = '<div class="loading"><i class="spinner"></i><span>Проверяем Telegram…</span></div>'; return; }
  if (state.user.is_banned) { content.innerHTML = '<div class="empty"><div class="empty-emoji">⛔</div><b>Доступ ограничен</b><p>Администратор заблокировал этот аккаунт.</p></div>'; return; }
  const pages = {
    catalog: renderCatalog, cart: renderCart, checkout: renderCheckout, orders: renderOrders,
    profile: renderProfile, club: renderClub, favorites: renderFavorites, promos: renderPromos, leaders: renderLeaders, admin: renderAdmin
  };
  content.innerHTML = (pages[state.page] || renderCatalog)();
  if (state.page === 'catalog') renderProductGrid();
}
function productSymbol(product) {
  const category = state.categories.find(c => c.id === product.category_id);
  const name = category ? category.name.toLowerCase() : '';
  if (name.includes('устрой')) return '▰';
  if (name.includes('расход')) return '✦';
  if (name.includes('пласт')) return '♫';
  if (name.includes('снюс')) return '◉';
  if (name.includes('набор')) return '✧';
  return '◈';
}
function renderProductGrid() {
  const grid = document.getElementById('product-grid');
  if (!grid) return;
  const products = state.products.filter(p => {
    const matchesCategory = !state.category || p.category_id === state.category;
    const term = state.search.trim().toLowerCase();
    return matchesCategory && (!term || (p.brand + ' ' + p.flavor).toLowerCase().includes(term));
  });
  if (!products.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1"><div class="empty-emoji">⌕</div>По этому запросу товаров нет.</div>';
    return;
  }
  grid.innerHTML = products.map(p => {
    const inCart = (state.cart.items || []).find(i => i.product_id === p.id);
    const isFavorite = state.favorites.some(x => x.id === p.id);
    return '<article class="product-card"><div class="product-art">' + (p.images && p.images.length ? '<img src="' + esc(p.images[0]) + '" alt="' + esc(p.brand) + '" loading="lazy" decoding="async" style="width:100%;height:100%;object-fit:cover;border-radius:14px">' : '<span class="product-symbol">' + productSymbol(p) + '</span>') + '<span class="product-no">№ ' + esc(p.number) + '</span><button class="favorite-toggle" data-action="favorite-toggle" data-id="' + p.id + '" aria-label="' + (isFavorite ? 'Убрать из избранного' : 'В избранное') + '">' + (isFavorite ? '♥' : '♡') + '</button><span class="stock-dot"></span></div>' +
      '<div class="product-body"><div class="product-brand">' + esc(p.brand) + '</div><div class="product-flavor">' + esc(p.flavor) + '</div>' + (p.images && p.images.length ? '<button class="mini-link" data-action="view-product" data-id="' + p.id + '" style="margin-top:5px">Фотографии · ' + p.images.length + '</button>' : '') + '<div class="item-sub" style="margin-top:6px">В наличии: ' + Number(p.stock_qty || 0) + ' шт.</div><div class="product-foot"><div class="price">' + money(p.price) + '</div>' +
      (inCart ? '<span class="muted">В корзине: ' + Number(inCart.quantity || 1) + '</span>' : '') +
      '<button class="add-button" data-action="add-cart" data-id="' + p.id + '" aria-label="Добавить в корзину" ' + (inCart && Number(inCart.quantity || 1) >= Number(p.stock_qty || 0) ? 'disabled' : '') + '>+</button></div></div></article>';
  }).join('');
}
function renderProductGallery(productId) {
  const product = state.products.find(p => p.id === Number(productId));
  if (!product || !product.images || !product.images.length) return;
  state.galleryProductId = product.id;
  state.galleryIndex = Math.max(0, Math.min(state.galleryIndex, product.images.length - 1));
  const index = state.galleryIndex;
  const root = document.getElementById('modal-root');
  root.innerHTML = '<div class="modal-backdrop" data-gallery-backdrop><div class="modal" role="dialog" aria-modal="true" aria-label="Фотографии товара" style="max-height:90vh;overflow-y:auto"><div class="row"><b>' + esc(product.brand) + ' — ' + esc(product.flavor) + '</b><button class="mini-link" data-action="gallery-close">Закрыть ✕</button></div><div style="margin-top:12px;background:#111;border-radius:14px;overflow:hidden"><img src="' + esc(product.images[index]) + '" alt="' + esc(product.brand) + ', фото ' + (index + 1) + '" style="display:block;width:100%;max-height:62vh;object-fit:contain"></div><div class="row" style="justify-content:space-between;margin-top:10px"><button class="secondary" data-action="gallery-prev" ' + (index === 0 ? 'disabled' : '') + '>← Назад</button><span class="muted">' + (index + 1) + ' / ' + product.images.length + '</span><button class="secondary" data-action="gallery-next" ' + (index >= product.images.length - 1 ? 'disabled' : '') + '>Далее →</button></div>' + (product.images.length > 1 ? '<div class="row" style="gap:7px;overflow-x:auto;margin-top:12px">' + product.images.map((url, n) => '<button data-action="gallery-select" data-index="' + n + '" aria-label="Фото ' + (n + 1) + '" style="padding:2px;border:2px solid ' + (n === index ? '#a855f7' : 'transparent') + ';background:transparent;border-radius:9px;flex:0 0 auto"><img src="' + esc(url) + '" alt="" style="width:58px;height:58px;object-fit:cover;border-radius:7px;display:block"></button>').join('') + '</div>' : '') + '</div></div>';
}
function renderCatalog() {
  let chips = '<button class="chip ' + (!state.category ? 'active' : '') + '" data-action="category" data-id="0">Все товары</button>';
  for (const c of state.categories) chips += '<button class="chip ' + (state.category === c.id ? 'active' : '') + '" data-action="category" data-id="' + c.id + '">' + esc(c.name) + '</button>';
  return '<section class="hero"><div class="eyebrow">DEAD SMOKE STORE / 01</div><h1>Твой выбор.<br>Твой стиль.</h1><p>Каталог магазина — выбирай товары, собирай корзину и оформляй заказ в пару нажатий.</p><div class="hero-line"><span class="hero-note">' + state.products.length + ' позиций в наличии</span><button class="ghost-button" data-action="go" data-page="cart">Корзина →</button></div></section>' +
    '<div class="section-head"><div><h2 class="section-title">Каталог</h2><div class="section-sub">Найди то, что нужно</div></div></div>' +
    '<div class="search-wrap"><span>⌕</span><input id="catalog-search" class="search" placeholder="Поиск по бренду или вкусу" value="' + esc(state.search) + '" autocomplete="off"></div>' +
    '<div class="chip-list">' + chips + '</div><div id="product-grid" class="product-grid"></div>';
}
function renderCart() {
  const cart = state.cart || { items: [], promos: [] };
  if (!cart.items || !cart.items.length) return '<div class="section-head"><div><h1 class="section-title">Корзина</h1><div class="section-sub">Тут появятся выбранные товары</div></div></div><div class="empty"><div class="empty-emoji">▣</div><b>Пока пусто</b><p>Добавь товар из каталога.</p><button class="primary" data-action="go" data-page="catalog" style="margin-top:10px">Перейти в каталог</button></div>';
  const items = cart.items.map(i => '<div class="item-row"><div class="item-thumb">◈</div><div class="item-info"><div class="item-title">' + esc(i.brand) + '</div><div class="item-sub">' + esc(i.flavor) + '</div><div class="item-sub">В наличии: ' + Number(i.stock_qty || 0) + ' шт.</div></div><div style="text-align:right"><b>' + money(i.price * Number(i.quantity || 1)) + '</b><div class="row" style="justify-content:flex-end;gap:7px;margin-top:6px"><button class="small-button" data-action="decrease-cart" data-id="' + i.product_id + '" ' + (Number(i.quantity || 1) <= 1 ? 'disabled' : '') + '>−</button><b>' + Number(i.quantity || 1) + ' шт.</b><button class="small-button" data-action="increase-cart" data-id="' + i.product_id + '" ' + (Number(i.quantity || 1) >= Number(i.stock_qty || 0) ? 'disabled' : '') + '>+</button></div><button class="mini-link" data-action="remove-cart" data-id="' + i.product_id + '">Убрать</button></div></div>').join('');
  const promos = (cart.promos || []).map(p => '<div class="summary-line"><span class="discount">🎟 ' + esc(p.code) + ' · ' + (p.kind === 'percent' ? esc(p.amount) + '%' : '−' + money(p.amount)) + '</span><span class="muted">' + (p.target_type === 'product' ? 'на товар' : p.target_type === 'category' ? 'на категорию' : 'на всё') + (p.applicable === false ? ' · условия не выполнены' : ' · активно') + '</span><button class="mini-link" data-action="remove-cart-promo" data-code="' + esc(p.code) + '">Убрать</button></div>').join('');
  return '<div class="section-head"><div><h1 class="section-title">Корзина</h1><div class="section-sub">' + cart.items.reduce((sum, i) => sum + Number(i.quantity || 1), 0) + ' шт. товаров</div></div><button class="mini-link" data-action="clear-cart">Очистить</button></div>' +
    '<div class="card">' + items + '</div><div class="card"><div class="row"><b>DSS CLUB</b><span class="muted">100 DCP = 1 ₽</span></div><div class="item-sub">Баланс: ' + Number(cart.club_balance || 0) + ' DCP · Фиксированная скидка по корзине: ' + money(cart.club_discount_offer || 0) + '</div><div class="item-sub">Подходящие товары: ' + money(cart.club_eligible_subtotal || 0) + '</div>' + (Number(cart.club_points || 0) > 0 ? '<div class="summary-line"><b>Списывается ' + Number(cart.club_points) + ' DCP</b><b class="discount">−' + money(cart.club_discount) + '</b></div><button class="secondary full-button" data-action="remove-club-dcp" type="button">Отменить списание DCP</button>' : (cart.club_can_redeem ? '<button class="secondary full-button" data-action="use-club-dcp" type="button">Получить скидку ' + money(cart.club_discount_offer) + ' ₽ за ' + Number(cart.club_required_points || 0) + ' DCP</button>' : '<button class="secondary full-button" type="button" disabled>Клубная скидка недоступна</button><div class="item-sub">' + esc(cart.club_unavailable_reason || 'Условия обмена не выполнены.') + '</div>')) + '<div class="item-sub">Участвуют товары от 500₽. Клубная скидка не суммируется с обычными промокодами.</div></div>' + '<div class="card"><div class="row"><b>Промокод</b><span class="muted">до 2 на заказ</span></div><form id="promo-form"><label class="field-label">Код скидки</label><div class="row"><input class="field" name="code" maxlength="16" required placeholder="Например, DSS2026"><button class="secondary" type="submit">Применить</button></div></form>' +
    promos + '<div class="summary-line"><span>Сумма товаров</span><b>' + money(cart.subtotal) + '</b></div><div class="summary-line"><span>Процентная скидка</span><span class="discount">−' + money(cart.percent_discount_amount || 0) + '</span></div><div class="summary-line"><span>Скидка фиксированная</span><span class="discount">−' + money(cart.fixed_discount) + '</span></div>' + (Number(cart.club_discount || 0) > 0 ? '<div class="summary-line"><span>Скидка DSS CLUB</span><span class="discount">−' + money(cart.club_discount) + '</span></div>' : '') + '<div class="summary-line total"><span>Итого без доставки</span><span>' + money(cart.total) + '</span></div><button class="primary" data-action="go" data-page="checkout" style="margin-top:10px">Оформить заказ →</button></div>';
}
function renderCheckout() {
  const cart = state.cart || { items: [], total: 0 };
  const cdek = state.delivery === 'cdek';
  if (!cart.items || !cart.items.length) return '<div class="empty">Корзина пуста. <button class="mini-link" data-action="go" data-page="catalog">Открыть каталог</button></div>';
  return '<div class="section-head"><div><h1 class="section-title">Оформление</h1><div class="section-sub">Заказ отправится администратору на подтверждение</div></div></div><form id="checkout-form"><div class="card"><b>Способ получения</b>' +
    '<label class="delivery-option ' + (!cdek ? 'selected' : '') + '"><input type="radio" name="delivery" value="pickup" ' + (!cdek ? 'checked' : '') + '><span><span class="option-title">🏠 Самовывоз</span><div class="option-desc">Без дополнительной платы</div></span></label>' +
    '<label class="delivery-option ' + (cdek ? 'selected' : '') + '"><input type="radio" name="delivery" value="cdek" ' + (cdek ? 'checked' : '') + '><span><span class="option-title">🚚 Доставка СДЭК</span><div class="option-desc">Доплата 500 ₽</div></span></label>' +
    (cdek ? '<label class="field-label">ФИО получателя</label><input name="delivery_name" class="field" required minlength="3" maxlength="128" autocomplete="name" placeholder="Иван Иванов"><label class="field-label">Телефон</label><input name="delivery_phone" class="field" required maxlength="32" autocomplete="tel" placeholder="+7…"><label class="field-label">Город, адрес или пункт СДЭК</label><textarea name="delivery_address" class="field" required minlength="5" maxlength="512" placeholder="Город, адрес или код пункта выдачи"></textarea>' : '') +
    '</div><div class="card"><div class="summary-line"><span>Сумма товаров</span><b>' + money(cart.subtotal) + '</b></div>' + (Number(cart.percent_discount_amount || 0) + Number(cart.fixed_discount || 0) > 0 ? '<div class="summary-line"><span>Скидка по промокодам</span><span class="discount">−' + money(Number(cart.percent_discount_amount || 0) + Number(cart.fixed_discount || 0)) + '</span></div>' : '') + (Number(cart.club_discount || 0) > 0 ? '<div class="summary-line"><span>Скидка DSS CLUB</span><span class="discount">−' + money(cart.club_discount) + '</span></div>' : '') + '<div class="summary-line"><span>К оплате за товары</span><b>' + money(cart.total) + '</b></div><div class="summary-line"><span>Доставка</span><b>' + (cdek ? money(500) : 'Бесплатно') + '</b></div><div class="summary-line total"><span>К оплате</span><span>' + money(Number(cart.total) + (cdek ? 500 : 0)) + '</span></div><button class="primary" type="submit" style="margin-top:12px">Подтвердить заказ</button><button class="secondary" type="button" data-action="go" data-page="cart" style="width:100%;margin-top:9px">Вернуться в корзину</button></div></form>';
}
function statusInfo(status) {
  const map = {accepted:['pending','Принят'], assembling:['pending','В сборке'], assembled:['pending','Собран'], in_transit:['pending','В пути'], completed:['approved','Выполнен'], rejected:['rejected','Отклонён'], pending:['pending','Принят'], approved:['approved','Выполнен']};
  return map[status] || ['pending','Обрабатывается'];
}
function renderFavorites() {
  if (!state.favorites.length) return '<div class="section-head"><div><h1 class="section-title">Избранное</h1><div class="section-sub">Товары, которые ты сохранил</div></div></div><div class="empty"><div class="empty-emoji">♡</div>Здесь пока пусто.<p>Нажимай на сердечко в карточке товара, чтобы сохранить понравившееся.</p><button class="primary" data-action="go" data-page="catalog">Открыть каталог</button></div>';
  return '<div class="section-head"><div><h1 class="section-title">Избранное</h1><div class="section-sub">' + state.favorites.length + ' сохранённых товаров</div></div><button class="mini-link" data-action="reload-favorites">Обновить</button></div><div class="card">' + state.favorites.map(p => '<div class="item-row"><div class="item-thumb">' + (p.images && p.images.length ? '<img src="' + esc(p.images[0]) + '" alt="" loading="lazy" decoding="async" style="width:100%;height:100%;object-fit:cover;border-radius:10px">' : '♡') + '</div><div class="item-info"><div class="item-title">' + esc(p.brand) + '</div><div class="item-sub">' + esc(p.flavor) + ' · ' + (p.in_stock ? 'В наличии: ' + p.stock_qty + ' шт.' : 'Нет в наличии') + '</div><div class="price">' + money(p.price) + '</div></div><button class="small-button" data-action="add-cart" data-id="' + p.id + '" ' + (!p.in_stock || !p.stock_qty ? 'disabled' : '') + '>В корзину</button><button class="mini-link" data-action="favorite-toggle" data-id="' + p.id + '" aria-label="Убрать из избранного">♥</button></div>').join('') + '</div>';
}
function renderOrders() {
  if (!state.orders.length) return '<div class="section-head"><div><h1 class="section-title">Мои заказы</h1><div class="section-sub">История покупок</div></div></div><div class="empty"><div class="empty-emoji">◷</div>Заказов пока нет.<p>Оформленный заказ появится здесь.</p><button class="primary" data-action="go" data-page="catalog">В каталог</button></div>';
  return '<div class="section-head"><div><h1 class="section-title">Мои заказы</h1><div class="section-sub">Последние оформленные заказы</div></div><button class="mini-link" data-action="reload-orders">Обновить</button></div>' +
    state.orders.map(o => {
      const st = statusInfo(o.status);
      const items = (o.items || []).map(i => '<div class="summary-line"><span>' + esc(i.brand) + ' — ' + esc(i.flavor) + ' × ' + Number(i.quantity || 1) + '</span><span>' + money(i.price * Number(i.quantity || 1)) + '</span></div>').join('');
      let review = '';
      if (o.status === 'completed' && !o.has_review) {
        if (state.reviewOrder === o.id) review = '<form id="review-form" data-order-id="' + o.id + '"><label class="field-label">Оценка</label><select class="field" name="rating"><option value="5">5 — Отлично</option><option value="4">4 — Хорошо</option><option value="3">3 — Нормально</option><option value="2">2 — Плохо</option><option value="1">1 — Очень плохо</option></select><label class="field-label">Отзыв</label><textarea class="field" name="text" required minlength="3" maxlength="2048" placeholder="Расскажи о покупке"></textarea><label class="field-label">Фотография (необязательно)</label><label class="file-picker">📷 Прикрепить фото<input type="file" name="photo" accept="image/jpeg,image/png,image/webp"><span class="file-picker-hint">JPG, PNG или WEBP · до 5 МБ</span></label><button class="primary" type="submit" style="margin-top:10px">Отправить отзыв</button></form>';
        else review = '<button class="secondary" data-action="write-review" data-id="' + o.id + '" style="width:100%;margin-top:10px">Оставить отзыв</button>';
      } else if (o.has_review) review = '<div class="item-sub">Спасибо за отзыв 💚</div>';
      const history = (o.status_history || []).map(h => '<div class="item-sub">✓ ' + esc(statusInfo(h.status)[1]) + ' · ' + dateText(h.changed_at) + '</div>').join('');
      return '<article class="list-card"><div class="row"><b>' + esc(o.order_number || ('DSS-' + String(o.id).padStart(6, '0'))) + '</b><span class="status ' + st[0] + '">' + st[1] + '</span></div><div class="item-sub" style="margin:6px 0 11px">' + dateText(o.created_at) + ' · ' + (o.delivery_type === 'cdek' ? 'Доставка' : 'Самовывоз') + '</div>' + items + '<div class="summary-line total"><span>Итого</span><span>' + money(o.total) + '</span></div>' + (Number(o.dcp_spent || 0) > 0 ? '<div class="item-sub">Скидка DSS CLUB: −' + money(o.club_discount) + ' · списано ' + Number(o.dcp_spent) + ' DCP</div>' : '') + (history ? '<div class="divider"></div><div class="item-sub">История статусов</div>' + history : '') + review + '</article>';
    }).join('');
}
function renderProfile() {
  const p = state.profile || {};
  const name = [p.first_name, p.last_name].filter(Boolean).join(' ') || p.username || ('ID ' + p.user_id);
  const initial = String(name).trim().charAt(0).toUpperCase() || 'D';
  const avatar = p.photo_url ? '<img src="' + esc(p.photo_url) + '" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%">' : esc(initial);
  return '<div class="section-head"><div><h1 class="section-title">Личный кабинет</h1><div class="section-sub">Твой профиль в Dead Smoke Store</div></div></div>' +
    '<div class="card"><div class="profile-head"><div class="profile-avatar">' + avatar + '</div><div><div class="profile-name">' + esc(name) + '</div><div class="item-sub">' + (p.telegram_username ? '@' + esc(String(p.telegram_username).replace('@','')) + ' · ' : '') + 'Telegram ID: ' + esc(p.user_id) + '</div></div></div><div class="divider"></div><div class="metric-grid"><div class="metric"><div class="metric-value">' + money(p.balance) + '</div><div class="metric-label">Всего потрачено</div></div><div class="metric"><div class="metric-value">' + esc(p.orders_count || 0) + '</div><div class="metric-label">Выполнено заказов</div></div><div class="metric"><div class="metric-value">' + state.favorites.length + '</div><div class="metric-label">В избранном</div></div><div class="metric"><div class="metric-value">' + esc(p.active_promos || 0) + '</div><div class="metric-label">Активных промокодов</div></div></div></div>' +
    '<div class="section-head"><h2 class="section-title">DSS CLUB</h2></div><div class="card" style="background:linear-gradient(135deg,#211334,#111019);border:1px solid #59358a"><div class="row" style="justify-content:space-between"><div><div class="eyebrow" style="color:#c4a5fd">MEMBERSHIP PROGRAM</div><div class="profile-name" style="color:#fff">Твой клубный профиль</div><div class="item-sub">Баллы, уровни и достижения</div></div><div style="font-size:30px;color:#c4a5fd">✦</div></div><button class="primary full-button" data-action="go" data-page="club" style="margin-top:12px">Открыть DSS CLUB →</button></div><div class="section-head"><h2 class="section-title">Мой магазин</h2></div><div class="card"><button class="secondary full-button" data-action="go" data-page="orders">📦 История и статусы заказов →</button><button class="secondary full-button" data-action="go" data-page="favorites">♡ Избранные товары (' + state.favorites.length + ') →</button><button class="secondary full-button" data-action="go" data-page="promos">🎟 Мои персональные промокоды →</button><div class="divider"></div><b>🏆 Рейтинги сообщества</b><button class="secondary full-button" data-action="leaders" data-kind="buyers">🥇 Топ покупателей →</button><button class="secondary full-button" data-action="leaders" data-kind="refs">👥 Топ реферов →</button>' +
    (p.ref_link ? '<div class="divider"></div><b>Пригласить друга</b><div class="item-sub">Поделись ссылкой на магазин</div><div class="row" style="margin-top:12px"><input class="field" id="ref-link" readonly value="' + esc(p.ref_link) + '" style="min-width:0;font-size:11px"><button class="secondary" data-action="copy-ref">Копировать</button></div><button class="secondary" data-action="share-ref" style="width:100%;margin-top:10px">Поделиться ссылкой</button>' : '') +
    (state.user && state.user.is_admin ? '<div class="divider"></div><button class="primary" data-action="go" data-page="admin">🛠 Открыть админ-панель</button>' : '') + '</div>';
}

function clubLevelMeta(title) {
  const key = String(title || 'MEMBER').toUpperCase().replace(/[^A-Z]/g, '');
  const map = {
    MEMBER: { icon: '◇', label: 'STARTER', tone: 'member' },
    ACTIVE: { icon: '✦', label: 'ACTIVE', tone: 'active' },
    PREMIUM: { icon: '✧', label: 'PREMIUM', tone: 'premium' },
    VIP: { icon: '♛', label: 'ELITE', tone: 'vip' },
    LEGEND: { icon: '♜', label: 'LEGEND', tone: 'legend' },
    AMBASSADOR: { icon: '♕', label: 'AMBASSADOR', tone: 'ambassador' }
  };
  return map[key] || map.MEMBER;
}
function clubRewardMeta(points) {
  const value = Number(points || 0);
  if (value >= 1500) return { icon: '✹', tone: 'mythic' };
  if (value >= 1000) return { icon: '⬡', tone: 'frame' };
  if (value >= 500) return { icon: '▣', tone: 'card' };
  if (value >= 250) return { icon: '✧', tone: 'title' };
  return { icon: '✦', tone: 'badge' };
}
function clubAchievementIcon(icon, title) {
  const value = String(icon || '');
  if (value && !/[?\uFFFD]/.test(value)) return value;
  const name = String(title || '').toLowerCase();
  if (name.includes('заказ')) return '▣';
  if (name.includes('рефер')) return '♧';
  if (name.includes('сезон')) return '✹';
  return '✦';
}

function renderClub() {
  const c = state.club || {}, tx = c.transactions || [], activity = state.clubActivities || {}, season = state.season || {};
  const nextText = c.next_level
    ? ('До ' + c.next_level + ': ' + Math.max(0, Number(c.next_level_orders || 0) - Number(c.completed_orders || 0)) + ' заказов · ' +
       Math.max(0, Number(c.next_level_points || 0) - Number(c.lifetime_earned || 0)) + ' Lifetime DCP' +
       (Number(c.next_level_referrals || 0) ? ' · ' + Math.max(0, Number(c.next_level_referrals) - Number(c.completed_referrals || 0)) + ' рефералов' : ''))
    : 'Достигнут максимальный уровень';
  const ledger = tx.length ? tx.map(t => '<div class="summary-line"><span><b>' + esc(t.reason) + '</b><div class="item-sub">' + dateText(t.created_at) + ' · Баланс: ' + esc(t.balance_after) + ' DCP</div></span><b class="discount">' + (Number(t.amount) >= 0 ? '+' : '') + esc(t.amount) + ' DCP</b></div>').join('') : '<div class="empty">Пока нет операций. DCP начисляются за завершённые заказы.</div>';
  const tasks = (activity.tasks || []).map(t => '<article class="list-card"><div class="row"><b>' + esc(t.title) + '</b><span class="discount">+' + esc(t.reward) + ' DCP</span></div><div class="item-sub">' + esc(t.description) + ' · ' + (t.period === 'daily' ? 'Ежедневно' : 'Еженедельно') + '</div><div class="item-sub" style="margin-top:8px">' + (t.completed ? '✓ Выполнено' : 'Прогресс: ' + esc(t.progress) + '/' + esc(t.target)) + '</div><div style="height:6px;background:#292431;border-radius:20px;overflow:hidden;margin-top:7px"><div style="height:100%;width:' + Math.min(100, Number(t.progress || 0) / Math.max(1, Number(t.target || 1)) * 100) + '%;background:#a78bfa"></div></div></article>').join('') || '<div class="empty">Активных заданий пока нет.</div>';
  const achievements = (activity.achievements || []).map(x => '<article class="club-achievement ' + (x.unlocked ? 'is-unlocked' : 'is-locked') + '"><div class="club-achievement-mark">' + esc(clubAchievementIcon(x.icon, x.title)) + '</div><div class="club-achievement-copy"><div class="row"><b>' + esc(x.title) + '</b><span class="club-achievement-state">' + (x.unlocked ? 'ОТКРЫТО' : 'ЗАКРЫТО') + '</span></div><div class="item-sub">' + esc(x.description) + '</div><div class="item-sub" style="margin-top:6px">' + (Number(x.reward || 0) ? 'Награда: +' + esc(x.reward) + ' DCP' : 'Косметическое достижение') + '</div>' + (x.unlocked && x.reward_type === 'title' ? '<button class="small-button" data-action="equip-club-title" data-key="' + esc(x.key) + '" style="margin-top:8px">' + (activity.selected_title === x.key ? '✓ Установлен' : 'Установить титул') + '</button>' : '') + '</div></article>').join('');
  const cosmetics = activity.cosmetics || [];
  const selectedTitle = (activity.achievements || []).find(x => x.key === activity.selected_title) || cosmetics.find(x => x.key === activity.selected_title);
  const perks = (c.level_perks || []).map(p => '<div class="club-perk"><span>✦</span>' + esc(p) + '</div>').join('');
  const levels = (c.levels || []).map((level, index) => { const meta = clubLevelMeta(level.title); return '<article class="club-level club-level-' + meta.tone + ' ' + (level.unlocked ? 'is-unlocked' : 'is-locked') + '"><div class="club-level-top"><span class="club-level-emblem">' + meta.icon + '</span><span class="club-level-state">' + (level.unlocked ? 'ОТКРЫТ' : 'ЗАКРЫТ') + '</span></div><b class="club-level-title">' + esc(level.title) + '</b><div class="club-level-tag">' + meta.label + '</div><div class="club-level-requirements"><span>▣ ' + esc(level.orders) + ' заказов</span><span>◈ ' + esc(level.points) + ' Lifetime DCP</span>' + (Number(level.referrals || 0) ? '<span>♧ ' + esc(level.referrals) + ' рефералов</span>' : '') + '</div></article>'; }).join('');
  const seasonRewards = (season.personal_rewards || []).map(r => { const meta = clubRewardMeta(r.points); return '<article class="season-reward season-reward-' + meta.tone + ' ' + (r.unlocked ? 'is-unlocked' : 'is-locked') + '"><span class="season-reward-icon">' + meta.icon + '</span><div class="season-reward-copy"><b>' + esc(r.points) + ' SP</b><span>' + esc(r.title) + '</span><small>' + (r.unlocked ? '✓ Награда открыта' : 'Осталось ' + Math.max(0, Number(r.points) - Number(season.points || 0)) + ' SP') + '</small></div></article>'; }).join('');
  const leaderboard = (season.leaderboard || []).slice(0, 5).map(r => '<div class="season-rank-row"><span class="season-rank-number">#' + esc(r.position) + '</span><span>' + esc(r.username ? '@' + r.username : 'Участник ' + r.user_id) + '</span><b>' + esc(r.points) + ' SP</b></div>').join('') || '<div class="item-sub">Рейтинг появится после старта сезона 1 декабря.</div>';
  const seasonPanel = '<section class="season-panel"><div class="season-orbit"></div><div class="row"><div><div class="eyebrow">DSS CLUB · LIMITED SERIES</div><h2 class="season-title">' + esc(season.title || 'Season 01 · Новый год') + '</h2><div class="season-edition">COLLECT · COMPETE · UNLOCK</div></div><span class="season-status">' + (season.active ? 'АКТИВЕН' : 'СКОРО') + '</span></div><div class="season-meta">' + (season.active ? 'До завершения: ' + esc(season.days_left) + ' дней' : 'Старт: 1 декабря 2026 · 00:00 МСК') + '</div><div class="season-score"><span class="season-score-token">✹</span><strong>' + esc(season.points || 0) + '</strong><span>SEASON POINTS</span></div><div class="season-meta">' + (season.rank ? 'Твоё место: #' + esc(season.rank) : 'Твоё место появится после первых сезонных очков') + ' · Участников: ' + esc(season.participants || 0) + '</div><div class="season-divider"></div><b class="season-subtitle">Личные сезонные награды</b><div class="season-rewards">' + seasonRewards + '</div><div class="season-divider"></div><div class="row"><b class="season-subtitle">TOP-50 сезона</b><span class="season-meta">Награды за места</span></div>' + leaderboard + '</section>';
  const rewardHistory = (season.earned_rewards || []).map(r => '<div class="summary-line"><span>' + esc(r.title) + '<div class="item-sub">Сезон ' + esc(r.season) + (r.rank ? ' · место #' + esc(r.rank) : '') + '</div></span><b>✓</b></div>').join('');
  const cosmeticInventory = cosmetics.map(x => '<article class="list-card"><div class="row"><b>' + esc(x.icon) + ' ' + esc(x.title) + '</b><span class="item-sub">' + esc(x.rarity) + ' · ' + esc(x.kind) + '</span></div><div class="item-sub">' + esc(x.description || 'Клубная косметика') + '</div>' + (x.kind === 'title' ? '<button class="small-button" data-action="equip-club-title" data-key="' + esc(x.key) + '" style="margin-top:8px">' + (activity.selected_title === x.key ? '✓ Установлен' : 'Установить титул') + '</button>' : '') + '</article>').join('');
  return '<div class="section-head"><div><h1 class="section-title">DSS CLUB</h1><div class="section-sub">Твой клубный статус и прогресс</div></div><button class="mini-link" data-action="go" data-page="profile">Назад</button></div>' +
    '<div class="club-hero club-hero-' + clubLevelMeta(c.level_title).tone + '"><div class="club-hero-orbit"></div><div class="club-hero-head"><div><div class="eyebrow">DSS CLUB · MEMBERSHIP</div><div class="club-level-name">' + esc(c.level_title || 'MEMBER') + '</div><div class="club-member-caption">YOUR STATUS · ' + clubLevelMeta(c.level_title).label + '</div></div><div class="club-hero-seal"><span>' + clubLevelMeta(c.level_title).icon + '</span><small>EST. DSS</small></div></div><div class="club-balance-label">ДОСТУПНЫЙ БАЛАНС</div><div class="club-balance"><span class="club-currency-icon">D</span>' + esc(c.balance || 0) + ' <span>DCP</span></div><div class="divider"></div><div class="metric-grid"><div class="metric"><div class="metric-value">' + esc(c.lifetime_earned || 0) + '</div><div class="metric-label">Lifetime DCP</div></div><div class="metric"><div class="metric-value">' + esc(c.completed_orders || 0) + '</div><div class="metric-label">Завершено заказов</div></div></div><div class="item-sub club-next">' + esc(nextText) + '</div><div class="club-progress"><div style="width:' + Math.max(0, Math.min(100, Number(c.progress_percent || 0))) + '%"></div></div><div class="item-sub club-progress-label">Прогресс до следующего уровня · ' + Math.max(0, Math.min(100, Number(c.progress_percent || 0))) + '%</div></div>' +
    '<div class="club-title-card"><div class="club-title-art"><span>' + clubLevelMeta(c.level_title).icon + '</span><small>PERSONAL INSIGNIA</small></div><div class="club-title-info"><div class="item-sub">ТЕКУЩИЙ ТИТУЛ</div><b>' + esc(selectedTitle ? selectedTitle.title : 'Без титула') + '</b><div class="club-perk-heading">ПРИВИЛЕГИИ ' + esc(c.level_title || 'MEMBER') + '</div>' + (perks || '<div class="item-sub">Базовый доступ к клубу</div>') + '</div></div>' +
    seasonPanel +
    '<div class="section-head"><div><h2 class="section-title">Уровни клуба</h2><div class="section-sub">Для перехода нужны все условия уровня</div></div></div><div class="club-level-list">' + levels + '</div>' +
    '<div class="section-head"><div><h2 class="section-title">Задания</h2><div class="section-sub">Ежедневные и еженедельные активности</div></div></div>' + tasks +
    '<div class="section-head"><div><h2 class="section-title">Достижения</h2><div class="section-sub">Клубные значки и титулы</div></div></div>' + (achievements || '<div class="empty">Достижения скоро появятся.</div>') +
    '<div class="section-head"><div><h2 class="section-title">Моя коллекция</h2><div class="section-sub">Титулы, значки, рамки и эффекты</div></div></div>' + (cosmeticInventory || '<div class="empty">Пока нет косметических предметов.</div>') +
    '<div class="section-head"><div><h2 class="section-title">История сезонных наград</h2></div></div><div class="card">' + (rewardHistory || '<div class="empty">Сезонных наград пока нет.</div>') + '</div>' +
    '<div class="section-head"><div><h2 class="section-title">История DCP</h2><div class="section-sub">Последние начисления клуба</div></div></div><div class="card">' + ledger + '</div><div class="item-sub" style="text-align:center;margin:12px 0">DCP — внутренняя бонусная валюта клуба. Не является денежным балансом и не выводится.</div>';
}
function renderPromos() {
  if (!state.promos.length) return '<div class="section-head"><div><h1 class="section-title">Мои промокоды</h1><div class="section-sub">Доступные скидки</div></div></div><div class="empty"><div class="empty-emoji">🎟</div>Активных промокодов пока нет.<p>Приглашай друзей и возвращайся после покупок.</p></div>';
  return '<div class="section-head"><div><h1 class="section-title">Мои промокоды</h1><div class="section-sub">Можно применить к заказу</div></div></div>' + state.promos.map(p => '<div class="list-card"><div class="row"><div><div class="item-title">' + esc(p.code) + '</div><div class="item-sub">' + (p.kind === 'percent' ? 'Процентная скидка' : 'Скидка в рублях') + '</div></div><div class="price discount">' + (p.kind === 'percent' ? '−' + esc(p.amount) + '%' : '−' + money(p.amount)) + '</div></div><div class="row" style="margin-top:12px"><span class="item-sub">Использовано ' + esc(p.used_count) + ' из ' + esc(p.max_uses) + '</span><button class="secondary" data-action="use-promo" data-code="' + esc(p.code) + '">В корзину</button></div></div>').join('');
}
function renderLeaders() {
  const refs = state.leaderKind === 'refs';
  const list = refs ? state.topref : state.top;
  const title = refs ? 'Топ реферов' : 'Топ покупателей';
  if (!list.length) return '<div class="section-head"><div><h1 class="section-title">' + title + '</h1><div class="section-sub">Рейтинг сообщества</div></div></div><div class="tabs"><button class="tab ' + (!refs ? 'active' : '') + '" data-action="leaders" data-kind="buyers">Покупатели</button><button class="tab ' + (refs ? 'active' : '') + '" data-action="leaders" data-kind="refs">Реферы</button></div><div class="empty">Пока нет данных для рейтинга.</div><button class="secondary full-button" data-action="go" data-page="profile">Назад в профиль</button>';
  return '<div class="section-head"><div><h1 class="section-title">' + title + '</h1><div class="section-sub">Топ-10 участников</div></div></div><div class="tabs"><button class="tab ' + (!refs ? 'active' : '') + '" data-action="leaders" data-kind="buyers">Покупатели</button><button class="tab ' + (refs ? 'active' : '') + '" data-action="leaders" data-kind="refs">Реферы</button></div><div class="card">' + list.map((u, i) => '<div class="item-row"><div class="item-thumb">' + (i + 1) + '</div><div class="item-info"><div class="item-title">' + esc(u.username ? '@' + String(u.username).replace('@', '') : 'Пользователь ' + u.user_id) + '</div><div class="item-sub">ID ' + esc(u.user_id) + '</div></div><b class="discount">' + (refs ? esc(u.value) + ' чел.' : money(u.value)) + '</b></div>').join('') + '</div><button class="secondary full-button" data-action="go" data-page="profile">Назад в профиль</button>';
}
function adminTabs() {
  const tabs = [['orders', 'Заказы'], ['products', 'Товары'], ['users', 'Пользователи'], ['promos', 'Промокоды'], ['reviews', 'Отзывы'], ['club', 'DSS CLUB'], ['stats', 'Статистика'], ['broadcast', 'Рассылка']];
  return '<div class="tabs">' + tabs.map(t => '<button class="tab ' + (state.adminTab === t[0] ? 'active' : '') + '" data-action="admin-tab" data-tab="' + t[0] + '">' + t[1] + '</button>').join('') + '</div>';
}
function renderAdmin() {
  if (!state.user || !state.user.is_admin) return '<div class="empty">Нет доступа к админ-панели.</div>';
  let body = '';
  if (state.adminTab === 'orders') {
    const list = state.adminOrders || [];
    body = '<div class="row" style="margin:12px 0"><b>Последние заказы</b><button class="mini-link" data-action="reload-admin">Обновить</button></div>' + (list.length ? list.map(o => {
      const st = statusInfo(o.status);
      const next = o.status === 'accepted' ? [['assembling','Начать сборку'],['rejected','Отклонить']] :
        o.status === 'assembling' ? [['assembled','Отметить собранным'],['rejected','Отклонить']] :
        o.status === 'assembled' ? (o.delivery_type === 'cdek' ? [['in_transit','Передать в доставку'],['rejected','Отклонить']] : [['completed','Выдать заказ'],['rejected','Отклонить']]) :
        o.status === 'in_transit' ? [['completed','Завершить заказ']] : [];
      const statusButtons = next.length ? '<div class="row" style="margin-top:12px">' + next.map(x => '<button class="' + (x[0] === 'rejected' ? 'danger' : 'primary') + '" data-action="change-order-status" data-status="' + x[0] + '" data-id="' + o.id + '">' + x[1] + '</button>').join('') + '</div>' : '';
      const history = (o.status_history || []).map(h => '<div class="item-sub">✓ ' + esc(statusInfo(h.status)[1]) + ' · ' + dateText(h.changed_at) + '</div>').join('');
      return '<article class="list-card"><div class="row"><b>' + esc(o.order_number || ('DSS-' + String(o.id).padStart(6, '0'))) + '</b><span class="status ' + st[0] + '">' + st[1] + '</span></div><div class="item-sub" style="margin:6px 0">ID ' + o.user_id + ' · ' + esc(o.username || 'без username') + ' · ' + dateText(o.created_at) + '</div>' +
        (o.items || []).map(i => '<div class="summary-line"><span>' + esc(i.brand) + ' — ' + esc(i.flavor) + ' × ' + Number(i.quantity || 1) + '</span><span>' + money(i.price * Number(i.quantity || 1)) + '</span></div>').join('') +
        '<div class="summary-line"><span>' + (o.delivery_type === 'cdek' ? 'СДЭК · ' + esc(o.delivery_name || '') + ' · ' + esc(o.delivery_phone || '') + ' · ' + esc(o.delivery_address || '') : 'Самовывоз') + '</span><b>' + money(o.total) + '</b></div>' +
        (o.promo_codes ? '<div class="item-sub">Промокоды: ' + esc(o.promo_codes) + '</div>' : '') +
        (history ? '<div class="divider"></div><div class="item-sub">История статусов</div>' + history : '') + statusButtons + '</article>';
    }).join('') : '<div class="empty">Заказов пока нет.</div>');
  } else if (state.adminTab === 'products') {
    body = '<form id="admin-product-form" class="card"><b>Добавить товар</b><label class="field-label">Категория</label><select name="category_id" class="field" required>' + state.categories.map(c => '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('') + '</select><div class="form-row"><div><label class="field-label">Бренд</label><input class="field" name="brand" required maxlength="64"></div><div><label class="field-label">Вкус / вариант</label><input class="field" name="flavor" required maxlength="64"></div></div><label class="field-label">Цена продажи, ₽</label><input class="field" type="number" min="1" step="0.01" name="price" required><label class="field-label">Закупочная цена за 1 шт., ₽ (только администратор)</label><input class="field" type="number" min="0" step="0.01" name="cost_price" placeholder="Не указана"><label class="field-label">Количество на складе, шт.</label><input class="field" type="number" min="0" max="1000000" step="1" name="stock_qty" value="1" required><label class="field-label">Фотографии товара</label><label class="file-picker">📷 Выбрать фотографии (до 5)<input id="admin-product-images" type="file" name="images" accept="image/jpeg,image/png,image/webp" multiple><span class="file-picker-hint" id="admin-product-images-hint">Нажми, чтобы открыть галерею телефона</span></label><div class="item-sub">JPG, PNG или WEBP · до 5 МБ на фото</div><button class="primary" type="submit" style="margin-top:12px">Добавить товар</button></form>' +
      state.adminProducts.map(p => '<article class="admin-product" data-product="' + p.id + '"><div class="row"><b>Товар №' + p.number + '</b><span class="status ' + (p.in_stock ? 'approved' : 'rejected') + '">' + (p.in_stock ? 'В наличии' : 'Скрыт') + '</span></div><div class="item-sub">Доступно: ' + Number(p.stock_qty || 0) + ' шт. · В резерве: ' + Number(p.reserved_qty || 0) + ' шт.</div><div class="admin-product-grid" style="margin-top:10px"><input class="field" name="brand" value="' + esc(p.brand) + '"><input class="field" name="flavor" value="' + esc(p.flavor) + '"><input class="field" name="price" type="number" min="1" step="0.01" value="' + esc(p.price) + '"><input class="field" name="cost_price" type="number" min="0" step="0.01" value="' + (p.cost_price == null ? "" : esc(p.cost_price)) + '" placeholder="Закупка/шт."><input class="field" name="stock_qty" type="number" min="0" max="1000000" value="' + esc(p.stock_qty || 0) + '" placeholder="Остаток, шт."><select class="field" name="category_id">' + state.categories.map(c => '<option value="' + c.id + '" ' + (c.id === p.category_id ? 'selected' : '') + '>' + esc(c.name) + '</option>').join('') + '</select></div>' + (p.images || []).map((url, index) => '<div class="row" style="margin-top:8px"><img src="' + esc(url) + '" alt="Фото товара" loading="lazy" decoding="async" style="width:64px;height:64px;object-fit:cover;border-radius:8px"><span class="item-sub">Фото ' + (index + 1) + '</span><button class="danger" data-action="delete-product-image" data-id="' + p.id + '" data-index="' + index + '">Убрать</button></div>').join('') + '<label class="field-label" style="margin-top:8px">Добавить фотографии</label><label class="file-picker">📷 Выбрать фото<input type="file" accept="image/jpeg,image/png,image/webp" multiple data-product-images="' + p.id + '"><span class="file-picker-hint">Можно добавить до 5 фото всего</span></label><div class="row" style="margin-top:9px"><button class="small-button" data-action="save-product" data-id="' + p.id + '">Сохранить</button><button class="small-button" data-action="toggle-product" data-id="' + p.id + '">' + (p.in_stock ? 'Скрыть' : 'Вернуть') + '</button><button class="danger" data-action="delete-product" data-id="' + p.id + '">Удалить</button></div></article>').join('');
  } else if (state.adminTab === 'users') {
    body = '<form id="user-search" class="card"><label class="field-label">Поиск по ID, @username или имени</label><div class="row"><input name="q" class="field" value="' + esc(state.adminQuery) + '" placeholder="@username или ID" style="min-width:0"><button class="secondary" type="submit">Найти</button></div></form>' +
      (state.adminUsers || []).map(u => '<article class="list-card"><div class="row"><b>' + esc(u.username ? '@' + String(u.username).replace('@', '') : 'ID ' + u.id) + '</b><span class="status ' + (u.is_banned ? 'rejected' : 'approved') + '">' + (u.is_banned ? 'Заблокирован' : 'Активен') + '</span></div><div class="item-sub" style="margin:5px 0 10px">ID ' + u.id + ' · Покупок ' + u.orders_count + ' · Рефералов ' + u.referrals_count + ' · Потрачено ' + money(u.balance) + '</div><div class="row">' + (u.is_banned ? '<button class="small-button" data-action="unban-user" data-id="' + u.id + '">Разблокировать</button>' : '<button class="danger" data-action="ban-user" data-id="' + u.id + '">Заблокировать</button>') + '<button class="small-button" data-action="reset-user" data-id="' + u.id + '">Сбросить статистику</button></div></article>').join('');
  } else if (state.adminTab === 'promos') {
    const targetOptions = '<option value="all">Весь каталог</option>' +
      (state.categories || []).map(x => '<option value="category:' + x.id + '">Категория: ' + esc(x.name) + ' (ID ' + x.id + ')</option>').join('') +
      (state.adminProducts || []).map(x => '<option value="product:' + x.id + '">Товар №' + x.number + ' · ' + esc(x.brand) + ' — ' + esc(x.flavor) + '</option>').join('');
    body = '<form id="admin-promo-form" class="card"><b>Создать промокод / акцию</b><label class="field-label">Код (пусто = сгенерировать)</label><input class="field" name="code" maxlength="16" placeholder="DSS2026"><div class="form-row"><div><label class="field-label">Тип скидки</label><select class="field" name="kind"><option value="fixed">Сумма, ₽</option><option value="percent">Процент, %</option></select></div><div><label class="field-label">Размер</label><input class="field" name="amount" type="number" min="0.01" step="0.01" required></div></div><label class="field-label">Где действует</label><select class="field" name="target">' + targetOptions + '</select><label class="field-label">Минимальная сумма корзины, ₽ (0 = без ограничения)</label><input class="field" name="min_order" type="number" min="0" step="0.01" value="0" required><div class="form-row"><div><label class="field-label">Владелец (0 = все)</label><input class="field" name="owner_id" type="number" min="0" value="0" required></div><div><label class="field-label">Лимит активаций</label><input class="field" name="max_uses" type="number" min="1" value="1" required></div></div><div class="form-row"><div><label class="field-label">Начало (МСК, необязательно)</label><input class="field" name="starts_at" type="datetime-local"></div><div><label class="field-label">Окончание (МСК, необязательно)</label><input class="field" name="expires_at" type="datetime-local"></div></div><button class="primary" type="submit" style="margin-top:12px">Создать</button></form>' +
      (state.adminPromos || []).map(p => {
        const target = p.target_type === 'product' ? 'товар ID ' + p.target_id : p.target_type === 'category' ? 'категория ID ' + p.target_id : 'весь каталог';
        const schedule = (p.starts_at ? 'с ' + String(p.starts_at).replace('T', ' ') : '') + (p.expires_at ? ' до ' + String(p.expires_at).replace('T', ' ') : '');
        return '<article class="list-card"><div class="row"><b>' + esc(p.code) + '</b><span class="price">' + (p.kind === 'percent' ? '−' + esc(p.amount) + '%' : '−' + money(p.amount)) + '</span></div><div class="item-sub">' + esc(target) + ' · минимум ' + money(p.min_order || 0) + ' · владелец ' + p.owner_id + '</div><div class="item-sub">Использований ' + p.used_count + '/' + p.max_uses + ' · ' + (p.active ? 'по времени активно' : 'вне срока действия') + (schedule ? ' · ' + esc(schedule) : '') + (p.used ? ' · лимит исчерпан' : '') + '</div><button class="danger" data-action="delete-promo" data-code="' + esc(p.code) + '" style="margin-top:10px">Удалить</button></article>';
      }).join('');
  } else if (state.adminTab === 'reviews') {
    body = (state.adminReviews || []).length ? state.adminReviews.map(r => '<article class="list-card"><div class="row"><b>Заказ №' + esc(r.order_id) + '</b><span class="discount">' + '★'.repeat(Number(r.rating || 0)) + '☆'.repeat(5 - Number(r.rating || 0)) + '</span></div><div class="item-sub" style="margin:6px 0 10px">Пользователь ID ' + esc(r.user_id) + ' · ' + dateText(r.published_at) + '</div><p>' + esc(r.text || 'Без текста') + '</p>' + (r.photo_url ? '<img src="' + esc(r.photo_url) + '" alt="Фото к отзыву" loading="lazy" style="display:block;width:100%;max-height:320px;object-fit:contain;background:#111;border-radius:12px;margin-top:10px">' : '') + '</article>').join('') : '<div class="empty">Отзывов пока нет.</div>';
  } else if (state.adminTab === 'club') {
    const cfg = state.adminClubConfig || {};
    const economy = cfg.economy || {};
    body = '<div class="card"><b>Экономика и правила DSS CLUB</b><p class="item-sub">Изменяй бонусы, курсы DCP, пороги обмена, уровни, достижения и сезонные награды. Настройки сохраняются в базе данных.</p><form id="club-config-form"><label class="field-label">Полная конфигурация клуба (JSON)</label><textarea class="field" name="config_json" rows="18" spellcheck="false" style="font-family:monospace;font-size:12px;min-height:300px">' + esc(JSON.stringify(cfg, null, 2)) + '</textarea><button class="primary" type="submit" style="margin-top:10px">Сохранить настройки DSS CLUB</button></form></div>' +
      '<div class="card"><b>Создать достижение</b><form id="club-achievement-form"><label class="field-label">Ключ (латиница)</label><input class="field" name="key" maxlength="48" pattern="[a-zA-Z0-9_]+" required placeholder="ten_orders"><label class="field-label">Название</label><input class="field" name="title" maxlength="96" required><label class="field-label">Описание</label><input class="field" name="description" maxlength="256" required><div class="form-row"><div><label class="field-label">Условие</label><select class="field" name="metric"><option value="orders">Завершённые заказы</option><option value="referrals">Купившие рефералы</option><option value="reviews">Опубликованные отзывы</option></select></div><div><label class="field-label">Количество</label><input class="field" name="target" type="number" min="1" max="1000000" value="1" required></div></div><div class="form-row"><div><label class="field-label">Награда DCP</label><input class="field" name="reward" type="number" min="0" max="1000000" value="0" required></div><div><label class="field-label">Косметическая награда</label><select class="field" name="reward_type"><option value="badge">Значок</option><option value="title">Титул</option></select></div></div><label class="field-label">Иконка</label><input class="field" name="icon" maxlength="16" value="🏆"><button class="primary" type="submit" style="margin-top:10px">Создать достижение</button></form></div>' +
      '<div class="section-head"><h3 class="section-title">Достижения</h3></div>' +
      (cfg.achievements || []).map(x => '<article class="list-card"><div class="row"><b>' + esc(x.icon || '✦') + ' ' + esc(x.title) + '</b><span class="item-sub">' + esc(x.metric) + ' · ' + esc(x.target) + '</span></div><div class="item-sub">' + esc(x.key) + ' · +' + esc(x.reward || 0) + ' DCP · ' + (x.active === false ? 'выключено' : 'активно') + '</div><button class="danger" data-action="delete-club-achievement" data-key="' + esc(x.key) + '" style="margin-top:8px">Удалить достижение</button></article>').join('') +
      '<div class="card"><b>Ручное начисление / списание DCP</b><form id="club-balance-form"><label class="field-label">Telegram ID пользователя</label><input class="field" name="user_id" type="number" min="1" required><label class="field-label">Количество DCP (минус = списать)</label><input class="field" name="amount" type="number" min="-10000000" max="10000000" required><label class="field-label">Причина операции</label><input class="field" name="reason" maxlength="256" required placeholder="Компенсация / бонус / корректировка"><button class="primary" type="submit" style="margin-top:10px">Изменить баланс</button></form></div>' +
      '<div class="card"><b>Создать задание</b><form id="club-task-form"><label class="field-label">Уникальный ключ (латиница)</label><input class="field" name="key" maxlength="48" pattern="[a-zA-Z0-9_]+" required placeholder="weekend_orders"><label class="field-label">Название</label><input class="field" name="title" maxlength="96" required><label class="field-label">Описание</label><input class="field" name="description" maxlength="256"><div class="form-row"><div><label class="field-label">Период</label><select class="field" name="period"><option value="daily">Ежедневное</option><option value="weekly">Еженедельное</option></select></div><div><label class="field-label">Действие</label><select class="field" name="event_type"><option value="orders">Завершённые заказы</option><option value="reviews">Опубликованные отзывы</option></select></div></div><div class="form-row"><div><label class="field-label">Цель</label><input class="field" name="target" type="number" min="1" max="100000" value="1" required></div><div><label class="field-label">Награда DCP</label><input class="field" name="reward" type="number" min="0" max="100000" value="30" required></div></div><button class="primary" type="submit" style="margin-top:10px">Создать задание</button></form></div>' +
      '<div class="section-head"><h3 class="section-title">Активные и сохранённые задания</h3></div>' +
      (state.adminClubTasks || []).map(t => '<article class="list-card club-task-admin" data-task="' + esc(t.key) + '"><div class="row"><b>' + esc(t.title) + '</b><span class="item-sub">' + esc(t.key) + '</span></div><div class="form-row"><div><label class="field-label">Название</label><input class="field" name="title" maxlength="96" value="' + esc(t.title) + '"></div><div><label class="field-label">Период</label><select class="field" name="period"><option value="daily" ' + (t.period === 'daily' ? 'selected' : '') + '>Ежедневное</option><option value="weekly" ' + (t.period === 'weekly' ? 'selected' : '') + '>Еженедельное</option></select></div></div><label class="field-label">Описание</label><input class="field" name="description" maxlength="256" value="' + esc(t.description) + '"><div class="form-row"><div><label class="field-label">Тип действия</label><select class="field" name="event_type"><option value="orders" ' + (t.event_type === 'orders' ? 'selected' : '') + '>Заказы</option><option value="reviews" ' + (t.event_type === 'reviews' ? 'selected' : '') + '>Отзывы</option></select></div><div><label class="field-label">Цель</label><input class="field" type="number" min="1" max="100000" name="target" value="' + esc(t.target) + '"></div></div><div class="form-row"><div><label class="field-label">Награда DCP</label><input class="field" type="number" min="0" max="100000" name="reward" value="' + esc(t.reward) + '"></div><div><label class="field-label">Статус</label><select class="field" name="active"><option value="true" ' + (t.active ? 'selected' : '') + '>Включено</option><option value="false" ' + (!t.active ? 'selected' : '') + '>Отключено</option></select></div></div><div class="row"><button class="small-button" data-action="save-club-task">Сохранить</button><button class="danger" data-action="delete-club-task">Удалить</button></div></article>').join('') +
      '<div class="card"><b>Создать косметику / титул</b><form id="club-cosmetic-form"><label class="field-label">Ключ</label><input class="field" name="key" maxlength="48" pattern="[a-zA-Z0-9_]+" required placeholder="founder_title"><label class="field-label">Название</label><input class="field" name="title" maxlength="96" required><div class="form-row"><div><label class="field-label">Тип</label><select class="field" name="kind"><option value="title">Титул</option><option value="badge">Значок</option><option value="frame">Рамка</option><option value="effect">Эффект</option><option value="card">Карточка</option></select></div><div><label class="field-label">Иконка / эмодзи</label><input class="field" name="icon" maxlength="16" value="✦"></div></div><div class="form-row"><div><label class="field-label">Редкость</label><select class="field" name="rarity"><option value="common">Обычная</option><option value="rare">Редкая</option><option value="epic">Эпическая</option><option value="legendary">Легендарная</option></select></div><div><label class="field-label">Описание</label><input class="field" name="description" maxlength="256"></div></div><button class="primary" type="submit" style="margin-top:10px">Создать косметику</button></form></div>' +
      '<div class="card"><b>Выдать косметику пользователю</b><form id="club-grant-form"><label class="field-label">Telegram ID пользователя</label><input class="field" name="user_id" type="number" min="1" required><label class="field-label">Предмет</label><select class="field" name="cosmetic_key">' + (state.adminClubCosmetics || []).filter(x => x.active).map(x => '<option value="' + esc(x.key) + '">' + esc(x.title) + ' · ' + esc(x.kind) + '</option>').join('') + '</select><button class="primary" type="submit" style="margin-top:10px" ' + (!(state.adminClubCosmetics || []).length ? 'disabled' : '') + '>Выдать предмет</button></form></div>' +
      '<div class="section-head"><h3 class="section-title">Каталог косметики</h3></div>' +
      (state.adminClubCosmetics || []).map(x => '<article class="list-card"><div class="row"><b>' + esc(x.icon) + ' ' + esc(x.title) + '</b><span class="item-sub">' + esc(x.kind) + ' · ' + esc(x.rarity) + ' · ' + (x.active ? 'активно' : 'выключено') + '</span></div><div class="item-sub">' + esc(x.key) + ' · ' + esc(x.description || '') + '</div><div class="row"><button class="small-button" data-action="toggle-club-cosmetic" data-key="' + esc(x.key) + '" data-active="' + (!x.active) + '">' + (x.active ? 'Отключить' : 'Включить') + '</button><button class="danger" data-action="delete-club-cosmetic" data-key="' + esc(x.key) + '">Удалить</button></div></article>').join('') +
      '<div class="item-sub">Квесты поддерживают события «завершённый заказ» и «опубликованный отзыв». Достижения и уровни редактируются в JSON выше; титулы и косметику можно создавать здесь и выдавать конкретным пользователям.</div>';
  } else if (state.adminTab === 'stats') {
    const s = state.adminStats;
    body = '<div class="row" style="margin:12px 0"><b>Период</b><select class="field" id="stats-period"><option value="today" ' + ((state.statsPeriod || "30d") === "today" ? "selected" : "") + '>Сегодня</option><option value="yesterday" ' + (state.statsPeriod === "yesterday" ? "selected" : "") + '>Вчера</option><option value="7d" ' + (state.statsPeriod === "7d" ? "selected" : "") + '>7 дней</option><option value="30d" ' + ((state.statsPeriod || "30d") === "30d" ? "selected" : "") + '>30 дней</option><option value="all" ' + (state.statsPeriod === "all" ? "selected" : "") + '>Всё время</option></select></div>' + (s ? '<div class="metric-grid"><div class="metric"><div class="metric-value">' + s.total_users + '</div><div class="metric-label">Пользователей</div></div><div class="metric"><div class="metric-value">' + s.total_orders + '</div><div class="metric-label">Выполнено заказов</div></div><div class="metric"><div class="metric-value">' + s.pending_orders + '</div><div class="metric-label">Заказов в работе</div></div><div class="metric"><div class="metric-value">' + money(s.total_revenue) + '</div><div class="metric-label">Выручка за период</div></div><div class="metric"><div class="metric-value">' + money(s.total_cost) + '</div><div class="metric-label">Себестоимость</div></div><div class="metric"><div class="metric-value">' + (s.gross_profit == null ? '—' : money(s.gross_profit)) + '</div><div class="metric-label">Валовая прибыль</div></div></div><div class="card"><div class="summary-line"><span>За сутки</span><b>' + money(s.revenue_day) + '</b></div><div class="summary-line"><span>За неделю</span><b>' + money(s.revenue_week) + '</b></div><div class="summary-line"><span>Средний чек</span><b>' + money(s.avg_check) + '</b></div><div class="summary-line"><span>Учтено себестоимости</span><b>' + s.cost_coverage + '%</b></div><div class="summary-line"><span>Проданные единицы без закупочной цены</span><b>' + s.untracked_units + '</b></div><div class="divider"></div><b>Топ продаж по количеству</b>' + (s.top_products || []).map((p, i) => '<div class="summary-line"><span>' + (i + 1) + '. ' + esc(p.brand) + ' — ' + esc(p.flavor) + '</span><b>' + p.quantity + ' шт.</b></div>').join('') + '<div class="divider"></div><b>Самые прибыльные товары</b>' + (s.top_profit_products || []).map((p, i) => '<div class="summary-line"><span>' + (i + 1) + '. ' + esc(p.brand) + ' — ' + esc(p.flavor) + '</span><b>' + (p.cost_tracked ? money(p.profit) : "Нет закупки") + '</b></div>').join('') + '</div>' : '<div class="empty">Загружаем статистику…</div>');
  } else if (state.adminTab === 'broadcast') {
    body = '<div class="card"><b>📢 Рассылка пользователям</b><p class="item-sub">Сообщение будет отправлено пользователям, доступным боту. Не используй рассылку для частых уведомлений.</p><form id="broadcast-form"><label class="field-label">Текст сообщения</label><textarea class="field" name="text" required maxlength="4000" placeholder="Текст рассылки…"></textarea><button class="primary" type="submit" style="margin-top:12px">Запустить рассылку</button></form></div>';
  }
  return '<div class="section-head"><div><h1 class="section-title">Админ-панель</h1><div class="section-sub">Управление магазином</div></div><button class="mini-link" data-action="go" data-page="profile">Закрыть</button></div>' + adminTabs() + '<div id="admin-body">' + body + '</div>';
}

async function uploadProductImages(productId, files) {
  if (!files || !files.length) return;
  if (files.length > 5) throw new Error('Можно выбрать не больше 5 фотографий.');
  const body = new FormData();
  Array.from(files).forEach(file => body.append('files', file));
  const headers = {};
  if (initData) headers['X-Init-Data'] = initData;
  const send = () => fetch('/api/admin/products/' + productId + '/images', { method: 'POST', headers, body, credentials: 'include' });
  let response = await send();
  let result = await response.json().catch(() => ({}));
  if (response.status === 428 && result.detail === 'CAPTCHA_REQUIRED') {
    await showCaptcha();
    response = await send();
    result = await response.json().catch(() => ({}));
  }
  if (!response.ok) throw new Error(result.detail || 'Не удалось загрузить фотографии.');
  return result;
}

async function loadBase() {
  const result = await Promise.all([api('/api/categories'), api('/api/products'), api('/api/cart'), api('/api/profile')]);
  state.categories = result[0]; state.products = result[1]; state.cart = result[2]; state.profile = result[3];
  state.user.is_admin = !!result[3].is_admin;
}
async function loadAdminTab(tab) {
  state.adminTab = tab;
  if (tab === 'orders') state.adminOrders = await api('/api/admin/orders');
  if (tab === 'products') state.adminProducts = await api('/api/admin/products');
  if (tab === 'users') state.adminUsers = await api('/api/admin/users' + (state.adminQuery ? '?q=' + encodeURIComponent(state.adminQuery) : ''));
  if (tab === 'promos') {
    const promoData = await Promise.all([api('/api/admin/promos'), api('/api/admin/products')]);
    state.adminPromos = promoData[0]; state.adminProducts = promoData[1];
  }
  if (tab === 'reviews') state.adminReviews = await api('/api/admin/reviews');
  if (tab === 'club') {
    const data = await Promise.all([api('/api/admin/club/tasks'), api('/api/admin/club/config'), api('/api/admin/club/cosmetics')]);
    state.adminClubTasks = data[0]; state.adminClubConfig = data[1]; state.adminClubCosmetics = data[2];
  }
  if (tab === 'stats') state.adminStats = await api('/api/admin/stats?period=' + encodeURIComponent(state.statsPeriod || '30d'));
}
async function loadPage(page) {
  if (page === 'orders') state.orders = await api('/api/orders');
  if (page === 'favorites') state.favorites = await api('/api/favorites');
  if (page === 'profile') state.profile = await api('/api/profile');
  if (page === 'club') { const data = await Promise.all([api('/api/club/activities'), api('/api/club'), api('/api/club/season')]); state.clubActivities = data[0]; state.club = data[1]; state.season = data[2]; }
  if (page === 'promos') state.promos = await api('/api/promos');
  if (page === 'leaders') {
    const data = await api(state.leaderKind === 'refs' ? '/api/topref' : '/api/top');
    if (state.leaderKind === 'refs') state.topref = data; else state.top = data;
  }
  if (page === 'admin') await loadAdminTab(state.adminTab);
}
async function go(page) {
  state.page = page;
  render();
  try { await loadPage(page); render(); }
  catch (error) { toast(error.message || 'Не удалось загрузить страницу.'); }
}
async function refreshCart() {
  state.cart = await api('/api/cart');
}

async function action(button) {
  const name = button.dataset.action;
  const id = Number(button.dataset.id || 0);
  if (name === 'go') { await go(button.dataset.page); return; }
  if (name === 'category') { state.category = id; render(); return; }
  if (name === 'view-product') { state.galleryIndex = 0; renderProductGallery(id); return; }
  if (name === 'gallery-close') { document.getElementById('modal-root').innerHTML = ''; state.galleryProductId = 0; return; }
  if (name === 'gallery-prev' || name === 'gallery-next' || name === 'gallery-select') {
    const product = state.products.find(p => p.id === state.galleryProductId);
    if (!product || !product.images || !product.images.length) return;
    if (name === 'gallery-prev') state.galleryIndex = Math.max(0, state.galleryIndex - 1);
    else if (name === 'gallery-next') state.galleryIndex = Math.min(product.images.length - 1, state.galleryIndex + 1);
    else state.galleryIndex = Math.max(0, Math.min(product.images.length - 1, Number(button.dataset.index)));
    renderProductGallery(product.id);
    return;
  }
  if (name === 'favorite-toggle') {
    try { await api('/api/favorites/toggle', { method: 'POST', body: JSON.stringify({ product_id: id }) }); state.favorites = await api('/api/favorites'); render(); toast(state.favorites.some(p => p.id === id) ? 'Добавлено в избранное' : 'Убрано из избранного'); } catch (error) { toast(error.message); } return;
  }
  if (name === 'reload-favorites') { try { state.favorites = await api('/api/favorites'); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'add-cart') {
    try { state.cart = await api('/api/cart/add', { method: 'POST', body: JSON.stringify({ product_id: id }) }); state.products = await api('/api/products'); haptic('light'); render(); toast('Добавлено в корзину'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'increase-cart' || name === 'decrease-cart') {
    const item = (state.cart.items || []).find(i => i.product_id === id);
    if (!item) return;
    const quantity = Number(item.quantity || 1) + (name === 'increase-cart' ? 1 : -1);
    try {
      state.cart = await api('/api/cart/quantity', { method: 'POST', body: JSON.stringify({ product_id: id, quantity }) });
      state.products = await api('/api/products');
      render();
    } catch (error) { toast(error.message); }
    return;
  }
  if (name === 'remove-cart') {
    try { await api('/api/cart/remove', { method: 'POST', body: JSON.stringify({ product_id: id }) }); await refreshCart(); render(); toast('Товар убран'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'clear-cart') {
    const clear = async () => { try { await api('/api/cart/clear', { method: 'POST' }); await refreshCart(); render(); toast('Корзина очищена'); } catch (error) { toast(error.message); } };
    if (tg && tg.showConfirm) tg.showConfirm('Очистить корзину?', ok => { if (ok) clear(); });
    else if (confirm('Очистить корзину?')) await clear();
    return;
  }
  if (name === 'copy-ref') {
    const input = document.getElementById('ref-link');
    try { await navigator.clipboard.writeText(input.value); toast('Ссылка скопирована'); }
    catch (_e) { input.select(); document.execCommand('copy'); toast('Ссылка скопирована'); }
    return;
  }
  if (name === 'share-ref') {
    const link = state.profile && state.profile.ref_link;
    if (!link) { toast('Ссылка появится после запуска бота'); return; }
    if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(link) + '&text=' + encodeURIComponent('Мой магазин'));
    else if (navigator.share) navigator.share({ title: 'Dead Smoke Store', text: 'Заходи в магазин по моей ссылке', url: link }).catch(() => {});
    else { try { await navigator.clipboard.writeText(link); toast('Ссылка скопирована'); } catch (_e) { toast(link); } }
    return;
  }
  if (name === 'leaders') { state.leaderKind = button.dataset.kind === 'refs' ? 'refs' : 'buyers'; await go('leaders'); return; }
  if (name === 'write-review') { state.reviewOrder = id; render(); return; }
  if (name === 'reload-orders') { try { state.orders = await api('/api/orders'); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'use-promo') {
    try { await api('/api/promo/apply', { method: 'POST', body: JSON.stringify({ code: button.dataset.code }) }); await refreshCart(); await go('cart'); toast('Промокод добавлен'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'use-club-dcp') {
    try { state.cart = await api('/api/club/redeem', { method: 'POST', body: JSON.stringify({ points: Number(state.cart.club_max_points || 0) }) }); render(); toast('Клубная скидка применена'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'remove-club-dcp') {
    try { state.cart = await api('/api/club/redeem/remove', { method: 'POST' }); render(); toast('Списание DCP отменено'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'remove-cart-promo') {
    try { await api('/api/promo/remove', { method: 'POST', body: JSON.stringify({ code: button.dataset.code }) }); await refreshCart(); render(); toast('Промокод убран'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'admin-tab') { try { await loadAdminTab(button.dataset.tab); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'reload-admin') { try { await loadAdminTab(state.adminTab); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'equip-club-title') {
    try { await api('/api/club/title', { method: 'POST', body: JSON.stringify({ title_key: button.dataset.key }) }); state.clubActivities = await api('/api/club/activities'); render(); toast('Титул установлен'); }
    catch (error) { toast(error.message); }
    return;
  }
  if (name === 'save-club-task') {
    const card = button.closest('.club-task-admin');
    const payload = {
      title: card.querySelector('[name="title"]').value,
      description: card.querySelector('[name="description"]').value,
      period: card.querySelector('[name="period"]').value,
      event_type: card.querySelector('[name="event_type"]').value,
      target: Number(card.querySelector('[name="target"]').value),
      reward: Number(card.querySelector('[name="reward"]').value),
      active: card.querySelector('[name="active"]').value === 'true'
    };
    try { await api('/api/admin/club/tasks/' + encodeURIComponent(card.dataset.task), { method: 'PUT', body: JSON.stringify(payload) }); await loadAdminTab('club'); render(); toast('Задание DSS CLUB сохранено'); }
    catch (error) { toast(error.message); }
    return;
  }
  if (name === 'delete-club-task') {
    if (!confirm('Удалить задание и его историю прогресса?')) return;
    try { await api('/api/admin/club/tasks/' + encodeURIComponent(button.closest('.club-task-admin').dataset.task), { method: 'DELETE' }); await loadAdminTab('club'); render(); toast('Задание удалено'); }
    catch (error) { toast(error.message); }
    return;
  }
  if (name === 'delete-club-achievement') {
    if (!confirm('Удалить достижение из правил клуба? Уже полученные награды и записи пользователей останутся в истории.')) return;
    try {
      const config = state.adminClubConfig || {};
      config.achievements = (config.achievements || []).filter(x => x.key !== button.dataset.key);
      await api('/api/admin/club/config', { method: 'PUT', body: JSON.stringify(config) });
      await loadAdminTab('club'); render(); toast('Достижение удалено');
    } catch (error) { toast(error.message); }
    return;
  }
  if (name === 'toggle-club-cosmetic') {
    try { await api('/api/admin/club/cosmetics/' + encodeURIComponent(button.dataset.key), { method: 'PUT', body: JSON.stringify({ active: button.dataset.active === 'true' }) }); await loadAdminTab('club'); render(); toast('Статус косметики изменён'); }
    catch (error) { toast(error.message); }
    return;
  }
  if (name === 'delete-club-cosmetic') {
    if (!confirm('Удалить косметический предмет?')) return;
    try { await api('/api/admin/club/cosmetics/' + encodeURIComponent(button.dataset.key), { method: 'DELETE' }); await loadAdminTab('club'); render(); toast('Предмет удалён'); }
    catch (error) { toast(error.message); }
    return;
  }
  if (name === 'save-product') {
    const card = button.closest('.admin-product');
    const costInput = card.querySelector('[name="cost_price"]').value;
    const payload = { brand: card.querySelector('[name="brand"]').value, flavor: card.querySelector('[name="flavor"]').value, price: Number(card.querySelector('[name="price"]').value), cost_price: costInput === "" ? null : Number(costInput), stock_qty: Number(card.querySelector('[name="stock_qty"]').value), category_id: Number(card.querySelector('[name="category_id"]').value) };
    try { await api('/api/admin/products/' + id, { method: 'PUT', body: JSON.stringify(payload) }); await loadAdminTab('products'); render(); toast('Товар сохранён'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'delete-product-image') {
    try { await api('/api/admin/products/' + id + '/images/' + Number(button.dataset.index), { method: 'DELETE' }); await loadAdminTab('products'); render(); toast('Фотография удалена'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'toggle-product') {
    const product = state.adminProducts.find(p => p.id === id);
    if (!product) return;
    try { await api('/api/admin/products/' + id, { method: 'PUT', body: JSON.stringify({ in_stock: !product.in_stock }) }); await loadAdminTab('products'); render(); toast(product.in_stock ? 'Товар скрыт' : 'Товар возвращён'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'delete-product') {
    if (!confirm('Удалить товар? Если он уже встречается в заказах, он будет скрыт для сохранения истории.')) return;
    try { await api('/api/admin/products/' + id, { method: 'DELETE' }); await loadAdminTab('products'); render(); toast('Готово'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'change-order-status') {
    const nextStatus = button.dataset.status;
    if (!confirm('Изменить статус заказа №' + id + ' на «' + statusInfo(nextStatus)[1] + '»?')) return;
    try { await api('/api/admin/orders/' + id + '/status', { method: 'POST', body: JSON.stringify({ status: nextStatus }) }); await loadAdminTab('orders'); render(); toast('Статус заказа обновлён'); } catch (error) { toast(error.message); } return;
  }
  if (name === 'approve-order' || name === 'reject-order') {
    const accept = name === 'approve-order';
    if (!confirm((accept ? 'Подтвердить' : 'Отклонить') + ' заказ №' + id + '?')) return;
    try { await api('/api/admin/orders/' + id + '/' + (accept ? 'approve' : 'reject'), { method: 'POST' }); await loadAdminTab('orders'); render(); toast(accept ? 'Заказ подтверждён' : 'Заказ отклонён'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'ban-user' || name === 'unban-user') {
    const method = name === 'ban-user' ? 'ban' : 'unban';
    if (!confirm((method === 'ban' ? 'Заблокировать' : 'Разблокировать') + ' пользователя ' + id + '?')) return;
    try { await api('/api/admin/users/' + id + '/' + method, { method: 'POST' }); await loadAdminTab('users'); render(); toast('Готово'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'reset-user') {
    if (!confirm('Сбросить потрачено, реферальные счётчики и промокоды? История заказов сохранится.')) return;
    try { await api('/api/admin/users/' + id + '/reset', { method: 'POST' }); await loadAdminTab('users'); render(); toast('Статистика сброшена'); }
    catch (error) { toast(error.message); } return;
  }
  if (name === 'delete-promo') {
    if (!confirm('Удалить промокод ' + button.dataset.code + '?')) return;
    try { await api('/api/admin/promos/' + encodeURIComponent(button.dataset.code), { method: 'DELETE' }); await loadAdminTab('promos'); render(); toast('Промокод удалён'); }
    catch (error) { toast(error.message); }
  }
}

document.addEventListener('click', event => {
  if (event.target.matches('[data-gallery-backdrop]')) { document.getElementById('modal-root').innerHTML = ''; state.galleryProductId = 0; return; }
  const button = event.target.closest('[data-action]');
  if (!button) return;
  event.preventDefault();
  action(button).catch(error => toast(error.message || 'Что-то пошло не так.'));
});
document.addEventListener('input', event => {
  if (event.target.id !== 'catalog-search') return;
  state.search = event.target.value;
  const cursor = state.search.length;
  renderProductGrid();
  const input = document.getElementById('catalog-search');
  if (input) { input.focus(); input.setSelectionRange(cursor, cursor); }
});
document.addEventListener('change', async event => {
  if (event.target.id === 'stats-period') { state.statsPeriod = event.target.value; try { await loadAdminTab("stats"); render(); } catch (error) { toast(error.message); } return; }
  if (event.target.name === 'delivery') { state.delivery = event.target.value; render(); return; }
  if (event.target.id === 'admin-product-images') {
    const hint = document.getElementById('admin-product-images-hint');
    if (hint) hint.textContent = event.target.files.length ? 'Выбрано фотографий: ' + event.target.files.length : 'Нажми, чтобы открыть галерею телефона';
    if (event.target.files.length > 5) { toast('Можно выбрать не больше 5 фотографий.'); event.target.value = ''; if (hint) hint.textContent = 'Нажми, чтобы открыть галерею телефона'; }
    return;
  }
  const input = event.target.closest('[data-product-images]');
  if (input && input.files && input.files.length) {
    try { await uploadProductImages(Number(input.dataset.productImages), input.files); await loadAdminTab('products'); render(); toast('Фотографии загружены'); }
    catch (error) { toast(error.message); input.value = ''; }
  }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  event.preventDefault();
  const data = new FormData(form);
  try {
    if (form.id === 'promo-form') {
      await api('/api/promo/apply', { method: 'POST', body: JSON.stringify({ code: String(data.get('code') || '') }) });
      await refreshCart(); render(); toast('Промокод применён');
    } else if (form.id === 'checkout-form') {
      const delivery = state.delivery;
      const payload = { delivery_type: delivery,
        delivery_name: delivery === 'cdek' ? String(data.get('delivery_name') || '').trim() : null,
        delivery_phone: delivery === 'cdek' ? String(data.get('delivery_phone') || '').trim() : null,
        delivery_address: delivery === 'cdek' ? String(data.get('delivery_address') || '').trim() : null };
      const order = await api('/api/checkout', { method: 'POST', body: JSON.stringify(payload) });
      await refreshCart(); await go('orders'); toast((order.order_number || ('№' + order.id)) + ' принят в обработку');
    } else if (form.id === 'review-form') {
      const reviewPayload = new FormData();
      reviewPayload.append('order_id', String(Number(form.dataset.orderId)));
      reviewPayload.append('rating', String(Number(data.get('rating'))));
      reviewPayload.append('text', String(data.get('text') || '').trim());
      const reviewPhoto = form.querySelector('[name="photo"]').files[0];
      if (reviewPhoto) reviewPayload.append('photo', reviewPhoto);
      await api('/api/review', { method: 'POST', body: reviewPayload });
      state.reviewOrder = 0; state.orders = await api('/api/orders'); render(); toast('Спасибо за отзыв!');
    } else if (form.id === 'admin-product-form') {
      const created = await api('/api/admin/products', { method: 'POST', body: JSON.stringify({ category_id: Number(data.get('category_id')), brand: String(data.get('brand') || '').trim(), flavor: String(data.get('flavor') || '').trim(), price: Number(data.get('price')), cost_price: data.get('cost_price') === "" ? null : Number(data.get('cost_price')), stock_qty: Number(data.get('stock_qty')) }) });
      const photos = form.querySelector('[name="images"]').files;
      if (photos.length) await uploadProductImages(created.id, photos);
      await loadAdminTab('products'); render(); toast('Товар добавлен');
    } else if (form.id === 'user-search') {
      state.adminQuery = String(data.get('q') || '').trim();
      state.adminUsers = await api('/api/admin/users' + (state.adminQuery ? '?q=' + encodeURIComponent(state.adminQuery) : ''));
      render();
    } else if (form.id === 'admin-promo-form') {
      const target = String(data.get('target') || 'all').split(':');
      const result = await api('/api/admin/promos', { method: 'POST', body: JSON.stringify({ code: String(data.get('code') || '').trim(), kind: String(data.get('kind')), amount: Number(data.get('amount')), owner_id: Number(data.get('owner_id')), max_uses: Number(data.get('max_uses')), target_type: target[0] === 'all' ? 'all' : target[0], target_id: target.length > 1 ? Number(target[1]) : null, min_order: Number(data.get('min_order') || 0), starts_at: String(data.get('starts_at') || '') || null, expires_at: String(data.get('expires_at') || '') || null }) });
      await loadAdminTab('promos'); render(); toast('Промокод создан: ' + result.code);
    } else if (form.id === 'club-config-form') {
      let config;
      try { config = JSON.parse(String(data.get('config_json') || '{}')); }
      catch (_e) { throw new Error('В конфигурации DSS CLUB ошибка JSON. Проверь запятые и скобки.'); }
      await api('/api/admin/club/config', { method: 'PUT', body: JSON.stringify(config) });
      await loadAdminTab('club'); render(); toast('Настройки DSS CLUB сохранены');
    } else if (form.id === 'club-achievement-form') {
      const config = state.adminClubConfig || {};
      config.achievements = config.achievements || [];
      const key = String(data.get('key') || '').trim().toLowerCase();
      if (config.achievements.some(x => x.key === key)) throw new Error('Достижение с таким ключом уже существует.');
      config.achievements.push({
        key, title: String(data.get('title') || '').trim(),
        description: String(data.get('description') || '').trim(),
        icon: String(data.get('icon') || '🏆'),
        metric: String(data.get('metric') || 'orders'),
        target: Number(data.get('target')), reward: Number(data.get('reward')),
        reward_type: String(data.get('reward_type') || 'badge'), active: true
      });
      await api('/api/admin/club/config', { method: 'PUT', body: JSON.stringify(config) });
      await loadAdminTab('club'); render(); toast('Достижение создано');
    } else if (form.id === 'club-task-form') {
      const result = await api('/api/admin/club/tasks', { method: 'POST', body: JSON.stringify({
        key: String(data.get('key') || ''), title: String(data.get('title') || ''),
        description: String(data.get('description') || ''), period: String(data.get('period') || 'daily'),
        event_type: String(data.get('event_type') || 'orders'), target: Number(data.get('target')),
        reward: Number(data.get('reward')), active: true
      }) });
      await loadAdminTab('club'); render(); toast('Задание создано: ' + result.key);
    } else if (form.id === 'club-cosmetic-form') {
      const result = await api('/api/admin/club/cosmetics', { method: 'POST', body: JSON.stringify({
        key: String(data.get('key') || ''), title: String(data.get('title') || ''),
        kind: String(data.get('kind') || 'badge'), icon: String(data.get('icon') || '✦'),
        rarity: String(data.get('rarity') || 'common'), description: String(data.get('description') || '')
      }) });
      await loadAdminTab('club'); render(); toast('Косметика создана: ' + result.key);
    } else if (form.id === 'club-balance-form') {
      const result = await api('/api/admin/club/users/' + Number(data.get('user_id')) + '/balance', { method: 'POST', body: JSON.stringify({
        amount: Number(data.get('amount')), reason: String(data.get('reason') || '')
      }) });
      form.reset(); toast('Баланс обновлён: ' + result.balance + ' DCP');
    } else if (form.id === 'club-grant-form') {
      await api('/api/admin/club/users/' + Number(data.get('user_id')) + '/cosmetics/' + encodeURIComponent(String(data.get('cosmetic_key') || '')), { method: 'POST' });
      form.reset(); toast('Косметика выдана пользователю');
    } else if (form.id === 'broadcast-form') {
      if (!confirm('Запустить рассылку всем доступным пользователям?')) return;
      await api('/api/admin/broadcast', { method: 'POST', body: JSON.stringify({ text: String(data.get('text') || '').trim() }) });
      form.reset(); toast('Рассылка поставлена в очередь');
    }
  } catch (error) { toast(error.message || 'Не удалось выполнить действие.'); }
});

async function init() {
  await waitForTelegram(4000);
  captureTelegram();
  setupTelegram();
  if (!initData) {
    document.getElementById('topbar').innerHTML = '<div class="brand"><div class="logo">DSS</div><div><div class="brand-name">DEAD SMOKE STORE</div><div class="brand-sub">Telegram Mini App</div></div></div>';
    document.getElementById('bottomnav').hidden = true;
    content.innerHTML = '<section class="hero"><div class="eyebrow">НУЖЕН TELEGRAM</div><h1>Открой магазин<br>через бота</h1><p>Для входа нужны подтверждённые данные Telegram. Открой чат с ботом и нажми «Открыть магазин».</p></section><div class="card"><b>Почему так?</b><p class="item-sub">Обычный браузер не передаёт подписанные данные пользователя, необходимые для авторизации.</p></div>';
    return;
  }
  try {
    state.user = await api('/api/auth', { method: 'POST' }, false);
    if (state.user.is_banned) { render(); return; }
    await loadBase();
    render();
  } catch (error) {
    content.innerHTML = '<div class="empty"><div class="empty-emoji">⚠️</div><b>Не удалось открыть магазин</b><p>' + esc(error.message) + '</p><button class="primary" onclick="location.reload()">Повторить</button></div>';
  }
}
init();
})();
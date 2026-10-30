(function () {
'use strict';
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const initData = tg && tg.initData ? tg.initData : '';
const content = document.getElementById('content');
const state = {
  user: null, page: 'catalog', categories: [], products: [], cart: {items: [], promos: []},
  profile: null, orders: [], promos: [], top: [], topref: [], search: '', category: 0,
  delivery: 'pickup', reviewOrder: 0, leaderKind: 'buyers', adminTab: 'orders',
  adminProducts: [], adminOrders: [], adminUsers: [], adminPromos: [], adminReviews: [], adminStats: null, adminQuery: ''
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

async function api(path, options, allowCaptchaRetry) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {});
  if (initData) headers['X-Init-Data'] = initData;
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, Object.assign({}, opts, { headers, credentials: 'include' }));
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
        if (sdk && typeof sdk.api.reset === 'function' && captchaWidget !== null) {
          const widgetId = captchaWidget && typeof captchaWidget === 'object' && 'id' in captchaWidget ? captchaWidget.id : captchaWidget;
          sdk.api.reset(widgetId);
        } else {
          mountCaptcha();
        }
      } catch (_e) { mountCaptcha(); }
    };
    fetch('/api/captcha/config').then(r => r.json()).then(config => {
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
  // DDoS-Guard's CAPTCHA integration may expose SPCaptcha rather than reCAPTCHA's grecaptcha.
  const candidates = [
    ['spcaptcha', window.SPCaptcha],
    ['ddgcaptcha', window.DDGCaptcha],
    ['ddg-captcha', window.DDGcaptcha],
    ['dcaptcha', window.dcaptcha],
    ['grecaptcha', window.grecaptcha],
    ['ddcaptcha', window.ddcaptcha]
  ];
  for (const [name, apiObject] of candidates) {
    if (apiObject && typeof apiObject.render === 'function') return { name, api: apiObject };
  }
  return null;
}

function mountCaptcha() {
  const target = document.getElementById('captcha-widget');
  if (!target) return;
  const sdk = getCaptchaSdk();
  if (!sdk) {
    const status = document.getElementById('captcha-status');
    if (status) {
      const found = ['SPCaptcha', 'DDGCaptcha', 'DDGcaptcha', 'dcaptcha', 'ddcaptcha', 'grecaptcha']
        .filter(name => Boolean(window[name]));
      status.textContent = found.length
        ? 'SDK CAPTCHA загружен, но API render недоступен (' + found.join(', ') + '). Проверь версию SDK DDoS-Guard.'
        : 'SDK CAPTCHA загрузился, но API виджета не найден. Проверь подключение SDK DDoS-Guard.';
    }
    return;
  }
  try {
    target.innerHTML = '';
    const onSuccess = function (token) {
      api('/api/captcha/verify', { method: 'POST', body: JSON.stringify({ token }) }, false).then(() => {
        const done = captchaResolve;
        document.getElementById('modal-root').innerHTML = '';
        if (done) done(true);
      }).catch(error => {
        const status = document.getElementById('captcha-status');
        if (status) status.textContent = error.message || 'Проверка не пройдена.';
      });
    };
    const onExpired = function () {
      const status = document.getElementById('captcha-status');
      if (status) status.textContent = 'Проверка истекла. Пройди её ещё раз.';
    };
    const options = sdk.name === 'spcaptcha'
      ? { siteKey: captchaSiteKey, language: 'ru', callback: onSuccess, onExpired }
      : { sitekey: captchaSiteKey, theme: 'dark', callback: onSuccess, 'expired-callback': onExpired };
    captchaWidget = sdk.api.render(target, options);
  } catch (error) {
    const status = document.getElementById('captcha-status');
    if (status) status.textContent = 'Не удалось показать CAPTCHA: ' + error.message;
  }
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
      (tab[0] === 'cart' && state.cart.items && state.cart.items.length ? '<span class="nav-badge">' + state.cart.items.length + '</span>' : '') + '</button>';
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
    profile: renderProfile, promos: renderPromos, leaders: renderLeaders, admin: renderAdmin
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
  grid.innerHTML = products.map(p => '<article class="product-card"><div class="product-art"><span class="product-no">№ ' + esc(p.number) + '</span><span class="stock-dot"></span><span class="product-symbol">' + productSymbol(p) + '</span></div>' +
    '<div class="product-body"><div class="product-brand">' + esc(p.brand) + '</div><div class="product-flavor">' + esc(p.flavor) + '</div><div class="product-foot"><div class="price">' + money(p.price) + '</div><button class="add-button" data-action="add-cart" data-id="' + p.id + '" aria-label="Добавить в корзину">+</button></div></div></article>').join('');
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
  const items = cart.items.map(i => '<div class="item-row"><div class="item-thumb">◈</div><div class="item-info"><div class="item-title">' + esc(i.brand) + '</div><div class="item-sub">' + esc(i.flavor) + '</div></div><div style="text-align:right"><b>' + money(i.price) + '</b><br><button class="mini-link" data-action="remove-cart" data-id="' + i.product_id + '">Убрать</button></div></div>').join('');
  const promos = (cart.promos || []).map(p => '<div class="summary-line"><span class="discount">🎟 ' + esc(p.code) + ' · ' + (p.kind === 'percent' ? esc(p.amount) + '%' : '−' + money(p.amount)) + '</span><span class="muted">' + (p.kind === 'percent' ? 'процентная' : 'фиксированная') + '</span></div>').join('');
  return '<div class="section-head"><div><h1 class="section-title">Корзина</h1><div class="section-sub">' + cart.items.length + ' товар(а)</div></div><button class="mini-link" data-action="clear-cart">Очистить</button></div>' +
    '<div class="card">' + items + '</div><div class="card"><div class="row"><b>Промокод</b><span class="muted">до 2 на заказ</span></div><form id="promo-form"><label class="field-label">Код скидки</label><div class="row"><input class="field" name="code" maxlength="16" required placeholder="Например, DSS2026"><button class="secondary" type="submit">Применить</button></div></form>' +
    promos + '<div class="summary-line"><span>Сумма товаров</span><b>' + money(cart.subtotal) + '</b></div><div class="summary-line"><span>Процентная скидка</span><span class="discount">−' + money((cart.subtotal || 0) * (cart.percent_discount || 0) / 100) + '</span></div><div class="summary-line"><span>Скидка фиксированная</span><span class="discount">−' + money(cart.fixed_discount) + '</span></div><div class="summary-line total"><span>Итого без доставки</span><span>' + money(cart.total) + '</span></div><button class="primary" data-action="go" data-page="checkout" style="margin-top:10px">Оформить заказ →</button></div>';
}
function renderCheckout() {
  const cart = state.cart || { items: [], total: 0 };
  const cdek = state.delivery === 'cdek';
  if (!cart.items || !cart.items.length) return '<div class="empty">Корзина пуста. <button class="mini-link" data-action="go" data-page="catalog">Открыть каталог</button></div>';
  return '<div class="section-head"><div><h1 class="section-title">Оформление</h1><div class="section-sub">Заказ отправится администратору на подтверждение</div></div></div><form id="checkout-form"><div class="card"><b>Способ получения</b>' +
    '<label class="delivery-option ' + (!cdek ? 'selected' : '') + '"><input type="radio" name="delivery" value="pickup" ' + (!cdek ? 'checked' : '') + '><span><span class="option-title">🏠 Самовывоз</span><div class="option-desc">Без дополнительной платы</div></span></label>' +
    '<label class="delivery-option ' + (cdek ? 'selected' : '') + '"><input type="radio" name="delivery" value="cdek" ' + (cdek ? 'checked' : '') + '><span><span class="option-title">🚚 Доставка СДЭК</span><div class="option-desc">Доплата 500 ₽</div></span></label>' +
    (cdek ? '<label class="field-label">ФИО получателя</label><input name="delivery_name" class="field" required minlength="3" maxlength="128" autocomplete="name" placeholder="Иван Иванов"><label class="field-label">Телефон</label><input name="delivery_phone" class="field" required maxlength="32" autocomplete="tel" placeholder="+7…"><label class="field-label">Город, адрес или пункт СДЭК</label><textarea name="delivery_address" class="field" required minlength="5" maxlength="512" placeholder="Город, адрес или код пункта выдачи"></textarea>' : '') +
    '</div><div class="card"><div class="summary-line"><span>Товары</span><b>' + money(cart.total) + '</b></div><div class="summary-line"><span>Доставка</span><b>' + (cdek ? money(500) : 'Бесплатно') + '</b></div><div class="summary-line total"><span>К оплате</span><span>' + money(Number(cart.total) + (cdek ? 500 : 0)) + '</span></div><button class="primary" type="submit" style="margin-top:12px">Подтвердить заказ</button><button class="secondary" type="button" data-action="go" data-page="cart" style="width:100%;margin-top:9px">Вернуться в корзину</button></div></form>';
}
function statusInfo(status) {
  if (status === 'approved') return ['approved', 'Подтверждён'];
  if (status === 'rejected') return ['rejected', 'Отклонён'];
  return ['pending', 'Ожидает'];
}
function renderOrders() {
  if (!state.orders.length) return '<div class="section-head"><div><h1 class="section-title">Мои заказы</h1><div class="section-sub">История покупок</div></div></div><div class="empty"><div class="empty-emoji">◷</div>Заказов пока нет.<p>Оформленный заказ появится здесь.</p><button class="primary" data-action="go" data-page="catalog">В каталог</button></div>';
  return '<div class="section-head"><div><h1 class="section-title">Мои заказы</h1><div class="section-sub">Последние оформленные заказы</div></div><button class="mini-link" data-action="reload-orders">Обновить</button></div>' +
    state.orders.map(o => {
      const st = statusInfo(o.status);
      const items = (o.items || []).map(i => '<div class="summary-line"><span>' + esc(i.brand) + ' — ' + esc(i.flavor) + '</span><span>' + money(i.price) + '</span></div>').join('');
      let review = '';
      if (o.status === 'approved' && !o.has_review) {
        if (state.reviewOrder === o.id) review = '<form id="review-form" data-order-id="' + o.id + '"><label class="field-label">Оценка</label><select class="field" name="rating"><option value="5">5 — Отлично</option><option value="4">4 — Хорошо</option><option value="3">3 — Нормально</option><option value="2">2 — Плохо</option><option value="1">1 — Очень плохо</option></select><label class="field-label">Отзыв</label><textarea class="field" name="text" required minlength="3" maxlength="2048" placeholder="Расскажи о покупке"></textarea><button class="primary" type="submit" style="margin-top:10px">Отправить отзыв</button></form>';
        else review = '<button class="secondary" data-action="write-review" data-id="' + o.id + '" style="width:100%;margin-top:10px">Оставить отзыв</button>';
      } else if (o.has_review) review = '<div class="item-sub">Спасибо за отзыв 💚</div>';
      return '<article class="list-card"><div class="row"><b>Заказ №' + o.id + '</b><span class="status ' + st[0] + '">' + st[1] + '</span></div><div class="item-sub" style="margin:6px 0 11px">' + dateText(o.created_at) + ' · ' + (o.delivery_type === 'cdek' ? 'СДЭК' : 'Самовывоз') + '</div>' + items + '<div class="summary-line total"><span>Итого</span><span>' + money(o.total) + '</span></div>' + review + '</article>';
    }).join('');
}
function renderProfile() {
  const p = state.profile || {};
  const name = p.username || ('ID ' + p.user_id);
  const left = Math.max(0, Number(p.next_bonus_at || 10) - Number(p.referrals_count || 0));
  return '<div class="section-head"><div><h1 class="section-title">Профиль</h1><div class="section-sub">Статистика и бонусы</div></div></div><div class="card"><div class="profile-head"><div class="profile-avatar">' + esc(String(name).replace('@', '').charAt(0).toUpperCase() || 'D') + '</div><div><div class="profile-name">' + esc(name) + '</div><div class="item-sub">Telegram ID: ' + esc(p.user_id) + '</div></div></div><div class="divider"></div><div class="metric-grid"><div class="metric"><div class="metric-value">' + money(p.balance) + '</div><div class="metric-label">Потрачено всего</div></div><div class="metric"><div class="metric-value">' + esc(p.orders_count || 0) + '</div><div class="metric-label">Покупок подтверждено</div></div><div class="metric"><div class="metric-value">' + esc(p.referrals_count || 0) + '</div><div class="metric-label">Купивших рефералов</div></div><div class="metric"><div class="metric-value">' + esc(p.active_promos || 0) + '</div><div class="metric-label">Активных промо</div></div></div></div>' +
    '<div class="card"><b>🎁 Реферальная программа</b><div class="item-sub">15% от первой покупки друга · −10% за каждые 10 покупателей</div><div class="summary-line"><span>До следующего бонуса</span><b>' + left + ' покупок</b></div>' +
    (p.ref_link ? '<div class="row" style="margin-top:12px"><input class="field" id="ref-link" readonly value="' + esc(p.ref_link) + '" style="min-width:0;font-size:11px"><button class="secondary" data-action="copy-ref">Копировать</button></div><button class="secondary" data-action="share-ref" style="width:100%;margin-top:10px">Поделиться ссылкой</button>' : '<div class="item-sub">Ссылка появится после подключения Telegram-бота.</div>') + '</div>' +
    '<div class="section-head"><h2 class="section-title">Быстрый доступ</h2></div><div class="card"><button class="secondary full-button" data-action="go" data-page="orders">📜 История заказов →</button><button class="secondary full-button" data-action="go" data-page="promos">🎟 Мои промокоды →</button><button class="secondary full-button" data-action="leaders" data-kind="buyers">🏆 Топ покупателей →</button><button class="secondary full-button" data-action="leaders" data-kind="refs">👥 Топ реферов →</button>' +
    (p.is_admin ? '<div class="divider"></div><button class="primary" data-action="go" data-page="admin">🛠 Открыть админ-панель</button>' : '') + '</div>';
}
function renderPromos() {
  if (!state.promos.length) return '<div class="section-head"><div><h1 class="section-title">Мои промокоды</h1><div class="section-sub">Доступные скидки</div></div></div><div class="empty"><div class="empty-emoji">🎟</div>Активных промокодов пока нет.<p>Приглашай друзей и возвращайся после покупок.</p></div>';
  return '<div class="section-head"><div><h1 class="section-title">Мои промокоды</h1><div class="section-sub">Можно применить к заказу</div></div></div>' + state.promos.map(p => '<div class="list-card"><div class="row"><div><div class="item-title">' + esc(p.code) + '</div><div class="item-sub">' + (p.kind === 'percent' ? 'Процентная скидка' : 'Скидка в рублях') + '</div></div><div class="price discount">' + (p.kind === 'percent' ? '−' + esc(p.amount) + '%' : '−' + money(p.amount)) + '</div></div><div class="row" style="margin-top:12px"><span class="item-sub">Использовано ' + esc(p.used_count) + ' из ' + esc(p.max_uses) + '</span><button class="secondary" data-action="use-promo" data-code="' + esc(p.code) + '">В корзину</button></div></div>').join('');
}
function renderLeaders() {
  const refs = state.leaderKind === 'refs';
  const list = refs ? state.topref : state.top;
  const title = refs ? 'Топ реферов' : 'Топ покупателей';
  if (!list.length) return '<div class="section-head"><div><h1 class="section-title">' + title + '</h1><div class="section-sub">Рейтинг сообщества</div></div></div><div class="empty">Пока нет данных для рейтинга.</div><button class="secondary full-button" data-action="go" data-page="profile">Назад в профиль</button>';
  return '<div class="section-head"><div><h1 class="section-title">' + title + '</h1><div class="section-sub">Топ-10 участников</div></div></div><div class="card">' + list.map((u, i) => '<div class="item-row"><div class="item-thumb">' + (i + 1) + '</div><div class="item-info"><div class="item-title">' + esc(u.username ? '@' + String(u.username).replace('@', '') : 'Пользователь ' + u.user_id) + '</div><div class="item-sub">ID ' + esc(u.user_id) + '</div></div><b class="discount">' + (refs ? esc(u.value) + ' чел.' : money(u.value)) + '</b></div>').join('') + '</div><button class="secondary full-button" data-action="go" data-page="profile">Назад в профиль</button>';
}
function adminTabs() {
  const tabs = [['orders', 'Заказы'], ['products', 'Товары'], ['users', 'Пользователи'], ['promos', 'Промокоды'], ['reviews', 'Отзывы'], ['stats', 'Статистика'], ['broadcast', 'Рассылка']];
  return '<div class="tabs">' + tabs.map(t => '<button class="tab ' + (state.adminTab === t[0] ? 'active' : '') + '" data-action="admin-tab" data-tab="' + t[0] + '">' + t[1] + '</button>').join('') + '</div>';
}
function renderAdmin() {
  if (!state.user || !state.user.is_admin) return '<div class="empty">Нет доступа к админ-панели.</div>';
  let body = '';
  if (state.adminTab === 'orders') {
    const list = state.adminOrders || [];
    body = '<div class="row" style="margin:12px 0"><b>Последние заказы</b><button class="mini-link" data-action="reload-admin">Обновить</button></div>' + (list.length ? list.map(o => {
      const st = statusInfo(o.status);
      return '<article class="list-card"><div class="row"><b>Заказ №' + o.id + '</b><span class="status ' + st[0] + '">' + st[1] + '</span></div><div class="item-sub" style="margin:6px 0">ID ' + o.user_id + ' · ' + esc(o.username || 'без username') + ' · ' + dateText(o.created_at) + '</div>' +
        (o.items || []).map(i => '<div class="summary-line"><span>' + esc(i.brand) + ' — ' + esc(i.flavor) + '</span><span>' + money(i.price) + '</span></div>').join('') +
        '<div class="summary-line"><span>' + (o.delivery_type === 'cdek' ? 'СДЭК · ' + esc(o.delivery_name || '') + ' · ' + esc(o.delivery_phone || '') + ' · ' + esc(o.delivery_address || '') : 'Самовывоз') + '</span><b>' + money(o.total) + '</b></div>' +
        (o.promo_codes ? '<div class="item-sub">Промокоды: ' + esc(o.promo_codes) + '</div>' : '') +
        (o.status === 'pending' ? '<div class="row" style="margin-top:12px"><button class="primary" data-action="approve-order" data-id="' + o.id + '">Принять</button><button class="danger" data-action="reject-order" data-id="' + o.id + '">Отклонить</button></div>' : '') + '</article>';
    }).join('') : '<div class="empty">Заказов пока нет.</div>');
  } else if (state.adminTab === 'products') {
    body = '<form id="admin-product-form" class="card"><b>Добавить товар</b><label class="field-label">Категория</label><select name="category_id" class="field" required>' + state.categories.map(c => '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('') + '</select><div class="form-row"><div><label class="field-label">Бренд</label><input class="field" name="brand" required maxlength="64"></div><div><label class="field-label">Вкус / вариант</label><input class="field" name="flavor" required maxlength="64"></div></div><label class="field-label">Цена, ₽</label><input class="field" type="number" min="1" step="1" name="price" required><button class="primary" type="submit" style="margin-top:12px">Добавить товар</button></form>' +
      state.adminProducts.map(p => '<article class="admin-product" data-product="' + p.id + '"><div class="row"><b>Товар №' + p.number + '</b><span class="status ' + (p.in_stock ? 'approved' : 'rejected') + '">' + (p.in_stock ? 'В наличии' : 'Скрыт') + '</span></div><div class="admin-product-grid" style="margin-top:10px"><input class="field" name="brand" value="' + esc(p.brand) + '"><input class="field" name="flavor" value="' + esc(p.flavor) + '"><input class="field" name="price" type="number" min="1" value="' + esc(p.price) + '"><select class="field" name="category_id">' + state.categories.map(c => '<option value="' + c.id + '" ' + (c.id === p.category_id ? 'selected' : '') + '>' + esc(c.name) + '</option>').join('') + '</select></div><div class="row" style="margin-top:9px"><button class="small-button" data-action="save-product" data-id="' + p.id + '">Сохранить</button><button class="small-button" data-action="toggle-product" data-id="' + p.id + '">' + (p.in_stock ? 'Скрыть' : 'Вернуть') + '</button><button class="danger" data-action="delete-product" data-id="' + p.id + '">Удалить</button></div></article>').join('');
  } else if (state.adminTab === 'users') {
    body = '<form id="user-search" class="card"><label class="field-label">Поиск по ID, @username или имени</label><div class="row"><input name="q" class="field" value="' + esc(state.adminQuery) + '" placeholder="@username или ID" style="min-width:0"><button class="secondary" type="submit">Найти</button></div></form>' +
      (state.adminUsers || []).map(u => '<article class="list-card"><div class="row"><b>' + esc(u.username ? '@' + String(u.username).replace('@', '') : 'ID ' + u.id) + '</b><span class="status ' + (u.is_banned ? 'rejected' : 'approved') + '">' + (u.is_banned ? 'Заблокирован' : 'Активен') + '</span></div><div class="item-sub" style="margin:5px 0 10px">ID ' + u.id + ' · Покупок ' + u.orders_count + ' · Рефералов ' + u.referrals_count + ' · Потрачено ' + money(u.balance) + '</div><div class="row">' + (u.is_banned ? '<button class="small-button" data-action="unban-user" data-id="' + u.id + '">Разблокировать</button>' : '<button class="danger" data-action="ban-user" data-id="' + u.id + '">Заблокировать</button>') + '<button class="small-button" data-action="reset-user" data-id="' + u.id + '">Сбросить статистику</button></div></article>').join('');
  } else if (state.adminTab === 'promos') {
    body = '<form id="admin-promo-form" class="card"><b>Создать промокод</b><label class="field-label">Код (пусто = сгенерировать)</label><input class="field" name="code" maxlength="16" placeholder="DSS2026"><div class="form-row"><div><label class="field-label">Тип скидки</label><select class="field" name="kind"><option value="fixed">Сумма, ₽</option><option value="percent">Процент, %</option></select></div><div><label class="field-label">Размер</label><input class="field" name="amount" type="number" min="1" step="1" required></div></div><div class="form-row"><div><label class="field-label">Владелец (0 = все)</label><input class="field" name="owner_id" type="number" min="0" value="0" required></div><div><label class="field-label">Активаций</label><input class="field" name="max_uses" type="number" min="1" value="1" required></div></div><button class="primary" type="submit" style="margin-top:12px">Создать промокод</button></form>' +
      (state.adminPromos || []).map(p => '<article class="list-card"><div class="row"><b>' + esc(p.code) + '</b><span class="price">' + (p.kind === 'percent' ? '−' + esc(p.amount) + '%' : '−' + money(p.amount)) + '</span></div><div class="item-sub">Владелец ' + p.owner_id + ' · использований ' + p.used_count + '/' + p.max_uses + ' · ' + esc(p.created_by) + (p.used ? ' · исчерпан' : '') + '</div><button class="danger" data-action="delete-promo" data-code="' + esc(p.code) + '" style="margin-top:10px">Удалить</button></article>').join('');
  } else if (state.adminTab === 'reviews') {
    body = (state.adminReviews || []).length ? state.adminReviews.map(r => '<article class="list-card"><div class="row"><b>Заказ №' + esc(r.order_id) + '</b><span class="discount">' + '★'.repeat(Number(r.rating || 0)) + '☆'.repeat(5 - Number(r.rating || 0)) + '</span></div><div class="item-sub" style="margin:6px 0 10px">Пользователь ID ' + esc(r.user_id) + ' · ' + dateText(r.published_at) + '</div><p>' + esc(r.text || 'Без текста') + '</p></article>').join('') : '<div class="empty">Отзывов пока нет.</div>';
  } else if (state.adminTab === 'stats') {
    const s = state.adminStats;
    body = s ? '<div class="metric-grid"><div class="metric"><div class="metric-value">' + s.total_users + '</div><div class="metric-label">Пользователей</div></div><div class="metric"><div class="metric-value">' + s.total_orders + '</div><div class="metric-label">Подтверждено заказов</div></div><div class="metric"><div class="metric-value">' + s.pending_orders + '</div><div class="metric-label">Ожидают решения</div></div><div class="metric"><div class="metric-value">' + money(s.total_revenue) + '</div><div class="metric-label">Общая выручка</div></div></div><div class="card"><div class="summary-line"><span>За сутки</span><b>' + money(s.revenue_day) + '</b></div><div class="summary-line"><span>За неделю</span><b>' + money(s.revenue_week) + '</b></div><div class="summary-line"><span>Средний чек</span><b>' + money(s.avg_check) + '</b></div><div class="divider"></div><b>Популярные товары</b>' + (s.top_products || []).map((p, i) => '<div class="summary-line"><span>' + (i + 1) + '. ' + esc(p.brand) + ' — ' + esc(p.flavor) + '</span><b>' + p.count + '</b></div>').join('') + '</div>' : '<div class="empty">Загружаем статистику…</div>';
  } else if (state.adminTab === 'broadcast') {
    body = '<div class="card"><b>📢 Рассылка пользователям</b><p class="item-sub">Сообщение будет отправлено пользователям, доступным боту. Не используй рассылку для частых уведомлений.</p><form id="broadcast-form"><label class="field-label">Текст сообщения</label><textarea class="field" name="text" required maxlength="4000" placeholder="Текст рассылки…"></textarea><button class="primary" type="submit" style="margin-top:12px">Запустить рассылку</button></form></div>';
  }
  return '<div class="section-head"><div><h1 class="section-title">Админ-панель</h1><div class="section-sub">Управление магазином</div></div><button class="mini-link" data-action="go" data-page="profile">Закрыть</button></div>' + adminTabs() + '<div id="admin-body">' + body + '</div>';
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
  if (tab === 'promos') state.adminPromos = await api('/api/admin/promos');
  if (tab === 'reviews') state.adminReviews = await api('/api/admin/reviews');
  if (tab === 'stats') state.adminStats = await api('/api/admin/stats');
}
async function loadPage(page) {
  if (page === 'orders') state.orders = await api('/api/orders');
  if (page === 'profile') state.profile = await api('/api/profile');
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
  if (name === 'add-cart') {
    try { state.cart = await api('/api/cart/add', { method: 'POST', body: JSON.stringify({ product_id: id }) }); haptic('light'); render(); toast('Добавлено в корзину'); }
    catch (error) { toast(error.message); } return;
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
  if (name === 'admin-tab') { try { await loadAdminTab(button.dataset.tab); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'reload-admin') { try { await loadAdminTab(state.adminTab); render(); } catch (error) { toast(error.message); } return; }
  if (name === 'save-product') {
    const card = button.closest('.admin-product');
    const payload = { brand: card.querySelector('[name="brand"]').value, flavor: card.querySelector('[name="flavor"]').value, price: Number(card.querySelector('[name="price"]').value), category_id: Number(card.querySelector('[name="category_id"]').value) };
    try { await api('/api/admin/products/' + id, { method: 'PUT', body: JSON.stringify(payload) }); await loadAdminTab('products'); render(); toast('Товар сохранён'); }
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
document.addEventListener('change', event => {
  if (event.target.name === 'delivery') { state.delivery = event.target.value; render(); }
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
      await refreshCart(); await go('orders'); toast('Заказ №' + order.id + ' отправлен на подтверждение');
    } else if (form.id === 'review-form') {
      await api('/api/review', { method: 'POST', body: JSON.stringify({ order_id: Number(form.dataset.orderId), rating: Number(data.get('rating')), text: String(data.get('text') || '').trim() }) });
      state.reviewOrder = 0; state.orders = await api('/api/orders'); render(); toast('Спасибо за отзыв!');
    } else if (form.id === 'admin-product-form') {
      await api('/api/admin/products', { method: 'POST', body: JSON.stringify({ category_id: Number(data.get('category_id')), brand: String(data.get('brand') || '').trim(), flavor: String(data.get('flavor') || '').trim(), price: Number(data.get('price')) }) });
      await loadAdminTab('products'); render(); toast('Товар добавлен');
    } else if (form.id === 'user-search') {
      state.adminQuery = String(data.get('q') || '').trim();
      state.adminUsers = await api('/api/admin/users' + (state.adminQuery ? '?q=' + encodeURIComponent(state.adminQuery) : ''));
      render();
    } else if (form.id === 'admin-promo-form') {
      const result = await api('/api/admin/promos', { method: 'POST', body: JSON.stringify({ code: String(data.get('code') || '').trim(), kind: String(data.get('kind')), amount: Number(data.get('amount')), owner_id: Number(data.get('owner_id')), max_uses: Number(data.get('max_uses')) }) });
      await loadAdminTab('promos'); render(); toast('Промокод создан: ' + result.code);
    } else if (form.id === 'broadcast-form') {
      if (!confirm('Запустить рассылку всем доступным пользователям?')) return;
      await api('/api/admin/broadcast', { method: 'POST', body: JSON.stringify({ text: String(data.get('text') || '').trim() }) });
      form.reset(); toast('Рассылка поставлена в очередь');
    }
  } catch (error) { toast(error.message || 'Не удалось выполнить действие.'); }
});

async function init() {
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
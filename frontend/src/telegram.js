// Обёртка над Telegram WebApp SDK

export const tg = window.Telegram?.WebApp

export function initTelegram() {
  if (tg) {
    tg.ready()
    tg.expand()
    // настройка темы под тёмную
    tg.setHeaderColor('#0a0a0a')
    tg.setBackgroundColor('#0a0a0a')
  }
}

export function getInitData() {
  return tg?.initData || ''
}

export function getTelegramUser() {
  return tg?.initDataUnsafe?.user || null
}

export function haptic(type = 'light') {
  try {
    tg?.HapticFeedback?.impactOccurred(type)
  } catch (e) {}
}

export function showAlert(message) {
  if (tg?.showAlert) {
    tg.showAlert(message)
  } else {
    alert(message)
  }
}

export function showConfirm(message) {
  return new Promise((resolve) => {
    if (tg?.showConfirm) {
      tg.showConfirm(message, (ok) => resolve(ok))
    } else {
      resolve(confirm(message))
    }
  })
}

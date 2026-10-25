import axios from 'axios'
import { getInitData } from './telegram'

// Базовый URL — пустой, потому что API на том же домене
const API_BASE = ''

const client = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
})

// автоматически добавляем initData в заголовки
client.interceptors.request.use((config) => {
  const initData = getInitData()
  if (initData) {
    config.headers['X-Init-Data'] = initData
  }
  return config
})

// обработка ошибок — показываем пользователю
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const msg = error.response?.data?.detail || error.message || 'Ошибка'
    return Promise.reject(new Error(msg))
  }
)

// ============ API методы ============

export const api = {
  // авторизация (проверка initData)
  auth: () => client.post('/api/auth'),

  // каталог
  getCategories: () => client.get('/api/categories'),
  getProducts: (categoryId = null) =>
    client.get('/api/products', { params: categoryId ? { category_id: categoryId } : {} }),

  // корзина
  getCart: () => client.get('/api/cart'),
  addToCart: (productId) => client.post('/api/cart/add', { product_id: productId }),
  removeFromCart: (productId) => client.post('/api/cart/remove', { product_id: productId }),
  clearCart: () => client.post('/api/cart/clear'),

  // промокоды
  applyPromo: (code) => client.post('/api/promo/apply', { code }),
  getAppliedPromos: () => client.get('/api/promo/applied'),

  // оформление
  checkout: (data) => client.post('/api/checkout', data),

  // профиль
  getProfile: () => client.get('/api/profile'),
  getOrders: () => client.get('/api/orders'),
  getMyPromos: () => client.get('/api/promos'),
  getTop: () => client.get('/api/top'),
  getTopRefs: () => client.get('/api/topref'),

  // отзывы
  leaveReview: (data) => client.post('/api/review', data),

  // health (для дебага)
  health: () => client.get('/api/health'),
}

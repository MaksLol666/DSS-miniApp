import { useEffect, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { initTelegram, getTelegramUser } from './telegram'
import { api } from './api'

// Заглушки страниц (пока — для проверки, что работает)
function Placeholder({ title }) {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6 text-center">
      <h1 className="text-3xl font-bold text-dss-accent mb-4">{title}</h1>
      <p className="text-dss-muted">Скоро здесь будет магазин 💨</p>
    </div>
  )
}

export default function App() {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [user, setUser] = useState(null)

  useEffect(() => {
    initTelegram()

    async function checkAuth() {
      try {
        const res = await api.auth()
        setUser(res.data)
      } catch (e) {
        console.error('Auth error:', e)
        setError(e.message)
      } finally {
        setLoading(false)
      }
    }

    checkAuth()
  }, [])

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-dss-accent text-xl animate-pulse">💨 Загрузка…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center p-6 text-center">
        <div className="text-dss-error text-xl mb-4">⚠️ {error}</div>
        <p className="text-dss-muted text-sm">
          Попробуй закрыть и открыть приложение заново
        </p>
      </div>
    )
  }

  return (
    <Routes>
      <Route path="/" element={<Placeholder title="📂 Каталог" />} />
      <Route path="/cart" element={<Placeholder title="🛒 Корзина" />} />
      <Route path="/checkout" element={<Placeholder title="🚚 Оформление" />} />
      <Route path="/profile" element={<Placeholder title="👤 Профиль" />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

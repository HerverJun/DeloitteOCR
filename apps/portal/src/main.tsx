import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

type Health = {
  service: 'ready' | 'unavailable' | 'disabled' | 'unknown'
  ui: string
  cpu: string
  gpu: string
  models: string
}

function Portal() {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/platform/v1/health', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error('服务未就绪')
        return response.json() as Promise<Health>
      })
      .then(setHealth)
      .catch(() => setHealth(null))
    return () => controller.abort()
  }, [])

  return (
    <main>
      <div className="eyebrow">WORKBENCH / LOCAL</div>
      <h1>多功能作业平台</h1>
      <p className="intro">工程基座已建立。应用目录与项目工作空间将在后续阶段接入。</p>
      <div className="status" role="status">
        <span className={health?.service === 'ready' ? 'dot live' : 'dot'} />
        平台服务：{health?.service === 'ready' ? '已连接' : '尚未连接'}
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><Portal /></StrictMode>,
)

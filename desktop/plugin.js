import { host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useRef, useState } from 'react'

function ReceiptPane({ ctx }) {
  const sessionId = useValue(host.state.activeSessionId)
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const current = useRef(sessionId)
  const generation = useRef(0)
  // Invalidate a pending fetch during render, before an async result can commit.
  if (current.current !== sessionId) {
    current.current = sessionId
    generation.current += 1
  }
  useEffect(() => { setData(null); setError('') }, [sessionId])
  const refresh = useCallback(async () => {
    const requested = sessionId
    const ticket = ++generation.current
    setData(null)
    if (!requested) {
      setError('Select an active session first.')
      return
    }
    try {
      const result = await ctx.rest('/current?session_id=' + encodeURIComponent(requested))
      if (ticket !== generation.current || current.current !== requested) return
      if (typeof result.png !== 'string' || !result.png.startsWith('data:image/png;base64,')) throw new Error('Invalid receipt')
      setData({ ...result, sessionId: requested })
      setError('')
    } catch (err) {
      if (ticket !== generation.current || current.current !== requested) return
      setData(null)
      setError('Receipt unavailable for this session.')
    }
  }, [ctx, sessionId])
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4 text-sm',
    children: [
      jsx('button', {
        type: 'button',
        disabled: !sessionId,
        className: 'rounded border px-3 py-2 disabled:opacity-50',
        onClick: refresh,
        children: '🧾 Get the receipt'
      }),
      error ? jsx('p', { role: 'alert', children: error }) : null,
      data && data.sessionId === sessionId ? jsxs('div', { children: [
        jsx('p', { children: data.warning }),
        jsx('img', { src: data.png, alt: 'Public scoped receipt; no explicit task manifest means completion unverified', style: { maxWidth: '100%' } })
      ] }) : jsx('p', { children: 'Select a session and press the button. A task needs an explicit manifest to verify completion.' })
    ]
  })
}

export default {
  id: 'receipts',
  name: 'Receipts',
  register(ctx) {
    // No per-turn transcript button slot in the public SDK; use a dockable pane.
    ctx.register({
      id: 'receipt-pane', area: 'panes', title: 'Receipts',
      data: { placement: 'right', width: '380px' },
      render: () => jsx(ReceiptPane, { ctx })
    })
  }
}

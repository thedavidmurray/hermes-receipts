import { host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useState } from 'react'

function ReceiptPane({ ctx }) {
  const sessionId = useValue(host.state.activeSessionId)
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try {
      const result = await ctx.rest('/last' + (sessionId ? '?session_id=' + encodeURIComponent(sessionId) : ''))
      setData(result)
      setError('')
    } catch (err) {
      setData(null)
      setError(String(err))
    }
  }, [ctx, sessionId])
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4 text-sm',
    children: [
      jsx('button', {
        type: 'button',
        className: 'rounded border px-3 py-2',
        onClick: refresh,
        children: '🧾 Get receipt for current session'
      }),
      error ? jsx('p', { role: 'alert', children: error }) : null,
      data ? jsxs('div', { children: [
        jsx('img', { src: data.png, alt: 'Hermes thermal receipt showing observed tools and verified claims', style: { maxWidth: '100%' } }),
        jsxs('details', { children: [
          jsx('summary', { children: 'Text receipt and full verification hash' }),
          jsx('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, children: data.text })
        ] })
      ] }) : jsx('p', { children: 'Press the button to show a receipt. Past turns are available immediately.' })
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

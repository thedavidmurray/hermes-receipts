import { host, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useRef, useState } from 'react'

// A pickable list: '' means "use the plugin default" and is always offered.
function Choice({ label, name, value, options, onChange, disabled }) {
  const entries = [['', 'default'], ...Object.keys(options || {}).sort().map((id) => [id, id])]
  return jsxs('label', { className: 'flex flex-col gap-1', children: [
    jsx('span', { children: label }),
    jsx('select', {
      'aria-label': name,
      className: 'rounded border px-2 py-1',
      value,
      disabled: Boolean(disabled),
      onChange: (event) => onChange(event.target.value),
      children: entries.map(([id, text]) => jsx('option', { value: id, children: text }, id))
    })
  ] })
}

function ReceiptPane({ ctx }) {
  const sessionId = useValue(host.state.activeSessionId)
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [options, setOptions] = useState(null)
  const [kit, setKit] = useState('')
  const [theme, setTheme] = useState('')
  const current = useRef(sessionId)
  const generation = useRef(0)
  // Invalidate a pending fetch during render, before an async result can commit.
  if (current.current !== sessionId) {
    current.current = sessionId
    generation.current += 1
  }
  useEffect(() => { setData(null); setError('') }, [sessionId])
  useEffect(() => {
    // The registry owns the list; a failed fetch leaves the pane on its defaults.
    let live = true
    ctx.rest('/options').then((result) => { if (live && result && result.kits) setOptions(result) })
      .catch(() => { if (live) setOptions(null) })
    return () => { live = false }
  }, [ctx])
  const refresh = useCallback(async () => {
    const requested = sessionId
    const ticket = ++generation.current
    setData(null)
    if (!requested) {
      setError('Select an active session first.')
      return
    }
    try {
      const result = await ctx.rest('/current?session_id=' + encodeURIComponent(requested)
        + (kit ? '&kit=' + encodeURIComponent(kit) : '')
        + (theme ? '&theme=' + encodeURIComponent(theme) : ''))
      if (ticket !== generation.current || current.current !== requested) return
      if (typeof result.png !== 'string' || !result.png.startsWith('data:image/png;base64,')) throw new Error('Invalid receipt')
      setData({ ...result, sessionId: requested })
      setError('')
    } catch (err) {
      if (ticket !== generation.current || current.current !== requested) return
      setData(null)
      setError('Receipt unavailable for this session.')
    }
  }, [ctx, sessionId, kit, theme])
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4 text-sm',
    children: [
      jsxs('div', { className: 'flex flex-col gap-2', children: [
        Choice({ label: 'What prints', name: 'Receipt kit', value: kit, options: options?.kits, onChange: setKit, disabled: !options }),
        Choice({ label: 'How it looks', name: 'Receipt theme', value: theme, options: options?.themes, onChange: setTheme, disabled: !options })
      ] }),
      jsx('p', { className: 'opacity-70', children: 'The verdict block always prints; a kit only chooses the fields around it.' }),
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
        jsx('p', { children: `kit ${data.kit} · theme ${data.theme}` }),
        jsx('img', { src: data.png, alt: 'Public scoped receipt; no explicit task manifest means completion unverified', style: { maxWidth: '100%' } })
      ] }) : jsx('p', { children: 'Select a session, choose what prints, then press the button. A task needs an explicit manifest to verify completion.' })
    ]
  })
}

export default {
  id: 'receipt-printer',
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

import type { ReactNode } from 'react'

interface Props {
  title: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}

export function CollapsibleSection({ title, open, onToggle, children }: Props) {
  return (
    <div className={`collapsible-section${open ? ' open' : ''}`}>
      <button
        type="button"
        className="collapsible-header"
        onClick={onToggle}
        aria-expanded={open}
      >
        <h2>{title}</h2>
        <span className="collapsible-chevron">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="collapsible-content">{children}</div>}
    </div>
  )
}

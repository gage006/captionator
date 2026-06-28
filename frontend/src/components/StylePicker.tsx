import type { StyleInfo } from '../types'
import { PREVIEW_BG, LARGE_STYLES, captionTextStyle } from './captionStyle'

interface Props {
  styles: StyleInfo[]
  selected: string
  onSelect: (id: string) => void
}

export function StylePicker({ styles, selected, onSelect }: Props) {
  return (
    <div className="style-section">
      <h2>Caption Style</h2>
      <div className="style-grid">
        {styles.map((style) => (
          <button
            key={style.id}
            className={`style-card${selected === style.id ? ' selected' : ''}`}
            onClick={() => onSelect(style.id)}
            type="button"
          >
            <div
              className="style-preview"
              style={{
                ...captionTextStyle(style),
                borderColor: selected === style.id ? style.preview_color : '#333',
                background: PREVIEW_BG[style.id] ?? '#111',
                fontSize: LARGE_STYLES.includes(style.id) ? '22px' : '18px',
              }}
            >
              Aa
            </div>
            <div className="style-info">
              <strong>{style.label}</strong>
              <small>{style.description}</small>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

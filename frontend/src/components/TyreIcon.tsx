import { compoundStyle } from '../lib/teams'

/**
 * The compound as a ring with its letter inside, matching F1's own visual
 * language. Soft red, Medium yellow, Hard white, Intermediate green, Wet blue.
 *
 * An unknown or absent compound renders a hollow grey ring rather than nothing,
 * so the column keeps its width and rows do not jump as stints are announced.
 */
export function TyreIcon({ compound }: { compound: string | null }) {
  const style = compoundStyle(compound)
  const colour = style?.colour ?? '#4A4A57'
  const label = style ? style.label : 'Unknown compound'

  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      role="img"
      aria-label={label}
      className="shrink-0"
    >
      <title>{label}</title>
      <circle cx="9" cy="9" r="7.5" fill="none" stroke={colour} strokeWidth="2.5" />
      {style && (
        <text
          x="9"
          y="9"
          textAnchor="middle"
          dominantBaseline="central"
          fill={colour}
          fontSize="8.5"
          fontWeight="700"
        >
          {style.letter}
        </text>
      )}
    </svg>
  )
}

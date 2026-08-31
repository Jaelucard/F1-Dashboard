/**
 * Fallback team colours for the 2026 grid.
 *
 * `team_colour` from /v1/drivers is the source of truth and is used whenever it
 * is present. This map exists only for the window at the start of a session
 * before the entry list has arrived, or if a team is missing a colour.
 *
 * Eleven teams in 2026: Audi (formerly Sauber) and Cadillac join the grid.
 * Colours are bare hex without "#", matching the API's own format.
 */

export const FALLBACK_TEAM_COLOURS: Record<string, string> = {
  'Red Bull Racing': '4781D7',
  Ferrari: 'ED1131',
  Mercedes: '00D7B6',
  McLaren: 'F47600',
  'Aston Martin': '229971',
  Alpine: '00A1E8',
  Williams: '1868DB',
  'Racing Bulls': '6C98FF',
  Audi: '00E701',
  Haas: '9C9FA2',
  Cadillac: 'B69A5A',
}

/** Anything with no colour anywhere: a neutral grey, never a guessed colour. */
export const UNKNOWN_TEAM_COLOUR = '6E6E7A'

/**
 * Resolve a driver's colour to a CSS value.
 *
 * The API sends bare hex ("4781D7"), so the "#" is added here. A value that
 * already has one is respected, in case that ever changes.
 */
export function teamColour(
  apiColour: string | null | undefined,
  teamName: string | null | undefined,
): string {
  const fromApi = apiColour?.trim()
  if (fromApi) return fromApi.startsWith('#') ? fromApi : `#${fromApi}`

  const fallback = teamName ? FALLBACK_TEAM_COLOURS[teamName] : undefined
  return `#${fallback ?? UNKNOWN_TEAM_COLOUR}`
}

/**
 * Tyre compound presentation. Soft red, Medium yellow, Hard white,
 * Intermediate green, Wet blue. Compounds arrive uppercase ("MEDIUM").
 */
export interface CompoundStyle {
  letter: string
  colour: string
  label: string
}

const COMPOUNDS: Record<string, CompoundStyle> = {
  SOFT: { letter: 'S', colour: '#E10600', label: 'Soft' },
  MEDIUM: { letter: 'M', colour: '#F5D020', label: 'Medium' },
  HARD: { letter: 'H', colour: '#F2F2F5', label: 'Hard' },
  INTERMEDIATE: { letter: 'I', colour: '#2ECC71', label: 'Intermediate' },
  WET: { letter: 'W', colour: '#3B82F6', label: 'Wet' },
}

export function compoundStyle(compound: string | null | undefined): CompoundStyle | null {
  if (!compound) return null
  return COMPOUNDS[compound.trim().toUpperCase()] ?? null
}

import { segmentColour } from '../lib/segments'

/**
 * The mini-sector strip: one small bar per mini-sector of one sector.
 *
 * OpenF1's `segments_sector_1/2/3` are arrays of status codes, one entry per
 * mini-sector, that fill in as the car crosses each one. The length varies by
 * circuit, by sector, and *within* a sector as the lap progresses - Monza FP1
 * runs 6 / 7 / 9, with partial arrays of 1 and 8 while a lap is under way - so
 * nothing here assumes a count: the bars share the cell width equally, whatever
 * there are.
 *
 * The container keeps its height with no bars at all, so a row does not jump
 * when a lap resets and the arrays go empty.
 */
export function MiniSectors({ segments }: { segments: number[] }) {
  return (
    <div
      aria-label="mini-sectors"
      data-testid="mini-sectors"
      title={segments.length > 0 ? segments.join(' ') : undefined}
      className="flex h-[3px] w-full gap-px"
    >
      {segments.map((code, index) => (
        <span
          // Position in the lap is the identity: the codes repeat, and a bar
          // only ever changes colour in place as the sector is revised.
          key={index}
          data-segment={code}
          className="h-full flex-1 rounded-[1px]"
          style={{ backgroundColor: segmentColour(code) }}
        />
      ))}
    </div>
  )
}

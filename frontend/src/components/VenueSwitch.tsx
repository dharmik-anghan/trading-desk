import type { Venue } from "../api";

interface Props {
  venues: readonly Venue[];
  selected: string;
  onSelect: (id: string) => void;
}

/** What to call each desk. */
const TITLE: Record<string, string> = {
  index_options: "Option Desk",
  perpetuals: "Crypto Desk",
};

function deskTitle(venue: Venue | undefined): string {
  if (venue === undefined) return "Option Desk";
  return TITLE[venue.asset_class] ?? venue.name;
}

/**
 * The desk's name, which is also how you change desks.
 *
 * One label rather than a two-position control: the title already says which
 * desk you are on, so a separate switch beside it says it twice. Clicking moves
 * to the next desk, and with two that reads as a toggle.
 *
 * It has to look pressable or it is a trap, so it carries a hover state, a real
 * button's focus ring, and the name of where it goes in its tooltip. With one
 * venue it renders as plain text, because then it goes nowhere.
 */
export function VenueSwitch({ venues: all, selected, onSelect }: Props) {
  // Only venues with a desk to switch to. Crypto options are served to the
  // strategy builder, not as a desk of their own.
  const venues = all.filter((v) => v.asset_class in TITLE);
  const current = venues.find((v) => v.id === selected);
  const label = deskTitle(current);

  if (venues.length < 2) {
    return <div className="brand">{label}</div>;
  }

  const index = venues.findIndex((v) => v.id === selected);
  const next = venues[(Math.max(0, index) + 1) % venues.length];

  return (
    <button
      className="brand deskswitch"
      onClick={() => onSelect(next.id)}
      title={`Switch to ${deskTitle(next)} · ${next.name}, priced in ${next.quote_currency}`}
      aria-label={`${label}. Click to switch to ${deskTitle(next)}`}
    >
      {label}
      {/* Two arrows rather than one: this swaps between desks, it does not go
          forward through them. */}
      <span className="swapmark" aria-hidden="true">
        ⇄
      </span>
    </button>
  );
}

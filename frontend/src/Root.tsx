import { useEffect, useState } from "react";
import App from "./App";
import { Home } from "./components/Home";
import { Backtesting } from "./components/Backtesting";
import { OptionBacktesting } from "./components/OptionBacktesting";
import { Simulator } from "./components/Simulator";
import { PreOpen } from "./components/PreOpen";
import { Rrg } from "./components/Rrg";
import { getVenues } from "./api";
import { useRoute } from "./useRoute";
import { useTheme } from "./hooks/useTheme";

/** Which venue each desk route trades. */
const VENUE_FOR = { options: "fyers", crypto: "shark" } as const;

/**
 * Which page is showing.
 *
 * The desks keep their own state and their own polling, so switching pages
 * unmounts one and mounts the other - which is what stops a hidden desk from
 * spending the rate limit it no longer needs.
 */
export default function Root() {
  const { route, go } = useRoute();
  // Above the router, so every page is the same colour as the last one.
  const { theme, toggle } = useTheme();
  // Only so the home page can say when the crypto desk has no credentials,
  // rather than offering a card that opens onto errors. One request, once.
  const [venueIds, setVenueIds] = useState<string[] | null>(null);
  useEffect(() => {
    void getVenues()
      .then((vs) => setVenueIds(vs.map((v) => v.id)))
      // A failure here should cost the warning, not the page: assume both desks
      // exist and let the desk itself report what is wrong.
      .catch(() => setVenueIds(null));
  }, []);

  if (route === "home") {
    return (
      <Home onGo={go} cryptoReady={venueIds === null || venueIds.includes("shark")} />
    );
  }
  if (route === "backtesting") {
    return <Backtesting onHome={() => go("home")} />;
  }
  if (route === "option-backtesting") {
    return <OptionBacktesting onHome={() => go("home")} />;
  }
  if (route === "option-simulator") {
    return <Simulator onHome={() => go("home")} />;
  }
  if (route === "preopen") {
    return <PreOpen onHome={() => go("home")} />;
  }
  if (route === "rotation") {
    return <Rrg onHome={() => go("home")} />;
  }
  return (
    <App
      venueId={VENUE_FOR[route]}
      // The title toggle still swaps desks; it now moves the address too, so the
      // switch and the back button cannot disagree about where you are.
      onVenue={(id) => go(id === "fyers" ? "options" : "crypto")}
      onHome={() => go("home")}
      theme={theme}
      onTheme={toggle}
    />
  );
}

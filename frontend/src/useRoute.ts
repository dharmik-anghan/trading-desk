import { useEffect, useState } from "react";

export type Route =
  | "home"
  | "options"
  | "crypto"
  | "backtesting"
  | "option-backtesting"
  | "option-simulator"
  | "preopen"
  | "rotation";

/** Paths, and what they mean. One place, so a link and a redirect cannot disagree. */
const PATHS: Record<Route, string> = {
  home: "/",
  options: "/options",
  crypto: "/crypto",
  backtesting: "/backtesting",
  "option-backtesting": "/options/backtesting",
  "option-simulator": "/options/simulator",
  preopen: "/preopen",
  rotation: "/rotation",
};

function routeFor(pathname: string): Route {
  const trimmed = pathname.replace(/\/+$/, "") || "/";
  const found = (Object.entries(PATHS) as [Route, string][]).find(([, p]) => p === trimmed);
  // An unknown path lands on the home page rather than a blank screen. There is
  // nothing useful to say about a typo'd URL that the home page does not say better.
  return found ? found[0] : "home";
}

export function pathFor(route: Route): string {
  return PATHS[route];
}

/** What each page is called in the tab strip and in history. */
const TITLE: Record<Route, string> = {
  home: "Desk",
  options: "Option Desk",
  crypto: "Crypto Desk",
  backtesting: "Backtesting",
  "option-backtesting": "Options backtesting",
  "option-simulator": "Simulator",
  preopen: "Pre-open",
  rotation: "Rotation",
};

/**
 * Which page is showing, from the address bar.
 *
 * Hand-rolled rather than a router library: there are four routes, none of them
 * nested, none with parameters. A router would be a dependency and a set of concepts
 * to learn for something that is twenty lines of History API.
 *
 * The backend serves index.html for unknown paths (`StaticFiles(html=True)`), so a
 * deep link and a refresh on /crypto both work in production as well as in dev.
 */
export function useRoute(): { route: Route; go: (route: Route) => void } {
  const [route, setRoute] = useState<Route>(() => routeFor(window.location.pathname));

  // Which page a tab is on, said in the tab strip. Without this every page is
  // called "Option Desk", including the two that are not.
  useEffect(() => {
    document.title = TITLE[route];
  }, [route]);

  useEffect(() => {
    // The back button. Without this, leaving a desk would change the address and
    // not the screen, which is worse than having no routes at all.
    const onPop = () => setRoute(routeFor(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const go = (next: Route) => {
    if (next === route) return;
    window.history.pushState(null, "", pathFor(next));
    setRoute(next);
  };

  return { route, go };
}

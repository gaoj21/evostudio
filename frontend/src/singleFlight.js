// A poll that skips its turn while the previous request is still out. On a
// fixed interval a slow server otherwise gets a new request every tick on top
// of the ones it has not answered yet, and each one makes it slower still.
export function singleFlight(task) {
  let inFlight = false;
  return async (...args) => {
    if (inFlight) return undefined;
    inFlight = true;
    try {
      return await task(...args);
    } finally {
      inFlight = false;
    }
  };
}

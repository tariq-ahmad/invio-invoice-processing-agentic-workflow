import { create } from "zustand";
import { toast } from "./useToasts";

const KEY = "alerts:notify";

function load(): boolean {
  try {
    return localStorage.getItem(KEY) === "1" && typeof Notification !== "undefined" && Notification.permission === "granted";
  } catch {
    return false;
  }
}

interface AlertPrefs {
  /** Show a desktop notification when approval is needed and the tab is hidden. */
  notify: boolean;
  toggleNotify: () => Promise<void>;
}

export const useAlertPrefs = create<AlertPrefs>((set, get) => ({
  notify: load(),
  async toggleNotify() {
    if (get().notify) {
      set({ notify: false });
      try {
        localStorage.setItem(KEY, "0");
      } catch {
        /* ignore */
      }
      toast("Desktop notifications off");
      return;
    }
    if (typeof Notification === "undefined") {
      toast("This browser doesn't support desktop notifications.", "error");
      return;
    }
    // Permission must be requested from a user gesture — this runs from a click.
    const perm = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    if (perm !== "granted") {
      toast(
        perm === "denied"
          ? "Notifications are blocked for this site. Allow them in your browser's site settings."
          : "Notification permission wasn't granted.",
        "error",
      );
      return;
    }
    set({ notify: true });
    try {
      localStorage.setItem(KEY, "1");
    } catch {
      /* ignore */
    }
    toast("You'll be notified when an approval needs you", "success");
  },
}));

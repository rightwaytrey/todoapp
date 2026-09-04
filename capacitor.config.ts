import type { CapacitorConfig } from '@capacitor/cli'

/**
 * Bundled mode: www/index.html is the whole UI and ships inside the app, so
 * there is no build step and nothing to fetch before the first paint. The
 * cached task list renders instantly and a task can be captured with the
 * server unreachable; the queued write goes out when the phone is back on the
 * tailnet (design.md D6).
 *
 * The only network traffic is JSON to the TaskMaster API on the home server,
 * reachable only over the Tailscale tailnet and overridable on the Settings
 * screen (design.md D4). The plain http is deliberate — WireGuard already
 * encrypts that path — and is why ios/App/App/Info.plist carries a scoped ATS
 * exception for that one domain.
 *
 * The host itself is NOT in this repo. The placeholder in `updateUrl` below, and
 * the one in each of the two Info.plists, are stamped at build time from the
 * TASKMASTER_API_BASE and TASKMASTER_ATS_DOMAIN repository secrets
 * (.github/workflows/testflight.yml, "Stamp server configuration"). README.md,
 * "Configuration", is the map of which placeholder is in which file.
 *
 * No push. Nothing here needs to reach a closed app: `pa remind` already sends
 * the due-date reminders through Pushover, which is what orderconfirm-ios and
 * carpool need APNs for and this app does not.
 *
 * Two plugins, both added 2026-09-04 for D11 (over-the-air web updates):
 * `@capgo/capacitor-updater`, configured below, and `@capacitor/app`, which the
 * client uses for appUrlOpen (the widget's deep link) and getInfo (the real
 * native version/build, which is what the update check is gated on). Neither
 * adds an entitlement, so CI still archives unsigned (Info.plist, bottom).
 */
const config: CapacitorConfig = {
  appId: 'org.rightwaytrey.taskmaster',
  appName: 'TaskMaster',
  webDir: 'www',
  ios: {
    contentInset: 'automatic',
    // Matches user-scalable=no in www/index.html. The two must agree: setting
    // this without the meta tag strands the webview at whatever scale a pinch
    // reached, and the meta tag without this gets you nothing.
    zoomEnabled: false
  },
  plugins: {
    /**
     * Live web-bundle updates (@capgo/capacitor-updater 8.51.15), self-hosted
     * on the home server — design.md D11.
     *
     * The whole client is one committed www/index.html. Every change to it used
     * to cost a TestFlight dispatch: ~30 billed macOS minutes for a file the
     * phone could have fetched in a second. With this the phone asks our own
     * API at launch whether a newer www/ exists, downloads the zip, checks its
     * SHA-256 and swaps it in. A store build is then only for what actually
     * changes natively — a plugin, Info.plist, the widget, or this very block.
     *
     * Apple allows it: the client runs in WKWebView, the engine named in the
     * 3.3.1(B)/3.3.2 carve-out for interpreted code. The rule to stay inside is
     * 2.3.1 — a bundle may fix and refine what review already saw, not add
     * functionality it never did.
     *
     * EVERY SETTING HERE IS NATIVE. None of it can be changed over the air;
     * changing any line below needs a store build to take effect on a phone.
     */
    CapacitorUpdater: {
      // 'onLaunch' is a DIRECT-update mode in 8.51.15, not a "check later" one:
      // directUpdateModeForAutoUpdateMode maps it straight through
      // (CapacitorUpdaterPlugin.swift:3979) and shouldUseDirectUpdate() returns
      // true for the FIRST check of a process (:3859), so a cold launch
      // downloads the bundle AND reloads onto it inside that same launch. That
      // is what "phones pick it up at next launch" means here.
      //
      // A check made LATER in the same process — the app is foregrounded and a
      // bundle was published while it ran — only queues the bundle, and the
      // plugin applies the queue at the next background or launch. Good: this
      // app is used in five-second bursts (complete a task, swipe home), and a
      // reload while a task sheet is open would throw away what was typed.
      //
      // The deprecated `directUpdate` key is deliberately NOT set — it is the
      // old spelling of this same thing and setting both invites drift.
      autoUpdate: 'onLaunch',
      // Our own server, on the tailnet, plain http (design.md D4 — WireGuard
      // already encrypts the path). The ATS exception in ios/App/App/Info.plist
      // is scoped to the same tailnet domain with NSIncludesSubdomains, so it
      // covers this host; ATS applies per bundle to every URLSession in the app,
      // this plugin's included, so nothing extra is needed for it.
      //
      // The placeholder below is stamped by CI from the TASKMASTER_API_BASE
      // secret (the file header). An unstamped checkout leaves the literal here,
      // which is a URL the plugin cannot resolve — it logs one failed check per
      // launch and changes nothing. That is the right failure for a tree nobody
      // has configured, and the workflow refuses to build one.
      //
      // Note the path: /api/app/update, NOT the /api/app-update transitnav uses
      // — this server namespaces the shell's endpoints under /api/app/.
      updateUrl: '__API_BASE__/api/app/update',
      // Both default to plugin.capgo.app; empty string disables them
      // (CapgoUpdater.swift:657 and :2819 guard on isEmpty). This install is
      // self-hosted and single-user: there is no Capgo account, one channel,
      // and no reason for a phone on a private tailnet to post telemetry to a
      // third party. Leaving these at their defaults would do exactly that.
      statsUrl: '',
      channelUrl: '',
      // The rollback handshake. www/index.html calls notifyAppReady() once it
      // has painted; a bundle that does not reach that call within this many ms
      // is reverted to the previous one at the next launch. The client renders
      // synchronously from its localStorage cache before any network call
      // (design.md D6), so the plugin's 10 s default is already generous — a
      // bundle that cannot paint in ten seconds on a phone is broken, and the
      // sooner it rolls back the better. Stated rather than left implicit so a
      // plugin upgrade cannot move it under us.
      appReadyTimeout: 10000,
      // A store build is a fresh start: drop any OTA bundle so the binary Apple
      // reviewed is what runs, and let the next check re-offer from there. This
      // is also what makes min_native safe — after a native upgrade the phone
      // is briefly back on the built-in bundle and asks again with its new
      // build number.
      resetWhenUpdate: true,
      // Housekeeping, both the plugin's defaults, stated for the same reason:
      // a bundle that failed is never retried, and only the current and
      // previous bundles are kept on the phone.
      autoDeleteFailed: true,
      autoDeletePrevious: true
    }
  }
}

export default config

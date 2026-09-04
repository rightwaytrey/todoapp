# Shipping TaskMaster

The Apple side, once, then every build is one command. ~15 minutes of clicking.
Four of the six secrets already exist for `orderconfirm-ios` and `carpool` —
the same Apple team and the same App Store Connect API key, so those values are
copy-paste, not new. The other two are this repo's own, and are the reason no
host name appears anywhere in the tree.

> **Status 2026-09-02:** steps 1 and 2 are DONE (driven through the developer
> portal and App Store Connect from this box). App ID `org.rightwaytrey.taskmaster`
> is registered with no capabilities; the App Store Connect record is named
> **"TaskMaster (rwt)"** because plain "TaskMaster" is taken store-wide, SKU
> `taskmaster`, Full Access. The private repo `rightwaytrey/todoapp` exists,
> `main` is pushed, the Apple secrets are set (2026-09-03), and **build 1.0.1
> (run 1) uploaded to TestFlight on 2026-09-03** — the "Program License
> Agreement updated" banner did not block it. Step 4 is done too: internal group
> **Internal** (automatic distribution) with the developer's own Apple ID as the
> tester, added 2026-09-03 after the user signed the desktop browser back in;
> re-added 2026-09-04 to force the invite (see the table below).
>
> 
## One time

1. **App IDs — there are two.** developer.apple.com → Certificates, IDs &
   Profiles → **Identifiers → + → App IDs → App**, once each for:

   | Bundle id | What |
   |---|---|
   | `org.rightwaytrey.taskmaster` | the app |
   | `org.rightwaytrey.taskmaster.widget` | the home-screen widget extension (design.md D7) |

   Tick **no capabilities at all** on both — nothing here has an entitlement,
   which is what lets CI archive unsigned (see the comment in the workflow).
   The widget does **not** need an App Group; that is exactly what D7 avoided.

   Only the app gets an App Store Connect record in step 2. An extension never
   does — it ships inside the app — but it still needs its own App ID, because
   `-exportArchive` signs every nested bundle separately and has to find a
   profile for each one.

2. **App record.** App Store Connect → **Apps → + → New App**: platform iOS,
   bundle id `org.rightwaytrey.taskmaster` picked from the dropdown (it appears
   only after step 1), SKU `taskmaster`.
   The **Name** must be unique store-wide, so if plain `TaskMaster` is taken use
   **`TaskMaster (rwt)`** — that name is only ever seen in App Store Connect and
   TestFlight. The name on the phone's home screen comes from
   `CFBundleDisplayName` in `ios/App/App/Info.plist` and stays **TaskMaster**
   either way. You never submit for review; TestFlight internal only.

3. **GitHub.** Create the repo `rightwaytrey/todoapp` and push `main`. Then
   Settings → Secrets and variables → Actions → New repository secret, **six**
   of them.

   The four Apple ones, same values as `orderconfirm-ios`/`carpool`:
   `APPLE_TEAM_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8_BASE64`.
   The last one is the `.p8` file encoded flat: `base64 -w0 AuthKey_XXXXXXXXXX.p8`.

   And two that are only this app's, because the repo is public and the server
   is not (design.md D4). The workflow's *Stamp server configuration* step seds
   them into `www/index.html`, `capacitor.config.ts` and both `Info.plist`s
   before `cap sync`, and **fails the build** if either is empty:

   | Secret | Holds |
   |---|---|
   | `TASKMASTER_API_BASE` | the API base URL the phone uses — scheme, host and port, no trailing slash. Replaces `__API_BASE__`. |
   | `TASKMASTER_ATS_DOMAIN` | the ATS exception domain — the bare domain the two plists scope their cleartext exception to. Replaces `__ATS_DOMAIN__`. |

   `TASKMASTER_API_BASE` must be the **same value** as `TASKMASTER_PUBLIC_BASE`
   in `~/.config/taskmaster/env` on the server (README.md, "Configuration"), or
   an over-the-air bundle will hand phones a default server the shell was not
   built for. Neither value belongs in a commit, a log line or an issue.

4. **Internal tester.** App Store Connect → TaskMaster → **TestFlight →
   Internal Testing** → add your own Apple ID. Install the TestFlight app on the
   phone from the App Store if it is not there already.

## Web changes — no build at all

Most changes to this app are to one HTML file, and since design.md D11 those do
not go through here. Nothing below this heading applies to them:

```bash
scripts/ship_web.sh "what changed"
```

That publishes `www/` as a bundle into `server/bundles/`, then proves it through
the same URL the phone will use — asks for an update the way the plugin does,
downloads what is offered, checks the SHA-256 and that `index.html` is at the
zip root. Phones pick it up at their **next cold launch**: no store review, no
CI minutes, nothing to install.

**One prerequisite, and it is this file's whole point: a store build carrying
`@capgo/capacitor-updater` must be on the phone first.** Build 1.0.1 (2026-09-03)
predates the plugin, so until the next dispatch every bundle published is inert —
the installed shell is not asking anyone anything. After that build, `Settings`
shows a **Bundle** line next to **Build**; `Build` is the native `1.0.<run>`, and
`Bundle` is the over-the-air version (`2026.0904.1`) or the build number again
when the phone is still on the shipped client.

**A bundle must not need a native change the installed shell lacks.** Gate it:

```bash
MIN_NATIVE=1.0.9 scripts/ship_web.sh "uses the new bridge"
```

Older shells are then told `"needs native 1.0.9"` and are left alone, instead of
being handed a client that calls a bridge that isn't there. The number is the
**Build** line. It sticks in the manifest until it is changed.

What is published, and who has asked:

```bash
curl -s "$TASKMASTER_PUBLIC_BASE/api/app/update" | python3 -m json.tool
journalctl --user -u taskmaster-api -f | grep app-update
```

(`TASKMASTER_PUBLIC_BASE` is in `~/.config/taskmaster/env` — not in this repo.
`set -a; . ~/.config/taskmaster/env; set +a` puts it in the shell.)

`server/bundles/` is deliberately **empty** right now. An empty store answers
every phone "no bundle published", which is the correct answer until there is a
shell that could use one.

## Every build

Everything from here down costs ~30 billed macOS minutes, so **dispatch only
with the user's explicit go**, and only when something *native* changed: a
plugin, `capacitor.config.ts`, `Info.plist`, an icon, the widget. There is no
`push:` trigger (macOS minutes bill at 10x; see the workflow header):

```bash
echo '{"ref":"main"}' | gh api --method POST \
  repos/rightwaytrey/todoapp/actions/workflows/testflight.yml/dispatches --input -

gh run watch          # ~15 min of build, then upload
```

Then wait **~10 minutes** for Apple to finish processing before the build shows
up, and install it from TestFlight on the phone.

First launch: **Settings** → the server URL should already be filled in with
the value of the `TASKMASTER_API_BASE` secret. Tap **Test connection**; it should
come back with the task count and the Taskwarrior version. If it instead says
**Not configured**, the stamp step did not run on the build that is installed. The **Build** line at the
bottom of that screen is `1.0.<run number>` and is how you tell which build a
phone is actually running.

This requires the phone to be **on the tailnet** — the Tailscale app installed
and connected. There is no path to the API from the open internet, by design
(design.md D4).

## When it goes wrong

| Symptom | Cause |
|---|---|
| Tester row says **No Builds Available** and no invite mail comes, although the group's build says Testing | Seen 2026-09-03 when the tester was added to a group whose build had already been auto-attached. Select the tester → **Remove** → **+** → add again. The row flips to *Invited* and the "invited you to test" mail arrives within seconds. |
| App says **offline**, Test connection fails | The phone is not on the tailnet — open the Tailscale app and connect. Then check the unit is up: `systemctl --user status taskmaster-api` on the server. |
| Offline with an **IP address** in the server URL | ATS exception domains are domain names; an IP literal is silently ignored and the load is refused. Use the server's MagicDNS name — the `TASKMASTER_API_BASE` secret. |
| `altool`: **"No suitable application records were found"** | Step 2 was never done, or the bundle id in the record does not match `org.rightwaytrey.taskmaster`. |
| Export fails on signing / provisioning | One of the four Apple secrets in step 3 is missing or stale. `APPLE_TEAM_ID` is the 10-character one from App Store Connect → Membership. |
| Build fails at **Stamp server configuration** | `TASKMASTER_API_BASE` or `TASKMASTER_ATS_DOMAIN` is empty (step 3), or a placeholder was removed from the file that carries it. The step names which. It fails on purpose: a silent no-op sed ships an app with no server. |
| Settings shows **Not configured**, or the widget says **Server not configured** | The installed build was made without the stamp step — anything before it existed, or a local Xcode build. Dispatch a fresh one. Entering the URL by hand in Settings also works for the app, but not for the widget, which reads its own `Info.plist`. |
| Export fails with **"no profiles for `org.rightwaytrey.taskmaster.widget`"** (or "failed to create provisioning profile" naming it) | The widget's App ID was never registered — step 1 has **two** rows and this is the second. Register it with no capabilities and re-dispatch; nothing in the repo needs changing. This is the one new way the build can fail since the widget was added (design.md D7). |
| Build uploads, but the widget is **not in the widget gallery** on the phone | Long-press the home screen → **+** and search "TaskMaster"; a newly installed extension can take a minute to be indexed. If it never appears the `.appex` was not embedded — check the "Embed Foundation Extensions" phase survived, with `ruby scripts/add_widget_target.rb` (it prints "already present" when all is well). |
| Widget says **"Can't reach the server"** while the app itself works | The extension has its own ATS exception, in `ios/App/TaskMasterWidget/Info.plist`, separate from the app's — ATS is evaluated per bundle. If that block was edited or dropped, this is what it looks like. Otherwise it is the ordinary case: the phone is off the tailnet. |
| Build installs but Settings shows the literal `__BUILD__` | The stamp step ran after `cap sync`, or `www/index.html` lost the placeholder. CI fails hard on the second case. |
| The app stops launching for no reason ~3 months in | TestFlight builds expire after 90 days. Re-dispatch. |
| A bundle was published but the phone never picks it up | In order: is the installed build newer than the one that first carried the plugin (`Settings → Build`)? Is the phone on the tailnet? Did the app get a **cold** launch — `autoUpdate: 'onLaunch'` applies on a launch from killed, not on a resume? Then ask the server what it is telling that phone: `journalctl --user -u taskmaster-api \| grep app-update`. `needs native …` means `min_native` is withholding it; `no bundle published` means `server/bundles/` is not the directory that was published to. |
| Settings shows the literal `__BUNDLE__` after an update | The bundle was zipped without stamping. `scripts/publish_bundle.py` refuses to publish when the placeholder is missing, so this means something else built the zip. |
| Upload succeeds but App Store Connect emails **ITMS-91053: Missing API declaration** (`NSPrivacyAccessedAPICategoryUserDefaults`) | New since the updater plugin: it keeps its bundle bookkeeping in `UserDefaults`, and its README asks for a privacy manifest declaring reason `CA92.1`. This repo has no `PrivacyInfo.xcprivacy` — the widget already used `UserDefaults` and build 1.0.1 uploaded without one, so today it is an informational mail, not a rejection. Fixing it means adding `ios/App/App/PrivacyInfo.xcprivacy` **and** wiring it in as a resource of the App target in `project.pbxproj` (`scripts/add_widget_target.rb` is the only way to edit that from Linux). Not done, deliberately: it is a pbxproj change and it is not blocking anything yet. |
| The phone updates, then goes back to the old client at the next launch | The bundle never reached `notifyAppReady()` within `appReadyTimeout`, so the plugin rolled it back — the client threw during startup. Reproduce it in a browser (`npm run serve`) before publishing again; the publish script only checks that the *call* is present, not that it is reached. |

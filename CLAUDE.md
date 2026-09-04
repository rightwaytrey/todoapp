# CLAUDE.md — TaskMaster

A native iOS front-end for the Taskwarrior tasks on the home server, replacing
Taskchamp (pull-only sync, broken filters). Taskwarrior stays the store; the
phone talks to a small HTTP API over the tailnet. Everything downstream of
`task` — `pa` roundup/digest/remind, the `+claude` queue, the Scriptable widget
— keeps working untouched, so do not propose moving the data anywhere.

## Where the decisions live

- `docs/design.md` — the decisions (D1–D6) and why. Read it before proposing
  an architecture change; most of them have a "ruled out" clause already.
- `docs/api.md` — the contract between `www/index.html` and `server/`. Both
  sides are built to it. **Change this file first**, and say so, before
  changing either side to match.

## Conventions

- **Capacitor 8, bundled.** `www/index.html` is the whole client: one file,
  vanilla JS, no framework, no build step, no external requests. `cap sync ios`
  copies it into the Xcode project.
- **Server** is FastAPI shelling out to the `task` CLI (argv only, never a
  shell string), running as the systemd **user** unit `taskmaster-api` on
  `:8101`. `systemctl --user`, never `sudo`.
- **"The app" means the phone** — the native app installed through TestFlight.
  A browser tab pointed at `www/index.html` is a development convenience, not
  the app. Verify claims against what is actually shipped, and say which one
  you checked.
- **Never run tests against the real `~/.task`.** The server tests create a
  temp dir and set `TASKRC`/`TASKDATA` with `hooks=off`. A test that touches
  the real database also fires the real hooks and pushes to the real phone.

## Working here

- **This repo is public.** No host names, tailnet addresses, device names or
  DDNS names in tracked files — **screenshots included**: a text grep cannot
  see pixels, and a Settings screenshot taken from a stamped copy once leaked
  the host. Render screenshots from the unstamped client (it shows "Not
  configured") and look at each one before it is staged. The app's server URL and the ATS exception domain
  are `__API_BASE__` / `__ATS_DOMAIN__` placeholders, stamped by CI from the
  `TASKMASTER_API_BASE` / `TASKMASTER_ATS_DOMAIN` repository secrets and, for
  bundles and the server, from `TASKMASTER_PUBLIC_BASE` in
  `~/.config/taskmaster/env` (outside the repo, mode 600). design.md D4.
- **Ask before committing**: show the proposed message and the file list, and
  wait. Nothing is pushed without being asked either.
- **TestFlight is dispatch-only** (`.github/workflows/testflight.yml`). macOS
  runners bill at 10x and August ran out of minutes on redundant builds. Do
  not dispatch for a change that does not touch `www/` or the shell — a
  server-only change ships with a `systemctl --user restart`.
- The **TransitNav backlog protocol** in `~/.claude/CLAUDE.md` does **not**
  apply to this project. It is scoped to the TransitNav repos and the vault;
  this is an unrelated app with no shared backlog file.

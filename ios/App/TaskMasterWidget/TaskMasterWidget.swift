//
//  TaskMasterWidget.swift
//  The home-screen widget: overdue + due-today, straight from the API, with a
//  tap-to-complete check box on every row.
//
//  This is the native replacement for scriptable-today-widget.js in
//  ~/projects/dashboards, and it deliberately shows the same thing: a "Today"
//  header with a count, overdue rows first in red, then today's, "+N more"
//  when the list is longer than the family fits, "Nothing due" when it is
//  empty, and a small "updated Xm ago" footer so a stale render is obvious.
//
//  It fetches the API ITSELF rather than reading anything the app wrote
//  (design.md D7). The app and this extension are separate bundles with
//  separate containers, and the only way to share a container is an App Group
//  — which is an entitlement, and this app has no entitlements on purpose,
//  which is exactly what lets CI archive UNSIGNED (see the comments in
//  ios/App/App/Info.plist and .github/workflows/testflight.yml). So v1 does its
//  own http and caches into its own UserDefaults. An App Group cache and a
//  server-URL override are future work, and would cost a signed archive.
//
//  Where it gets the server: its OWN Info.plist, key TaskMasterAPIBase, read at
//  runtime by serverBase(). Reading the plist needs no entitlement; reading the
//  app's Settings screen would need the App Group above. Unstamped, the widget
//  says "Server not configured" and never fetches.
//
//  COMPLETING (design.md D10). The box at the head of each row is an
//  interactive widget toggle backed by CompleteTaskIntent: the first tap turns
//  it into a green check IMMEDIATELY — SwiftUI flips an intent-backed Toggle on
//  touch, before the intent has even run — and only arms the row, and three
//  seconds later the intent POSTs /api/tasks/<uuid>/done, the same call the app
//  makes. A second tap inside those three seconds unticks the box and disarms
//  the row, nothing is sent at all, and a mis-tap therefore costs one more tap
//  instead of an undo.
//
//  The armed uuids live in this extension's own UserDefaults (kArmedKey below):
//  the intent re-reads them after its sleep to decide whether to send, and the
//  timeline reads them so a redraw mid-grace keeps the check mark on. Nothing
//  else is written locally and nothing is read back — WidgetKit reloads this
//  widget's timeline when the intent returns, and the next fetch is the account
//  of record. A tap anywhere ELSE still opens the app (.widgetURL); the two
//  coexist.
//
//  Deployment target is iOS 17.0 — THIS TARGET ONLY. The app stays at 15.0, and
//  scripts/add_widget_target.rb is what sets the floor, so re-run it after any
//  `npx cap sync ios`. The floor moved for the interactive row — it was
//  Button(intent:) and invalidatableContent, and it is now Toggle(isOn:intent:)
//  (iOS 17) over a SetValueIntent (iOS 16) — and an extension is allowed a
//  higher floor than its host app: the price is that the widget is absent from
//  the gallery on anything older, and this phone is well past 17. Nothing here
//  is behind an #available check any more, which is why
//  WidgetContainerBackground at the bottom is now one line.
//

import WidgetKit
import SwiftUI
import Foundation
import AppIntents

// MARK: - Where the tasks come from

/// The Info.plist key this extension reads its API base out of. Its value is a
/// placeholder in the committed plist and is stamped at build time from the
/// TASKMASTER_API_BASE repository secret, the same value that goes into
/// `API_DEFAULT` in `www/index.html` and `updateUrl` in `capacitor.config.ts`
/// (.github/workflows/testflight.yml, "Stamp server configuration"). The host
/// is not in this repo.
private let kServerBaseKey = "TaskMasterAPIBase"

/// The API base, or nil when this build was never stamped.
///
/// `Bundle.main` inside an app extension is the EXTENSION's bundle, which is
/// what we want: this reads TaskMasterWidget/Info.plist, not the app's, and
/// needs no entitlement — unlike an App Group, which is what would be required
/// to read the user's Settings URL instead (design.md D7, still future work).
///
/// Two ways to have no server, treated the same: the key is absent, or it still
/// holds the literal placeholder. Both mean nothing was configured, and the
/// widget says so rather than fetching a URL that cannot resolve.
///
/// Plain http over the tailnet is design.md D4, and ATS is evaluated per
/// bundle: the app's exception does not cover this extension, so
/// TaskMasterWidget/Info.plist carries its own copy of the scoped exception.
/// Without it the fetch fails with no useful log.
private func serverBase() -> String? {
    guard let raw = Bundle.main.object(forInfoDictionaryKey: kServerBaseKey) as? String
    else { return nil }
    let base = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if base.isEmpty || base.hasPrefix("__") { return nil }
    return base
}

/// `status=pending` is the default (api.md), but say it out loud — the widget
/// only ever wants the pending list, and a future change of default should not
/// silently change what the home screen shows.
private let kTasksPath = "/api/tasks?status=pending"

/// 10 s, the same budget the Scriptable widget used. WidgetKit kills a slow
/// refresh anyway; failing fast leaves time to paint the cache instead.
private let kTimeout: TimeInterval = 10

/// How far out to ask for the next refresh. Advisory — iOS throttles by
/// budget, exactly as the Scriptable widget's refreshAfterDate was.
private let kRefreshInterval: TimeInterval = 15 * 60

/// The extension's OWN UserDefaults (not an App Group — see the file header).
/// Holds the last response body that parsed, so a phone off the tailnet shows
/// the last known list instead of an error.
private let kCacheBodyKey = "tm_last_body"
private let kCacheDateKey = "tm_last_fetched"

/// The rows that have been ticked but whose POST has not gone out yet: uuid ->
/// the instant it was armed, as a UNIX timestamp, the whole lot under one key
/// as a [String: Double]. A dictionary of Doubles because that is a plist type
/// and round-trips through UserDefaults exactly — which matters, because the
/// timestamp is compared for EQUALITY below to tell one arming from the next.
///
/// Written by the intent, read by the intent AND by the timeline. That works
/// because an interactive widget's intent is performed in this extension's own
/// process — the same Bundle.main serverBase() reads, and the same defaults
/// container as the cache above. No App Group, so still no entitlement (D7).
private let kArmedKey = "tm_armed"

/// How long a tick sits there before it is sent: long enough to notice the
/// wrong row and tap it again, short enough that nobody wonders whether it
/// worked. Nanoseconds because that is what Task.sleep takes.
private let kGraceNanoseconds: UInt64 = 3_000_000_000

/// Anything armed longer ago than this is not waiting for anything: its intent
/// was killed mid-sleep (an extension is never promised its process), and the
/// check mark would otherwise sit on that row for good. Swept in getTimeline.
private let kArmedMaxAge: TimeInterval = 60

// MARK: - The API's Task, cut down to what a row needs

/// Only the fields the widget draws, plus `priority`, which it never draws and
/// only sorts on (api.md, "Canonical order"). Codable ignores everything else in
/// the object (api.md ships ~15 keys), so the server can grow without touching
/// this. `description` is renamed on the way in: a stored property called
/// `description` shadows CustomStringConvertible and reads badly at every use.
///
/// `priority` is Optional, so the synthesised decoder uses decodeIfPresent: a
/// task with no priority — which is most of them — decodes to nil rather than
/// failing the whole array.
private struct APITask: Decodable {
    let uuid: String
    let text: String
    let due: String?
    let priority: String?

    enum CodingKeys: String, CodingKey {
        case uuid
        case text = "description"
        case due
        case priority
    }
}

// MARK: - Dates
//
// `due` is a LOCAL wall-clock string, "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM"
// (api.md, design.md D3). The classification below is the same rule as
// groupOf() in www/index.html, written the same way for the same reason:
// compare "YYYY-MM-DD" strings, never Date instants. Building a Date from a due
// and comparing instants is how a task due today jumps into Overdue at 19:00
// Chicago time, once the UTC day has rolled over.

/// "2026-09-05T14:30" -> ("2026-09-05", "14:30"); date-only -> time nil.
private func dueParts(_ due: String?) -> (date: String, time: String?)? {
    guard let due = due, due.count >= 10 else { return nil }
    let date = String(due.prefix(10))
    guard let t = due.firstIndex(of: "T") else { return (date, nil) }
    let after = due[due.index(after: t)...]
    guard after.count >= 5 else { return (date, nil) }
    return (date, String(after.prefix(5)))
}

/// Today as a local "YYYY-MM-DD" — the string todayStr(0) builds in the client.
private func localToday() -> String {
    let c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
    return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
}

/// Now as a local "HH:MM" — nowHHMM() in the client.
private func localNowHHMM() -> String {
    let c = Calendar.current.dateComponents([.hour, .minute], from: Date())
    return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
}

/// "14:30" -> "2:30 pm". Hand-rolled rather than DateFormatter because the
/// input is already a wall-clock string, and round-tripping it through a Date
/// would drag a time zone back into a calculation that has none.
private func fmtClock(_ hhmm: String) -> String {
    guard hhmm.count >= 5, let h = Int(hhmm.prefix(2)) else { return hhmm }
    let m = String(hhmm.dropFirst(3).prefix(2))
    let h12 = (h % 12) == 0 ? 12 : (h % 12)
    return "\(h12):\(m) " + (h < 12 ? "am" : "pm")
}

/// "just now" / "7m ago" / "3h ago" — the Scriptable widget's ago().
private func agoLabel(_ since: Date) -> String {
    let mins = Int((Date().timeIntervalSince(since) / 60).rounded())
    if mins < 1 { return "just now" }
    if mins < 60 { return String(mins) + "m ago" }
    return String(Int((Double(mins) / 60).rounded())) + "h ago"
}

// MARK: - What the widget draws
//
// These two are deliberately internal, not private: TaskMasterProvider's
// associated Entry type and TaskMasterWidgetView's stored property are both
// internal, and Swift refuses an internal declaration whose type is private.

struct TodayRow: Identifiable {
    let id: String
    let text: String
    let due: String
    let overdue: Bool
    /// Sorts overdue above today, then by when it is due, then by priority,
    /// then by uuid so the order is TOTAL — Swift's sort is not stable, and two
    /// tasks due in the same minute must not shuffle places between refreshes.
    /// It is docs/api.md's "Canonical order" key with the group rank glued on
    /// the front; orderKey() in www/index.html builds the same string, which is
    /// the whole point of design.md D8 — change one and you change both.
    let sortKey: String
}

struct TodayEntry: TimelineEntry {
    let date: Date
    let rows: [TodayRow]
    /// Everything that qualified, before the family's row limit truncated it.
    let total: Int
    /// When the shown list was actually fetched; nil when there has never been
    /// a successful fetch.
    let updated: Date?
    /// Non-nil replaces the body. Only used when there is no cache to fall
    /// back on — a stale list beats an error message.
    let error: String?
    /// The uuids that are ticked but not yet sent (kArmedKey). It is the ONLY
    /// thing the check marks are drawn from, so a timeline rebuilt in the
    /// middle of a grace period — the app going to the background is enough to
    /// cause one — redraws the row still ticked instead of snapping it back to
    /// an empty circle under the user's finger.
    let armed: Set<String>
}

/// "H"/"M"/"L"/nil -> "0"/"1"/"2"/"3" — the `<prio>` character of the canonical
/// sort key (api.md). A switch over a non-optional String rather than over the
/// Optional itself: matching an Optional against a string literal does work,
/// but there is nothing to be clever about here. Anything the server might one
/// day add that is not H/M/L sorts last, alongside the unset ones, which is the
/// same thing an unknown priority does in the client.
private func prioRank(_ priority: String?) -> String {
    switch priority ?? "" {
    case "H": return "0"
    case "M": return "1"
    case "L": return "2"
    default:  return "3"
    }
}

/// Turns a decoded list into the rows the widget draws: overdue + due today
/// only, sorted, labelled. Upcoming and undated tasks are deliberately absent —
/// this is the "Today" widget, and the app is one tap away for the rest.
private func rowsFrom(_ tasks: [APITask]) -> [TodayRow] {
    let today = localToday()
    let now = localNowHHMM()
    var out: [TodayRow] = []

    for t in tasks {
        guard let p = dueParts(t.due) else { continue }   // no due -> not today's problem
        if p.date > today { continue }                    // upcoming

        let overdue: Bool
        if p.date < today {
            overdue = true
        } else if let time = p.time, time <= now {
            // Due at 09:00 is overdue at 09:01. A DATE-ONLY due today is NOT
            // overdue until tomorrow, because date-only means "some time today"
            // (design.md D3) — which is why this is `if let time`, not `?? ""`.
            overdue = true
        } else {
            overdue = false
        }

        let label: String
        if overdue {
            label = "overdue"
        } else if let time = p.time {
            label = fmtClock(time)
        } else {
            label = "today"
        }

        // Descriptions may contain newlines (api.md); a row is one line.
        let flat = t.text
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        // api.md's canonical key — <date>T<HH:MM|99:99><prio><uuid> — with the
        // group rank in front, so one sort puts overdue above today instead of
        // two sorts over two arrays. Every part is fixed-width or ends in a
        // separator ("T", then a 5-char clock, then one prio digit), so plain
        // string comparison stays positional: no field can bleed into the next.
        //
        // Built in steps with explicit types rather than one six-term
        // expression: a long chain of `+` over String with a ternary and a `??`
        // in it is exactly the shape that makes the Swift type-checker slow,
        // and this file has to compile on a runner nobody is watching.
        let rank: String = overdue ? "0" : "1"
        let clock: String = p.time ?? "99:99"
        let prio: String = prioRank(t.priority)
        let sortKey: String = rank + p.date + "T" + clock + prio + t.uuid

        out.append(TodayRow(
            id: t.uuid,
            text: flat.isEmpty ? "(no description)" : flat,
            due: label,
            overdue: overdue,
            sortKey: sortKey))
    }

    out.sort { $0.sortKey < $1.sortKey }
    return out
}

/// Decodes a response body into rows. nil when the body is not the array of
/// tasks api.md promises — which is also how a 403 body or a captive-portal
/// HTML page gets rejected instead of cached over the good data.
private func decodeRows(_ data: Data) -> [TodayRow]? {
    guard let tasks = try? JSONDecoder().decode([APITask].self, from: data) else { return nil }
    return rowsFrom(tasks)
}

/// The last body that parsed, re-decoded NOW so the overdue/today split and the
/// clock labels are right for the current time even though the data is old.
/// Re-decoding rather than caching finished rows is the whole reason it is the
/// raw body that gets stored.
///
/// `armed` is passed in rather than read here: the cache is also what the
/// gallery's snapshot is built from, and nothing in the gallery is mid-grace.
private func cachedEntry(armed: Set<String>) -> TodayEntry? {
    let store = UserDefaults.standard
    guard let body = store.data(forKey: kCacheBodyKey),
          let rows = decodeRows(body) else { return nil }
    let when = store.object(forKey: kCacheDateKey) as? Date
    return TodayEntry(date: Date(), rows: rows, total: rows.count, updated: when,
                      error: nil, armed: armed)
}

/// GET /api/tasks?status=pending. Completion handlers, not async/await: this
/// target is compiled in Swift 5 language mode and a callback URLSession keeps
/// every actor and Sendable question off the table. Free functions rather than
/// methods for the same reason — nothing captures self.
private func fetchRows(_ done: @escaping ([TodayRow]?, Data?) -> Void) {
    guard let base = serverBase(), let url = URL(string: base + kTasksPath) else {
        done(nil, nil)
        return
    }

    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = kTimeout
    config.timeoutIntervalForResource = kTimeout
    config.waitsForConnectivity = false
    config.requestCachePolicy = .reloadIgnoringLocalCacheData
    let session = URLSession(configuration: config)

    let task = session.dataTask(with: url) { data, response, _ in
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200, let data = data, let rows = decodeRows(data) else {
            done(nil, nil)
            return
        }
        done(rows, data)
    }
    task.resume()
    // Nothing else is queued on this session; let it tear down when the one
    // task finishes rather than leaking it for the extension's lifetime.
    session.finishTasksAndInvalidate()
}

// MARK: - Completing a task from the widget (design.md D10)

/// The armed rows as they stand: uuid -> the instant that row was ticked.
///
/// compactMapValues rather than one `as? [String: Double]` over the whole
/// dictionary: an entry that will not read — a plist left by an older build,
/// say — drops itself instead of throwing away everybody else's tick.
private func armedMap() -> [String: Double] {
    let raw = UserDefaults.standard.dictionary(forKey: kArmedKey) ?? [:]
    return raw.compactMapValues { $0 as? Double }
}

/// Writes it back, removing the key outright when nothing is armed so the
/// resting state of this extension's defaults is what it was before the grace
/// period existed.
private func writeArmedMap(_ map: [String: Double]) {
    let store = UserDefaults.standard
    if map.isEmpty {
        store.removeObject(forKey: kArmedKey)
    } else {
        store.set(map, forKey: kArmedKey)
    }
}

/// The uuids still genuinely inside their grace period, with the stale ones
/// swept on the way past. getTimeline is the sweeper because it is the only
/// thing here that runs often and is allowed to take its time.
///
/// The age is compared with abs() on purpose. A sweep CANCELS an intent that is
/// at that moment sleeping on the stamp it just wrote, so the test has to keep
/// anything that could still be live: a clock that stepped backwards gives a
/// live tick a negative age, and abs() keeps it, while still dropping a stamp
/// from a clock that has moved a long way in either direction.
private func liveArmed() -> Set<String> {
    let now = Date().timeIntervalSince1970
    let map = armedMap()
    let live = map.filter { abs(now - $0.value) < kArmedMaxAge }
    if live.count != map.count { writeArmedMap(live) }
    return Set(live.keys)
}

/// The one WRITE this extension can make: POST /api/tasks/<uuid>/done, the same
/// call the app makes when a row is checked off.
///
/// It is deliberately dumb. It does not read the response body and does not
/// call WidgetCenter: WidgetKit reloads this widget's timeline as soon as the
/// intent returns, and the fetch that follows is the only account of what
/// happened worth believing. So a POST that fails shows up as "the row is still
/// there" — which is exactly right, and better than a row that vanishes locally
/// over a task the server never completed.
///
/// The same two limitations as the fetch (D7) apply: the server is whatever
/// serverBase() reads out of this extension's Info.plist, and no bearer token is
/// sent, so if TASKMASTER_TOKEN is ever set on the server the whole widget stops
/// working, not just this box.
private func postDone(_ uuid: String) async {
    // A uuid that cannot be put in a URL is not worth an error badge on the
    // home screen; the row simply stays. (The server refuses anything that is
    // not a full 36-character uuid anyway — api.md.)
    guard !uuid.isEmpty, let base = serverBase(),
          let url = URL(string: base + "/api/tasks/" + uuid + "/done") else {
        return
    }

    // No body and no Content-Type: the route takes neither (api.md), and
    // `done` is idempotent there, so a double tap settles instead of 502ing.
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = kTimeout

    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = kTimeout
    config.timeoutIntervalForResource = kTimeout
    config.waitsForConnectivity = false
    let session = URLSession(configuration: config)
    defer { session.finishTasksAndInvalidate() }

    // `try?`, on purpose: an intent that throws puts an error on the widget,
    // and "the tailnet is down" is not something anyone can act on from the
    // home screen.
    _ = try? await session.data(for: request)
}

/// What a tap on a row's box does.
///
/// A SetValueIntent behind a Toggle, rather than the AppIntent behind a Button
/// this used to be, for one reason: `Toggle(isOn:intent:)` is the control
/// SwiftUI flips OPTIMISTICALLY. The box becomes a green check the moment it is
/// touched, before perform() has been called, let alone returned — feedback a
/// Button(intent:) can never give, because a button only gets to redraw once its
/// intent is finished. WidgetKit sets `value` on this intent to the state the
/// tap produced before performing it, so `value == true` means "tick it" and
/// `value == false` means "untick it".
///
/// Internal, not private, for the same reason TodayRow is: Toggle takes it as a
/// generic AppIntent, and the App Intents metadata this target emits at build
/// time has to be able to name the type.
///
/// perform() IS the grace period. Ticking writes the uuid into kArmedKey with
/// the current instant, sleeps three seconds, then re-reads: the POST goes out
/// only if that same stamp is still there. A second tap runs this again with
/// `value == false`, which just removes the uuid — and the sleeping call, whose
/// stamp has gone, returns having sent nothing. Nothing about the row is decided
/// locally: it goes away when the next fetch says the server no longer lists it.
struct CompleteTaskIntent: SetValueIntent {
    /// A stored `static var` is how Apple's own samples declare this, and it is
    /// fine in the Swift 5 language mode this target builds in.
    static var title: LocalizedStringResource = "Complete task"

    /// A raw uuid string rather than an AppEntity: an entity would want a query
    /// type and a display representation, and nothing ever fills this in by
    /// hand — the toggle hands it the uuid of the row it was drawn for.
    @Parameter(title: "Task") var uuid: String

    /// SetValueIntent's one requirement, and it has to be spelled `value`: the
    /// state the box was moved TO. A @Parameter for the same reason uuid is —
    /// that is how the system carries a value into the performing process.
    @Parameter(title: "Done") var value: Bool

    /// AppIntent requires the empty init; the other one is what the row uses.
    init() {}

    init(uuid: String, value: Bool) {
        self.uuid = uuid
        self.value = value
    }

    func perform() async throws -> some IntentResult {
        guard !uuid.isEmpty else { return .result() }

        // Unticked — the second tap, inside the window. Forget the row and
        // stop: the call the FIRST tap left sleeping will find its stamp gone
        // and return without sending anything. This is the whole cancel.
        guard value else {
            var map = armedMap()
            map.removeValue(forKey: uuid)
            writeArmedMap(map)
            return .result()
        }

        // Ticked — arm the row. The timestamp is the token: anything that
        // disarms or re-arms this uuid replaces it, and this call then knows it
        // is no longer the one that speaks for the row.
        let stamp: Double = Date().timeIntervalSince1970
        var map = armedMap()
        map[uuid] = stamp
        writeArmedMap(map)

        // async/await lives HERE and nowhere else in this file — the timeline
        // still runs on completion handlers, which keeps every Sendable
        // question away from the provider. `try?` on the sleep because a
        // cancelled one means the system is reclaiming the process, and there
        // is nothing to do about that from here except not send; the sweep in
        // getTimeline is what clears the tick that gets left behind.
        try? await Task.sleep(nanoseconds: kGraceNanoseconds)

        guard armedMap()[uuid] == stamp else { return .result() }

        await postDone(uuid)

        // Disarm, but only if this call is still the one holding the row. The
        // tick has done its job; leaving it set would hold a check mark on a row
        // the next fetch is about to drop anyway.
        var after = armedMap()
        if after[uuid] == stamp { after.removeValue(forKey: uuid) }
        writeArmedMap(after)
        return .result()
    }
}

// MARK: - Provider

struct TaskMasterProvider: TimelineProvider {

    func placeholder(in context: Context) -> TodayEntry {
        // Redacted-placeholder content: the right SHAPE, never real data. The
        // ids are not uuids on purpose — the gallery does not run intents, and
        // if one ever did, the server refuses anything that is not a full
        // 36-character uuid (api.md) and CompleteTaskIntent swallows the 404.
        let rows = [
            TodayRow(id: "1", text: "Water the plants", due: "overdue", overdue: true, sortKey: "0"),
            TodayRow(id: "2", text: "Call the dentist", due: "2:30 pm", overdue: false, sortKey: "1"),
            TodayRow(id: "3", text: "Pay the water bill", due: "today", overdue: false, sortKey: "2")
        ]
        return TodayEntry(date: Date(), rows: rows, total: rows.count, updated: Date(),
                          error: nil, armed: [])
    }

    func getSnapshot(in context: Context, completion: @escaping (TodayEntry) -> Void) {
        // The widget gallery asks for this and will not wait on a network round
        // trip, so answer from the cache, or with the placeholder shape. Armed
        // is empty: the gallery is a picture, and nothing in it is mid-grace.
        completion(cachedEntry(armed: []) ?? placeholder(in: context))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<TodayEntry>) -> Void) {
        let next = Date().addingTimeInterval(kRefreshInterval)

        // Sweep first, then draw. This is the only thing in the file that runs
        // often enough to clear a tick left behind by an intent the system
        // killed mid-sleep, and what survives the sweep is exactly what the
        // rows are drawn ticked from.
        let armed: Set<String> = liveArmed()

        // No server in the Info.plist: an unstamped build (see serverBase()).
        // There is nothing to fetch and nothing a cache could stand in for, so
        // say what is actually wrong instead of "can't reach", which would send
        // whoever reads it to the Tailscale app for no reason.
        guard serverBase() != nil else {
            let entry = TodayEntry(date: Date(), rows: [], total: 0, updated: nil,
                                   error: "Server not configured", armed: armed)
            completion(Timeline(entries: [entry], policy: .after(next)))
            return
        }

        fetchRows { rows, body in
            let entry: TodayEntry
            if let rows = rows, let body = body {
                let now = Date()
                let store = UserDefaults.standard
                store.set(body, forKey: kCacheBodyKey)
                store.set(now, forKey: kCacheDateKey)
                entry = TodayEntry(date: now, rows: rows, total: rows.count, updated: now,
                                   error: nil, armed: armed)
            } else if let cached = cachedEntry(armed: armed) {
                // Off the tailnet, or the server is down. The last good list is
                // more useful than an error, and the "updated Xm ago" footer is
                // what admits it is stale.
                entry = cached
            } else {
                entry = TodayEntry(date: Date(), rows: [], total: 0, updated: nil,
                                   error: "Can't reach the server", armed: armed)
            }
            // .after, not .atEnd: there is exactly one entry, and its content
            // does not expire on a schedule the way prayerlist's day rollover
            // does — it goes stale when the tasks change, which is unknowable
            // from here. 15 minutes is the same hint the Scriptable widget gave.
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }
}

// MARK: - View

/// The check mark's glyph, and the square you have to hit to change it. 20 pt
/// where the circle used to be 12 ("slightly bigger would be nice") and a 28 pt
/// target around it, which is about a fingertip and still nowhere near the
/// whole row — see the comment on the Toggle in rowBody().
private let kSymbolSize: CGFloat = 18
private let kTapTarget: CGFloat = 26

/// How a row's Toggle draws: an empty circle that becomes a filled green check.
///
/// A style, rather than drawing the two states inline, because an intent-backed
/// Toggle is not something you get to hand a Binding — the system owns the
/// state. This is the documented way to style one: render `configuration.isOn`,
/// and write to it from a plain Button, which is what asks SwiftUI to run the
/// intent. `.buttonStyle(.plain)` keeps it from painting a bordered button over
/// the glyph, and is not optional.
///
/// The label goes in the HStack for completeness; the row passes an EmptyView,
/// so nothing is drawn there and no spacing is reserved for it.
private struct CheckToggleStyle: ToggleStyle {
    /// What an UNticked box is drawn in — red on an overdue row, gray
    /// otherwise, the same two colours the row's due label uses. Ticked is
    /// always green: it means the same thing on every row.
    let offColor: Color

    func makeBody(configuration: Configuration) -> some View {
        // Built as a local rather than inline in the modifier: two string
        // literals in a ternary handed straight to accessibilityLabel is the
        // shape that goes ambiguous between its LocalizedStringKey and
        // StringProtocol overloads, and this has to compile unattended.
        let spoken: String = configuration.isOn ? "Completing, tap to cancel" : "Complete"

        return Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 0) {
                Image(systemName: configuration.isOn ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: kSymbolSize))
                    .foregroundColor(configuration.isOn ? .green : offColor)
                    .accessibilityLabel(Text(spoken))
                    // The glyph is 20 pt; this frame is what makes the TAP
                    // TARGET 28, and contentShape below is what makes all 28 of
                    // it tappable instead of just the ring itself.
                    .frame(width: kTapTarget, height: kTapTarget)
                configuration.label
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct TaskMasterWidgetView: View {
    var entry: TodayEntry
    @Environment(\.widgetFamily) private var family

    /// How many rows the family has room for. Small is the count plus a glance;
    /// large is the whole morning.
    private var rowLimit: Int {
        switch family {
        case .systemSmall:  return 3
        case .systemMedium: return 5   // 5 × 26 pt rows + header + footer fit the 170 pt medium family; 6 did not
        default:            return 12
        }
    }

    private var shown: [TodayRow] { Array(entry.rows.prefix(rowLimit)) }
    private var hidden: Int { max(0, entry.total - shown.count) }

    private var footerText: String {
        guard let updated = entry.updated else { return "no data yet" }
        return "updated " + agoLabel(updated)
    }

    /// One row: the tap target, the description, the due label.
    ///
    /// Its own method rather than an inline closure in body(). A Toggle with an
    /// intent, a custom style, a frame and two labels is now the most involved
    /// expression in this file, and long expressions are exactly what make the
    /// Swift type-checker slow on a runner nobody is watching.
    private func rowBody(_ row: TodayRow) -> some View {
        // The ONLY source of the check mark. It comes from the entry rather
        // than from any @State so that a redraw during the grace period — the
        // app backgrounding is enough to trigger one — keeps it showing.
        let armed: Bool = entry.armed.contains(row.id)

        // .center, where this used to be .firstTextBaseline. Baseline alignment
        // lines a 20 pt glyph's baseline up with 13 pt text, which hangs the box
        // high above the row's optical middle and drops the due label with it.
        // The row's height is now the tap target's, not the text's, so centring
        // the three pieces on that box is what actually lines up.
        return HStack(alignment: .center, spacing: 4) {
            // The box IS the control (design.md D10), and it is still no bigger
            // than a tap needs: everything around it is the .widgetURL below,
            // so a near-miss opens the app. What changed is the cost of a miss
            // that LANDS — it now takes a second tap within three seconds to
            // undo, instead of having completed a task nobody meant to complete.
            //
            // WidgetKit sets `value` on the intent to the state the tap
            // produced before performing it; passing the same thing here costs
            // nothing, and makes the row say out loud what a tap on it means.
            Toggle(isOn: armed, intent: CompleteTaskIntent(uuid: row.id, value: !armed)) {
                // No label: the description next door is NOT part of the tap
                // target and must not become part of it — see above.
                EmptyView()
            }
            .toggleStyle(CheckToggleStyle(offColor: row.overdue ? .red : .gray))
            Text(row.text)
                .font(.system(size: 13))
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 4)
            // fixedSize so a long description truncates and the due label never
            // does — the label is the point.
            Text(row.due)
                .font(.system(size: 11))
                .foregroundColor(row.overdue ? .red : .gray)
                .fixedSize()
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                Text("Today")
                    .font(.system(size: 15, weight: .semibold))
                Spacer(minLength: 4)
                Text(String(entry.total))
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundColor(.gray)
            }
            .padding(.bottom, 6)

            if let error = entry.error {
                Text(error)
                    .font(.system(size: 13))
                    .foregroundColor(.gray)
                    .lineLimit(2)
            } else if entry.rows.isEmpty {
                Text("Nothing due 🎉")
                    .font(.system(size: 14))
                    .foregroundColor(.gray)
            } else {
                // spacing 0 where it used to be 3: a row is now as tall as
                // its 28 pt tap target rather than as tall as 13 pt of text,
                // and the clear space that target leaves around the 20 pt glyph
                // is already wider than the gap the old spacing drew.
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(shown) { row in
                        rowBody(row)
                    }
                    if hidden > 0 {
                        Text("+" + String(hidden) + " more")
                            .font(.system(size: 11))
                            .foregroundColor(.gray)
                            .padding(.top, 2)
                    }
                }
            }

            Spacer(minLength: 0)

            Text(footerText)
                .font(.system(size: 9))
                .foregroundColor(.gray)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        // Tapping anywhere opens the app. The app does not have to HANDLE the
        // URL — iOS launches it on the scheme alone — but the scheme has to be
        // registered in the APP's Info.plist (CFBundleURLTypes) or the tap does
        // nothing at all.
        .widgetURL(URL(string: "taskmaster://today"))
        .modifier(WidgetContainerBackground())
    }
}

/// iOS 17 made `containerBackground` mandatory: a widget that does not declare
/// one is drawn with no background at all, which reads as a bug. With this
/// target's floor now at 17 (design.md D10) there is no second path left — the
/// #available check, the pre-17 padding, and the Color(UIColor.systemBackground)
/// fill that used to live here are all gone, and the UIKit import with them.
///
/// Still a ViewModifier rather than an inlined call so the view's body stays one
/// flat list of modifiers, and so the next change to the widget's background has
/// exactly one place to happen.
private struct WidgetContainerBackground: ViewModifier {
    func body(content: Content) -> some View {
        content.containerBackground(.background, for: .widget)
    }
}

// MARK: - Entry point

@main
struct TaskMasterWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "TaskMasterWidget", provider: TaskMasterProvider()) { entry in
            TaskMasterWidgetView(entry: entry)
        }
        .configurationDisplayName("Today")
        .description("Overdue and due-today tasks from TaskMaster.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

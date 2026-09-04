//
//  TaskMasterWidget.swift
//  The home-screen widget: the server's widget feed, drawn one row per line,
//  with a tap-to-complete check box on every row.
//
//  WHAT IS SHOWN IS NOT DECIDED HERE (design.md D14). This file used to GET
//  /api/tasks and work out for itself which tasks were overdue or due today,
//  in what order, and under what label. It does none of that any more: it GETs
//  /api/widget, and the SERVER — applying the user's `prefs.widget`, which the
//  app edits under Settings → Widget — decides which groups are in (overdue /
//  today / upcoming / none), which category, how many rows each family gets,
//  whether the category is drawn, and what each row's due label says. The rows
//  arrive in display order and are drawn in the order they arrive.
//
//  So "the widget is showing the wrong thing" is a Settings change or a server
//  change, and never a change here. What IS still this file's business: the
//  chrome — the "Today" header and its count, "+N more", "Nothing due 🎉", the
//  "updated Xm ago" footer — the layout of a row, the check box, and the cache.
//
//  It is still the native replacement for scriptable-today-widget.js in
//  ~/projects/dashboards, which is fed separately by `pa` and unaffected (D1).
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

/// The feed, and the whole of what this widget knows (api.md, "Widget feed").
/// No query string: every choice that used to be one — pending only, which
/// groups, how many rows, which category — now lives in `prefs.widget` on the
/// server, so there is nothing left to ask for.
private let kWidgetPath = "/api/widget"

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

// MARK: - The widget feed, exactly as api.md spells it
//
// `{"updated": iso, "total": int, "rows": [{uuid, text, due, overdue, group,
// category}], "caps": {small, medium, large}}`.
//
// Every field but `uuid`, `text` and `rows` itself is Optional, so the
// synthesised decoder uses decodeIfPresent and a server that omits one — or
// sends null — loses that field instead of failing the whole feed. `rows` is
// deliberately NOT optional: it is what makes this a widget feed, and requiring
// it is what keeps a 403 body or a captive portal's HTML from decoding to an
// empty list and being cached over the last good one.
//
// Dates: there are none here any more. `due` arrives as the finished LABEL the
// server built in the server's zone ("overdue", "today", "2:30 pm",
// "Thu Sep 4", ""), and this file never parses, compares or formats a date
// again. The string-comparison rule that used to live here (design.md D3, the
// twin of groupOf() in www/index.html) now lives on the server, once, for the
// app and the widget both.

private struct FeedRow: Decodable {
    let uuid: String
    /// Already flattened to one line by the server.
    let text: String
    /// The label to DRAW. Not a date, not parsed, not reformatted.
    let due: String?
    /// The only thing that decides the red.
    let overdue: Bool?
    /// "overdue" | "today" | "upcoming" | "none". Informational: the rows are
    /// already grouped and ordered, so nothing here reads it. Decoded so the
    /// shape in this file matches the shape in api.md.
    let group: String?
    /// Filled only when `prefs.widget.show_category` is on; "" or absent when
    /// it is off, and then nothing is drawn.
    let category: String?
}

/// `prefs.widget.rows` echoed back, so a row count chosen in Settings reaches
/// the home screen without a rebuild.
private struct FeedCaps: Decodable {
    let small: Int?
    let medium: Int?
    let large: Int?
}

private struct WidgetFeed: Decodable {
    /// When the SERVER built this feed. Decoded because api.md lists it; not
    /// drawn — see footerText, which is about when this WIDGET last got data.
    let updated: String?
    /// Everything that qualified, before the server's own `rows.large` limit.
    /// It is what "+N more" counts against.
    let total: Int?
    let rows: [FeedRow]
    /// `prefs.widget.rows` echoed back under its OWN key — "a separate key,
    /// since `rows` is the array" (api.md, which said `rows` for both until the
    /// collision was spotted building this). Absent, kFits* stands.
    let caps: FeedCaps?

    // No CodingKeys: every property above is spelled exactly as the wire is.
}

// MARK: - How many rows a family gets

/// What each family has ROOM for at the 26 pt row height below. The ceiling on
/// anything Settings asks for, and the answer when the feed carries no caps.
private let kFitsSmall = 3
private let kFitsMedium = 5   // 5 × 26 pt rows + header + footer fit the 170 pt medium family; 6 did not
private let kFitsLarge = 12

/// The resolved caps for one feed: never optional past this point, so the view
/// has one rule (`min(cap, what fits)`) and no fallbacks scattered through it.
///
/// Internal, not private, for the reason given above TodayRow: it is stored on
/// TodayEntry, which is internal, and Swift refuses an internal declaration
/// whose type is private.
struct RowCaps {
    let small: Int
    let medium: Int
    let large: Int

    /// What the widget did before the server had an opinion, and what it does
    /// again when the feed carries no caps.
    static let fallback = RowCaps(small: kFitsSmall, medium: kFitsMedium, large: kFitsLarge)

    func cap(for family: WidgetFamily) -> Int {
        switch family {
        case .systemSmall:  return small
        case .systemMedium: return medium
        default:            return large
        }
    }
}

/// One cap, or the fallback. Absent and null both mean "unset"; so does anything
/// <= 0, which would otherwise draw a header, a footer and no tasks at all — a
/// widget indistinguishable from a broken one, and there is no way to tell a
/// deliberate zero from a bad write.
private func saneCap(_ n: Int?, _ fallback: Int) -> Int {
    guard let n = n, n > 0 else { return fallback }
    return n
}

private func capsFrom(_ f: FeedCaps?) -> RowCaps {
    guard let f = f else { return .fallback }
    return RowCaps(small: saneCap(f.small, kFitsSmall),
                   medium: saneCap(f.medium, kFitsMedium),
                   large: saneCap(f.large, kFitsLarge))
}

// MARK: - Dates

/// "just now" / "7m ago" / "3h ago" — the Scriptable widget's ago(). The one
/// piece of date arithmetic left in this file, and it is about the FETCH, not
/// about any task.
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
    /// The server's label, drawn verbatim. May be "" — an upcoming-only feed
    /// with no clock on the task has nothing to say on the right of the row.
    let due: String
    let overdue: Bool
    /// "" unless Settings → Widget asked for it. Never a lookup: whether a
    /// category appears is the server's call, and this is only whether to draw
    /// the string it sent.
    let category: String
}

struct TodayEntry: TimelineEntry {
    let date: Date
    let rows: [TodayRow]
    /// Everything that qualified, straight from the feed's `total` — counted by
    /// the server before its own row limit, and so still right for "+N more"
    /// after this family truncates further.
    let total: Int
    /// The per-family row counts this feed came with, already resolved.
    let caps: RowCaps
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

/// One decoded feed: the rows to draw, the count to draw them against, and the
/// caps that came with them. Private, because nothing internal names it — it is
/// only ever a local inside a function body.
private struct FeedContents {
    let rows: [TodayRow]
    let total: Int
    let caps: RowCaps
}

/// Decodes a response body. nil when the body is not the feed api.md promises —
/// which is also how a 403 body or a captive-portal HTML page gets rejected
/// instead of cached over the good data.
///
/// The mapping is deliberately dull: it renames nothing, decides nothing, and
/// re-orders nothing. Two defensive touches only, both about DRAWING rather
/// than about meaning — the newline flatten (the server flattens `text`
/// already; a stray one would still break a one-line row) and the empty-text
/// placeholder, which is also what the trim is for.
private func decodeFeed(_ data: Data) -> FeedContents? {
    guard let feed = try? JSONDecoder().decode(WidgetFeed.self, from: data) else { return nil }

    // The closure is spelled out — parameter type and result type both — rather
    // than left to inference. It is multi-statement, and a body the type-checker
    // has to solve from scratch is what makes a Swift build slow on a runner
    // nobody is watching.
    let rows: [TodayRow] = feed.rows.map { (r: FeedRow) -> TodayRow in
        let flat: String = r.text
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return TodayRow(id: r.uuid,
                        text: flat.isEmpty ? "(no description)" : flat,
                        due: r.due ?? "",
                        overdue: r.overdue ?? false,
                        category: r.category ?? "")
    }

    // `total` counts what qualified server-side, so it is >= rows.count and is
    // what "+N more" is measured against. Missing, the rows themselves are the
    // only count there is.
    return FeedContents(rows: rows,
                        total: feed.total ?? rows.count,
                        caps: capsFrom(feed.caps))
}

/// The last body that parsed, decoded again on the way out. Storing the raw
/// body rather than finished rows is what lets the decode change — as it just
/// did — without a cache format to migrate, and it costs nothing.
///
/// What it no longer does is RECOMPUTE anything: the labels in a cached body are
/// the ones the server wrote when it was fetched, so a cache that survives
/// midnight can say "today" about yesterday. That is what the "updated Xm ago"
/// footer is for, and it is the same trade as showing a stale list at all.
///
/// `armed` is passed in rather than read here: the cache is also what the
/// gallery's snapshot is built from, and nothing in the gallery is mid-grace.
private func cachedEntry(armed: Set<String>) -> TodayEntry? {
    let store = UserDefaults.standard
    guard let body = store.data(forKey: kCacheBodyKey),
          let feed = decodeFeed(body) else { return nil }
    let when = store.object(forKey: kCacheDateKey) as? Date
    return TodayEntry(date: Date(), rows: feed.rows, total: feed.total, caps: feed.caps,
                      updated: when, error: nil, armed: armed)
}

/// GET /api/widget. Completion handlers, not async/await: this target is
/// compiled in Swift 5 language mode and a callback URLSession keeps every actor
/// and Sendable question off the table. Free functions rather than methods for
/// the same reason — nothing captures self.
private func fetchFeed(_ done: @escaping (FeedContents?, Data?) -> Void) {
    guard let base = serverBase(), let url = URL(string: base + kWidgetPath) else {
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
        guard code == 200, let data = data, let feed = decodeFeed(data) else {
            done(nil, nil)
            return
        }
        done(feed, data)
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
        //
        // No categories on these: `show_category` is off by default, so this is
        // what the widget looks like out of the box.
        let rows = [
            TodayRow(id: "1", text: "Water the plants", due: "overdue", overdue: true, category: ""),
            TodayRow(id: "2", text: "Call the dentist", due: "2:30 pm", overdue: false, category: ""),
            TodayRow(id: "3", text: "Pay the water bill", due: "today", overdue: false, category: "")
        ]
        return TodayEntry(date: Date(), rows: rows, total: rows.count, caps: .fallback,
                          updated: Date(), error: nil, armed: [])
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
            let entry = TodayEntry(date: Date(), rows: [], total: 0, caps: .fallback,
                                   updated: nil, error: "Server not configured", armed: armed)
            completion(Timeline(entries: [entry], policy: .after(next)))
            return
        }

        fetchFeed { feed, body in
            let entry: TodayEntry
            if let feed = feed, let body = body {
                let now = Date()
                let store = UserDefaults.standard
                store.set(body, forKey: kCacheBodyKey)
                store.set(now, forKey: kCacheDateKey)
                entry = TodayEntry(date: now, rows: feed.rows, total: feed.total,
                                   caps: feed.caps, updated: now, error: nil, armed: armed)
            } else if let cached = cachedEntry(armed: armed) {
                // Off the tailnet, or the server is down. The last good list is
                // more useful than an error, and the "updated Xm ago" footer is
                // what admits it is stale.
                entry = cached
            } else {
                entry = TodayEntry(date: Date(), rows: [], total: 0, caps: .fallback,
                                   updated: nil, error: "Can't reach the server", armed: armed)
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

/// The check mark's glyph, and the square you have to hit to change it. 18 pt
/// where the circle used to be 12 ("slightly bigger would be nice") and a 26 pt
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
                    // The glyph is 18 pt; this frame is what makes the TAP
                    // TARGET 26, and contentShape below is what makes all 26 of
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

    /// How many rows to draw: what Settings → Widget asked for, but never more
    /// than the family has ROOM for. The cap is a preference and kFits* is a
    /// fact about a 170 pt box, so the smaller of the two is the only answer
    /// that cannot overflow — asking for 20 rows on a small widget gets 3.
    ///
    /// With no caps in the feed both sides are kFits*, and the limit is exactly
    /// the 3/5/12 this widget has always used.
    private var rowLimit: Int {
        let fits: Int
        switch family {
        case .systemSmall:  fits = kFitsSmall
        case .systemMedium: fits = kFitsMedium
        default:            fits = kFitsLarge
        }
        return min(entry.caps.cap(for: family), fits)
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
        // lines an 18 pt glyph's baseline up with 13 pt text, which hangs the box
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
            // "Water the plants · work", drawn only when the server sent a
            // category — which it does only when Settings → Widget asked for
            // one. A separate Text rather than one interpolated string because
            // the two halves are different sizes and colours, and because the
            // DESCRIPTION is the half that gives way: it is the only flexible
            // view in this row, so SwiftUI hands the fixedSize ones their ideal
            // width and truncates it, which is the wanted order — the category
            // is a word, and half a word says nothing.
            if !row.category.isEmpty {
                Text("· " + row.category)
                    .font(.system(size: 11))
                    .foregroundColor(.gray)
                    .lineLimit(1)
                    .fixedSize()
            }
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
                // its 26 pt tap target rather than as tall as 13 pt of text,
                // and the clear space that target leaves around the 18 pt glyph
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
        // Deliberately vaguer than it was ("Overdue and due-today tasks"): which
        // groups appear is a preference now, so the gallery cannot promise one.
        .description("Your TaskMaster list, as set up in Settings → Widget.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

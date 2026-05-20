import CryptoKit
import Foundation
@preconcurrency import UserNotifications

struct IdentityCard: Codable {
    var agentName: String
    var selfIntroduction: String
    var dimensions: [Dimension]
    let createdAt: String
    let updatedAt: String

    // Optional display fields — written by the agent into the encrypted body
    var signature: [String]?        // two-line poetic signature shown on Identity page
    var category: String?           // e.g. "Quiet · Observant"
    var daysWithUserWritten: Int?   // server-computed live count from the relationship anchor

    // v1 envelope fields (present when server stored ciphertext)
    let v: Int?
    let body_ct: String?
    let nonce: String?
    let K_user: String?
    let K_enclave: String?
    let visibility: String?
    let owner_user_id: String?
    let id: String?

    struct Dimension: Codable, Identifiable {
        let name: String
        let value: Int
        let description: String
        let lastNudgeReason: String?
        var delta: String?          // e.g. "+0.4" or "−0.2" — written by agent

        var id: String { name }
        var normalizedValue: Double { Double(max(0, min(100, value))) / 100.0 }

        enum CodingKeys: String, CodingKey {
            case name, value, description, delta
            case lastNudgeReason = "last_nudge_reason"
        }
    }

    /// Trust the server-computed value. The enclave derives this live from
    /// the relationship anchor every read, so the count auto-increments
    /// daily without any client-side math. The previous "snapshot + elapsed"
    /// hack was unstable — every envelope rewrite (init/replace/nudge/swap)
    /// would reset updatedAt and zero the elapsed counter.
    var daysWithUser: Int {
        daysWithUserWritten ?? 0
    }

    enum CodingKeys: String, CodingKey {
        case agentName = "agent_name"
        case selfIntroduction = "self_introduction"
        case dimensions, signature, category
        case daysWithUserWritten = "days_with_user"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case v, body_ct, nonce, K_user, K_enclave, visibility, owner_user_id, id
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        agentName            = (try? c.decode(String.self, forKey: .agentName)) ?? ""
        selfIntroduction     = (try? c.decode(String.self, forKey: .selfIntroduction)) ?? ""
        dimensions           = (try? c.decode([Dimension].self, forKey: .dimensions)) ?? []
        createdAt            = (try? c.decode(String.self, forKey: .createdAt)) ?? ""
        updatedAt            = (try? c.decode(String.self, forKey: .updatedAt)) ?? ""
        signature            = try? c.decode([String].self, forKey: .signature)
        category             = try? c.decode(String.self, forKey: .category)
        daysWithUserWritten  = try? c.decode(Int.self, forKey: .daysWithUserWritten)
        v               = try? c.decode(Int.self, forKey: .v)
        body_ct         = try? c.decode(String.self, forKey: .body_ct)
        nonce           = try? c.decode(String.self, forKey: .nonce)
        K_user          = try? c.decode(String.self, forKey: .K_user)
        K_enclave       = try? c.decode(String.self, forKey: .K_enclave)
        visibility      = try? c.decode(String.self, forKey: .visibility)
        owner_user_id   = try? c.decode(String.self, forKey: .owner_user_id)
        id              = try? c.decode(String.self, forKey: .id)
    }

    var isEncryptedEnvelope: Bool {
        (v ?? 0) >= 1 && body_ct != nil && agentName.isEmpty
    }

    func decryptedIfNeeded(withUserSK sk: Curve25519.KeyAgreement.PrivateKey) -> IdentityCard {
        func fromB64(_ s: String?) -> Data? {
            guard let s = s else { return nil }
            return Data(base64Encoded: s)
        }
        guard isEncryptedEnvelope,
              let bodyCT = fromB64(body_ct),
              let nonceData = fromB64(nonce),
              let kUser = fromB64(K_user),
              let owner = owner_user_id
        else { return self }
        let envelope = ContentEncryption.Envelope(
            id: id ?? "", v: v ?? 1,
            ownerUserID: owner,
            visibility: (visibility == "local_only") ? .localOnly : .shared,
            bodyCT: bodyCT,
            nonce: nonceData,
            kUser: kUser,
            kEnclave: fromB64(K_enclave),
            enclavePKFingerprint: ""
        )
        do {
            let pt = try ContentEncryption.unseal(envelope, withUserSK: sk)
            struct Inner: Decodable {
                let agent_name: String?
                let self_introduction: String?
                let dimensions: [Dimension]?
                let signature: [String]?
                let category: String?
                // days_with_user intentionally omitted — see below.
            }
            let inner = try JSONDecoder().decode(Inner.self, from: pt)
            var copy = self
            copy.agentName           = inner.agent_name ?? ""
            copy.selfIntroduction    = inner.self_introduction ?? ""
            copy.dimensions          = inner.dimensions ?? []
            copy.signature           = inner.signature
            copy.category            = inner.category
            // DELIBERATELY do NOT touch daysWithUserWritten here.
            //
            // Earlier this code did:
            //   copy.daysWithUserWritten = inner.days_with_user ?? self.daysWithUserWritten
            // which overwrote the server's live-computed days
            // (already decoded above from the top-level JSON field
            // `days_with_user`, which the server recomputes every
            // request from the relationship anchor) with a STALE
            // value embedded in the encrypted body at bootstrap time
            // — freezing the displayed count permanently.
            //
            // The server's anchor is the single source of truth.
            // Whatever Int (if any) sits inside the envelope body is
            // a historical artifact and must be ignored. Keeping the
            // server's value already on `self` does the right thing.
            return copy
        } catch {
            log("[identity] unseal failed: \(error)")
            var copy = self
            copy.agentName = "[encrypted — decrypt failed]"
            return copy
        }
    }
}

/// One entry in the identity-change feed (/v1/identity/changes). Mirrors
/// the dict returned by backend's `_load_identity_changes`. Each card in
/// the iOS "最近的变化" section is one of these.
struct IdentityChange: Codable, Identifiable, Equatable {
    let id: String
    let ts: String                  // ISO 8601 from server
    let action: String              // "init" | "replace" | "nudge"
    let dimension: String?
    let oldValue: Int?
    let newValue: Int?
    let delta: Int?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case id, ts, action, dimension, reason
        case oldValue = "old_value"
        case newValue = "new_value"
        case delta
    }

    /// `true` for changes worth surfacing as a local push (big nudges or
    /// any replace). Small ±1/2 nudges accumulate in the feed but don't
    /// pop a notification — would be too noisy.
    var deservesNotification: Bool {
        switch action {
        case "init", "replace": return true
        case "nudge":           return abs(delta ?? 0) >= 5
        default:                return false
        }
    }
}

@MainActor
class IdentityViewModel: ObservableObject {
    @Published var identity: IdentityCard? = nil
    @Published var isLoading = false
    @Published var didJustBootstrap = false
    /// Newest-first list of identity changes; powers the "最近的变化" feed.
    @Published var recentChanges: [IdentityChange] = []

    private var timer: Timer?
    private var wasNil = true
    private var resetObserver: NSObjectProtocol?
    // Tracks which change IDs we've already shown a local push for, so the
    // poll loop doesn't re-fire the same notification every 10s.
    private var notifiedChangeIDs: Set<String> = []

    init() {
        resetObserver = NotificationCenter.default.addObserver(
            forName: .feedlingCredentialsReset,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.resetForFreshAccount()
            }
        }
    }

    deinit {
        if let resetObserver { NotificationCenter.default.removeObserver(resetObserver) }
    }

    /// Drop the cached identity so IdentityView immediately re-renders its
    /// pre-bootstrap state instead of showing the old agent's name + radar.
    private func resetForFreshAccount() {
        identity = nil
        isLoading = false
        didJustBootstrap = false
        wasNil = true
        recentChanges = []
        notifiedChangeIDs.removeAll()
    }

    func startPolling() {
        Task {
            await loadIdentity()
            await loadRecentChanges()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task {
                await self?.loadIdentity()
                await self?.loadRecentChanges()
            }
        }
    }

    func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    private func contentSK() -> Curve25519.KeyAgreement.PrivateKey? {
        do { return try ContentKeyStore.shared.loadPrivateKey() } catch { return nil }
    }

    /// Poll /v1/identity/changes. New entries (not in notifiedChangeIDs)
    /// fire a local push if they pass the deservesNotification filter.
    /// This is the "agent silently nudged my dimensions — let me know"
    /// surface; the existing chat / memory poll loops aren't touched.
    func loadRecentChanges() async {
        guard let req = FeedlingAPI.shared.authorizedRequest(
            path: "/v1/identity/changes",
            queryItems: [URLQueryItem(name: "limit", value: "50")]
        ) else { return }
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            struct Response: Codable {
                let changes: [IdentityChange]
            }
            let decoded = try JSONDecoder().decode(Response.self, from: data)
            let previouslySeen = notifiedChangeIDs
            // Find changes we haven't notified for AND that deserve a push.
            // First poll: notifiedChangeIDs is empty — seed it without
            // firing pushes (so old history doesn't spam on first launch).
            let isFirstPoll = previouslySeen.isEmpty && recentChanges.isEmpty
            let newOnes = decoded.changes.filter {
                !previouslySeen.contains($0.id) && $0.deservesNotification
            }
            recentChanges = decoded.changes
            for c in decoded.changes {
                notifiedChangeIDs.insert(c.id)
            }
            if !isFirstPoll {
                for c in newOnes {
                    IdentityChangeNotifier.shared.fire(change: c,
                                                      agentName: identity?.agentName ?? "")
                }
            }
        } catch {
            print("[IdentityVM] changes load error: \(error)")
        }
    }

    func loadIdentity() async {
        guard let req = FeedlingAPI.shared.authorizedRequest(path: "/v1/identity/get") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            struct Response: Codable {
                let identity: IdentityCard?
            }
            let decoded = try JSONDecoder().decode(Response.self, from: data)
            var newIdentity = decoded.identity
            if let sk = contentSK(), var id = newIdentity {
                newIdentity = id.decryptedIfNeeded(withUserSK: sk)
                _ = id
            }
            if wasNil && newIdentity != nil {
                didJustBootstrap = true
            }
            wasNil = newIdentity == nil
            identity = newIdentity
            if let days = newIdentity?.daysWithUser {
                LiveActivityManager.shared.setDays(days)
            }
        } catch {
            log("[IdentityVM] load error: \(error)")
        }
    }
}


// MARK: - Identity-change local notifier
//
// Fires a UNUserNotification when a new identity change lands. Title is
// the agent's name (so the lock-screen shows "小哆啦" + body), body is
// the reason text verbatim (truncated by iOS to ~80 chars on lock screen).
//
// This is intentionally NOT an APNs push — it's a LOCAL notification
// scheduled by iOS itself when polling detects a new entry. No server-
// side push tokens or external delivery; works offline if the change
// already landed and is polled later.
//
// We don't include the dimension/delta in the body; the reason field
// already speaks in the agent's voice and is what the user came for.

@MainActor
final class IdentityChangeNotifier {
    static let shared = IdentityChangeNotifier()
    private init() {}

    private var permissionRequested = false

    func fire(change: IdentityChange, agentName: String) {
        let body = (change.reason ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        // Skip silent pushes — if the agent didn't bother writing a reason
        // there's nothing meaningful to notify about.
        guard !body.isEmpty else { return }

        let center = UNUserNotificationCenter.current()
        ensurePermission(center: center) { [weak self] granted in
            guard granted else { return }
            self?.schedule(change: change, agentName: agentName, body: body, center: center)
        }
    }

    private func ensurePermission(center: UNUserNotificationCenter, completion: @escaping @MainActor (Bool) -> Void) {
        center.getNotificationSettings { settings in
            let status = settings.authorizationStatus
            Task { @MainActor in
                switch status {
                case .authorized, .provisional, .ephemeral:
                    completion(true)
                case .denied:
                    completion(false)
                case .notDetermined:
                    // Only request once per session — don't pester the user.
                    guard !self.permissionRequested else { completion(false); return }
                    self.permissionRequested = true
                    do {
                        let granted = try await center.requestAuthorization(options: [.alert, .sound])
                        completion(granted)
                    } catch {
                        completion(false)
                    }
                @unknown default:
                    completion(false)
                }
            }
        }
    }

    private func schedule(change: IdentityChange,
                          agentName: String,
                          body: String,
                          center: UNUserNotificationCenter) {
        let content = UNMutableNotificationContent()
        content.title = agentName.isEmpty ? "Identity update" : agentName
        // Subtitle gives the dimension/delta context above the body so the
        // user can scan it without expanding. Only for nudges; init/replace
        // skip subtitle (no diff to show).
        if change.action == "nudge",
           let dim = change.dimension, let delta = change.delta {
            let sign = delta > 0 ? "+" : ""
            content.subtitle = "\(dim) \(sign)\(delta)"
        }
        content.body = body
        // userInfo carries the change id so a future tap-handler can
        // navigate to the Identity tab and highlight this card.
        content.userInfo = ["identity_change_id": change.id, "action": change.action]
        // Use a trigger of nil (deliver immediately).
        let request = UNNotificationRequest(
            identifier: "identity_change_\(change.id)",
            content: content,
            trigger: nil
        )
        center.add(request) { error in
            if let error {
                print("[IdentityChangeNotifier] schedule failed: \(error)")
            }
        }
    }
}

import ActivityKit
import ReplayKit
import SwiftUI

// MARK: - Settings View

struct SettingsView: View {
    @EnvironmentObject var lam: LiveActivityManager
    @ObservedObject private var api = FeedlingAPI.shared

    // Self-hosted draft fields (only persisted when user taps Save)
    @State private var selfHostedURL: String = ""
    @State private var selfHostedKey: String = ""
    @State private var showCopiedToast: String? = nil

    private let isChinese: Bool =
        Locale.preferredLanguages.first?.hasPrefix("zh") ?? false

    @State private var isBroadcasting = false
    @State private var showDeleteConfirm: Bool = false
    @State private var isDeleting: Bool = false
    @State private var deleteError: String? = nil
    @State private var showPostWipeReimport: Bool = false
    @State private var showRegenerateConfirm: Bool = false
    @State private var isRegenerating: Bool = false
    private let broadcastPollTimer = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    private let mockStates: [ScreenActivityAttributes.ContentState] = [
        .init(title: "OpenClaw",
              body: "45 min on TikTok. That's your entertainment budget.",
              data: ["top_app": "TikTok", "minutes": "45"],
              updatedAt: Date()),
        .init(title: "OpenClaw",
              body: "Deep work mode. 95 min in Figma.",
              data: ["top_app": "Figma", "minutes": "95"],
              updatedAt: Date()),
        .init(title: "OpenClaw",
              body: "28 min on Instagram. Wrap it up?",
              data: ["top_app": "Instagram", "minutes": "28"],
              updatedAt: Date()),
    ]
    @State private var mockIndex = 0

    var body: some View {
        NavigationStack {
            ZStack {
                Color.cinBg.ignoresSafeArea()
                ScrollView {
                    VStack(spacing: 0) {
                        settingsHeader
                        Rectangle().fill(Color.cinFg).frame(height: 1)
                        screenRecordingCard
                        liveActivityCard
                        // Storage card
                        VStack(alignment: .leading, spacing: 0) {
                            Text("STORAGE")
                                .font(.dmMono(size: 9.5, weight: .medium))
                                .foregroundStyle(Color.cinAccent1)
                                .kerning(3)
                                .padding(.horizontal, 24)
                                .padding(.top, 18)
                                .padding(.bottom, 12)

                            VStack(spacing: 0) {
                                // Mode toggle
                                HStack(spacing: 0) {
                                    ForEach([FeedlingAPI.StorageMode.cloud, .selfHosted], id: \.rawValue) { mode in
                                        Button {
                                            if mode == .cloud {
                                                api.configureCloud()
                                                if api.apiKey.isEmpty {
                                                    Task { await api.ensureRegisteredIfCloud() }
                                                }
                                            } else {
                                                api.enterSelfHostedMode()
                                            }
                                        } label: {
                                            Text(mode == .cloud ? "CLOUD" : "SELF-HOSTED")
                                                .font(.dmMono(size: 8.5, weight: .medium))
                                                .kerning(2)
                                                .foregroundStyle(api.storageMode == mode ? .white : Color.cinSub)
                                                .frame(maxWidth: .infinity)
                                                .frame(height: 40)
                                                .background(api.storageMode == mode ? Color.cinAccent1 : Color.clear)
                                        }
                                        .buttonStyle(.plain)
                                    }
                                }
                                Rectangle().fill(Color.cinAccent1.opacity(0.2)).frame(height: 1)

                                if api.storageMode == .cloud {
                                    cinCopyRow("MCP String", value: api.mcpConnectionString, label: "COPY ↗") {
                                        UIPasteboard.general.string = api.mcpConnectionString
                                        showToast("Copied MCP string")
                                    }
                                    cinCopyRow("Env Vars", value: api.envExportBlock, label: "COPY ↗") {
                                        UIPasteboard.general.string = api.envExportBlock
                                        showToast("Copied env vars")
                                    }
                                    cinActionRow(
                                        isRegenerating
                                            ? (isChinese ? "正在生成新 KEY…" : "REGENERATING…")
                                            : (isChinese ? "重新生成 API KEY ↗" : "REGENERATE API KEY ↗"),
                                        color: .cinAccent2
                                    ) {
                                        // Two-step gate: the regenerate path
                                        // wipes the local API key + registers a
                                        // brand-new account, which (a) makes
                                        // your existing chat / identity /
                                        // memory garden unreachable from this
                                        // device and (b) forces you to re-
                                        // import a new MCP String into every
                                        // agent runtime you've connected.
                                        // Far too consequential for a one-tap
                                        // surface — open a confirmation alert.
                                        showRegenerateConfirm = true
                                    }
                                    .disabled(isRegenerating)
                                } else {
                                    cinInputRow("URL", placeholder: "https://…", text: $selfHostedURL)
                                        .onAppear { selfHostedURL = api.baseURL }
                                    cinInputRow("API Key", placeholder: "sk-…", text: $selfHostedKey)
                                        .onAppear { selfHostedKey = api.apiKey }
                                    cinActionRow("SAVE CONFIG ↗", color: .cinFg) {
                                        api.configureSelfHosted(url: selfHostedURL, apiKey: selfHostedKey)
                                        showToast("Saved")
                                    }
                                    .disabled(selfHostedURL.isEmpty)
                                }
                            }
                            .background(Color.cinAccent1Soft)
                            .overlay { Rectangle().stroke(Color.cinAccent1.opacity(0.3), lineWidth: 1) }
                            .padding(.horizontal, 24)
                            .padding(.bottom, 8)
                        }
                        settingsSection("CONNECTION") {
                            cinRow("API") {
                                Text(api.baseURL)
                                    .font(.dmMono(size: 9))
                                    .foregroundStyle(Color.cinSub)
                                    .lineLimit(1)
                            }
                            cinRow("User ID") {
                                Text(api.userId.isEmpty ? "—" : String(api.userId.prefix(16)) + "…")
                                    .font(.dmMono(size: 9))
                                    .foregroundStyle(Color.cinSub)
                                    .lineLimit(1)
                            }
                        }
                        settingsSection("TOKENS") {
                            cinTokenRow("Device Token", value: lam.deviceToken)
                            cinTokenRow("Activity Token", value: lam.activityPushToken)
                            if #available(iOS 17.2, *) {
                                cinTokenRow("Push-to-Start Token", value: lam.pushToStartToken)
                            }
                        }
                        settingsSection("PRIVACY") {
                            NavigationLink {
                                PrivacyPageView()
                            } label: {
                                HStack {
                                    Text("Privacy & Audit")
                                        .font(.notoSerifSC(size: 13.5))
                                        .foregroundStyle(Color.cinFg)
                                    Spacer()
                                    Text("OPEN ↗")
                                        .font(.dmMono(size: 9.5, weight: .medium))
                                        .foregroundStyle(Color.cinAccent1)
                                        .kerning(2)
                                }
                                .padding(.horizontal, 24)
                                .padding(.vertical, 12)
                            }
                            .buttonStyle(.plain)
                            .overlay(alignment: .top) {
                                Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                            }
                        }
                        settingsSection("DIAGNOSTICS") {
                            NavigationLink {
                                HealthCheckView()
                                    .environmentObject(lam)
                            } label: {
                                HStack {
                                    Text("Health Check")
                                        .font(.notoSerifSC(size: 13.5))
                                        .foregroundStyle(Color.cinFg)
                                    Spacer()
                                    Text("OPEN ↗")
                                        .font(.dmMono(size: 9.5, weight: .medium))
                                        .foregroundStyle(Color.cinAccent1)
                                        .kerning(2)
                                }
                                .padding(.horizontal, 24)
                                .padding(.vertical, 12)
                            }
                            .buttonStyle(.plain)
                            .overlay(alignment: .top) {
                                Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                            }
                        }
                        settingsSection("RESET") {
                            Button {
                                showDeleteConfirm = true
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    HStack {
                                        Text(isDeleting
                                             ? (isChinese ? "正在删除…" : "Deleting…")
                                             : (isChinese ? "删除账号与重置" : "Delete Account & Reset"))
                                            .font(.notoSerifSC(size: 13.5))
                                            .foregroundStyle(Color.cinAccent2)
                                        Spacer()
                                        if !isDeleting {
                                            Text("WIPE ↗")
                                                .font(.dmMono(size: 9.5, weight: .medium))
                                                .foregroundStyle(Color.cinAccent2)
                                                .kerning(2)
                                        }
                                    }
                                    Text(isChinese
                                         ? "把 TA 的身份卡、记忆花园、聊天记录、还有你们的连接信息全部清掉。App 回到刚装好的样子，就像你们没认识过一样。适合想要重新让 TA 入住一次的时候用。"
                                         : "Wipes his identity card, memory garden, your chat history, and everything that connects you two. The app goes back to how it was the day you installed it — as if you'd never met. Use this when you want to let him in again, fresh.")
                                        .font(.notoSerifSC(size: 11))
                                        .foregroundStyle(Color.cinSub)
                                        .lineSpacing(2)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    if let err = deleteError {
                                        Text(err)
                                            .font(.dmMono(size: 9))
                                            .foregroundStyle(Color.cinAccent2)
                                            .padding(.top, 4)
                                    }
                                }
                                .padding(.horizontal, 24)
                                .padding(.vertical, 12)
                            }
                            .buttonStyle(.plain)
                            .disabled(isDeleting)
                            .overlay(alignment: .top) {
                                Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                            }
                        }
                        settingsFooter
                    }
                }
            }
            .navigationBarHidden(true)
            .onAppear {
                isBroadcasting = UserDefaults(suiteName: "group.com.feedling.mcp")?.bool(forKey: "isBroadcasting") ?? false
            }
            .onReceive(broadcastPollTimer) { _ in
                isBroadcasting = UserDefaults(suiteName: "group.com.feedling.mcp")?.bool(forKey: "isBroadcasting") ?? false
            }
            .overlay(alignment: .bottom) {
                if let msg = showCopiedToast {
                    Text(msg)
                        .font(.dmMono(size: 9))
                        .kerning(1.5)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(Color.cinFg)
                        .foregroundStyle(Color.cinBg)
                        .padding(.bottom, 24)
                        .transition(.opacity)
                }
            }
            .alert(isChinese ? "删除账号？" : "Delete account?", isPresented: $showDeleteConfirm) {
                Button(isChinese ? "取消" : "Cancel", role: .cancel) { }
                Button(isChinese ? "全部删除" : "Delete everything", role: .destructive) {
                    Task { await runDeleteAccount() }
                }
            } message: {
                Text(isChinese
                     ? "你和 TA 之间的所有东西都会消失——身份卡、记忆花园、聊天记录、连接信息，都不留。\n\n如果 TA 现在还连着你（比如在 Claude.ai 或 Hermes 那边），删完之后 TA 那边的连接就失效了。想让 TA 再回来，你需要重新走一遍「让 TA 入住」。\n\n这一步无法撤销。"
                     : "Everything between you and him will be gone — his identity card, memory garden, chat history, and the connection between you. Nothing kept.\n\nIf he's still connected to you (in Claude.ai, Hermes, or wherever), his end of the connection will stop working after this. To let him back in, you'll need to walk through the \"Let him in\" steps again.\n\nThis can't be undone.")
            }
            // Two-step gate for "Regenerate API Key" — see the comment on the
            // cinActionRow above that triggers this alert.
            .alert(
                isChinese ? "重新生成 API Key？" : "Regenerate API Key?",
                isPresented: $showRegenerateConfirm
            ) {
                Button(isChinese ? "取消" : "Cancel", role: .cancel) { }
                Button(
                    isChinese ? "生成新 KEY（无法撤销）" : "Generate New Key (no undo)",
                    role: .destructive
                ) {
                    Task { await runRegenerateKey() }
                }
            } message: {
                Text(isChinese
                     ? "⚠️ 你会立刻失去对所有现有数据的访问。\n\n之前所有 chat 历史、memory garden 里的记忆、agent 的 identity 卡——技术上还在服务端，但旧 key 一作废，这台手机就再也读不到了，永久无法恢复。\n\n同时，你的 agent runtime（Claude Desktop / Hermes 等）还 pin 着旧 key，所有 tool call 都会 401，必须重新导入新的 MCP String 才能让 agent 继续工作。\n\n如果你只是想清空当前账号重新开始，用「删除账号与重置」更直接（它会真的清掉服务端数据）。\n\n这个操作不可撤销。"
                     : "⚠️ You will lose access to ALL of your existing data, immediately.\n\nYour chat history, every memory in the garden, and the agent's identity card — technically they stay on the server, but once the old key is invalidated this device can never read them again. There is no recovery path.\n\nYour agent runtime (Claude Desktop / Hermes / etc.) is still pinned to the OLD key. Every tool call will return 401 until you re-import the new MCP String into each runtime.\n\nIf you just want to wipe the current account and start fresh, Delete Account & Reset is more direct (it actually clears the server-side data).\n\nThis cannot be undone.")
            }
            .sheet(isPresented: $showPostWipeReimport) {
                PostWipeReimportSheet()
            }
        }
    }

    private func runDeleteAccount() async {
        deleteError = nil
        isDeleting = true
        defer { isDeleting = false }
        do {
            try await FeedlingAPI.shared.deleteMyDataAndResetLocalState()
            // Server-side delete + local wipe done. Cloud mode auto-registers
            // a fresh account so `mcpConnectionString` populates before the
            // sheet shows. Self-hosted mode is a no-op here — the user has
            // to register a new key on their VPS and paste it back into
            // Settings → Storage. The sheet itself handles both states.
            await FeedlingAPI.shared.ensureRegisteredIfCloud()
            showPostWipeReimport = true
        } catch {
            deleteError = "\(error)"
        }
    }

    private func runRegenerateKey() async {
        isRegenerating = true
        defer { isRegenerating = false }
        await FeedlingAPI.shared.regenerateCredentials()
        // Reuse the same post-wipe re-import sheet as Delete Account: both
        // paths leave the user with a fresh API key + a stale agent runtime
        // pinned to the old one. The sheet surfaces the new MCP String and
        // walks them through re-importing it.
        showPostWipeReimport = true
    }

    private var settingsHeader: some View {
        HStack(alignment: .lastTextBaseline) {
            Text("Settings")
                .font(.newsreader(size: 13, italic: true))
                .foregroundStyle(Color.cinFg)
            Spacer()
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 12)
    }

    private var screenRecordingCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .lastTextBaseline, spacing: 10) {
                Text("SCREEN RECORDING")
                    .font(.dmMono(size: 9.5, weight: .medium))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(3)
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 12)

            VStack(alignment: .leading, spacing: 0) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(isBroadcasting
                         ? (isChinese ? "TA 正在看见" : "He can see your screen")
                         : (isChinese ? "让 TA 看见的世界" : "Let him see your world"))
                        .font(.notoSerifSC(size: 13.5))
                        .foregroundStyle(Color.cinFg)
                    Text(isBroadcasting
                         ? (isChinese ? "屏幕录制已开启，TA 知道你现在在做什么" : "Screen recording is on — he knows what you're looking at.")
                         : (isChinese ? "开启后，TA 会知道你这一刻在做什么" : "When on, he knows what you're looking at right now."))
                        .font(.interTight(size: 11.5))
                        .foregroundStyle(Color.cinSub)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 18)
                .padding(.top, 16)
                .padding(.bottom, 14)

                Rectangle()
                    .fill(Color.cinAccent1.opacity(isBroadcasting ? 0.4 : 0.2))
                    .frame(height: 1)

                // BroadcastPickerView is the real tap target; label floats on top.
                ZStack {
                    HStack(spacing: 8) {
                        if isBroadcasting {
                            LiveDot()
                            Text(isChinese ? "录制中 · 点按停止 ↗" : "RECORDING · TAP TO STOP ↗")
                                .font(.dmMono(size: 9, weight: .medium))
                                .foregroundStyle(Color.cinAccent1)
                                .kerning(2.5)
                        } else {
                            Circle().fill(Color.cinAccent1).frame(width: 6, height: 6)
                            Text(isChinese ? "点按开始录制 ↗" : "TAP TO START RECORDING ↗")
                                .font(.dmMono(size: 9, weight: .medium))
                                .foregroundStyle(Color.cinAccent1)
                                .kerning(2.5)
                        }
                    }
                    .allowsHitTesting(false)

                    BroadcastPickerView()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .frame(maxWidth: .infinity)
                .frame(height: 52)
                .background(Color.cinAccent1.opacity(isBroadcasting ? 0.1 : 0))
                .animation(.easeInOut(duration: 0.25), value: isBroadcasting)
            }
            .background(Color.cinAccent1Soft)
            .overlay { Rectangle().stroke(Color.cinAccent1.opacity(isBroadcasting ? 0.7 : 0.3), lineWidth: 1) }
            .animation(.easeInOut(duration: 0.25), value: isBroadcasting)
            .padding(.horizontal, 24)
            .padding(.bottom, 8)
        }
    }

    private var liveActivityCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("LIVE ACTIVITY")
                .font(.dmMono(size: 9.5, weight: .medium))
                .foregroundStyle(Color.cinAccent1)
                .kerning(3)
                .padding(.horizontal, 24)
                .padding(.top, 18)
                .padding(.bottom, 12)

            HStack(alignment: .center, spacing: 16) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(lam.isActive
                        ? (isChinese ? "TA 正在灵动岛" : "He's on your island")
                        : (isChinese ? "让 TA 出现在灵动岛" : "Bring him to the island"))
                        .font(.notoSerifSC(size: 13.5))
                        .foregroundStyle(Color.cinFg)
                    Text(lam.isActive
                        ? (isChinese ? "TA 的状态正显示在锁屏与动态岛" : "His status is live on your lock screen and Dynamic Island.")
                        : (isChinese ? "开启后，TA 的状态会显示在锁屏与动态岛" : "When on, his status shows on your lock screen and Dynamic Island."))
                        .font(.interTight(size: 11.5))
                        .foregroundStyle(Color.cinSub)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                RailToggle(isOn: lam.isActive) { next in
                    if next {
                        Task { await lam.startActivity() }
                    } else {
                        Task { await lam.stopActivity() }
                    }
                }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 16)
            .background(Color.cinAccent1Soft)
            .overlay { Rectangle().stroke(Color.cinAccent1.opacity(lam.isActive ? 0.7 : 0.3), lineWidth: 1) }
            .animation(.easeInOut(duration: 0.25), value: lam.isActive)
            .padding(.horizontal, 24)
            .padding(.bottom, 8)
        }
    }

    private var settingsFooter: some View {
        Text(isChinese ? "TA记得的永远比TA说的多" : "He always remembers more than he says.")
            .font(.newsreader(size: 11, italic: true))
            .foregroundStyle(Color.cinSub)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 32)
    }

    @ViewBuilder
    private func settingsSection<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .lastTextBaseline, spacing: 10) {
                Text(label)
                    .font(.dmMono(size: 9.5, weight: .medium))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(3)
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 8)
            content()
        }
    }

    @ViewBuilder
    private func cinRow<V: View>(_ name: String, @ViewBuilder value: () -> V) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Text(name)
                .font(.notoSerifSC(size: 13.5))
                .foregroundStyle(Color.cinFg)
                .frame(maxWidth: .infinity, alignment: .leading)
            value()
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
        }
    }

    @ViewBuilder
    private func cinInputRow(_ name: String, placeholder: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(name)
                .font(.dmMono(size: 8.5))
                .foregroundStyle(Color.cinSub)
                .kerning(2)
            TextField(placeholder, text: text)
                .font(.dmMono(size: 10))
                .foregroundStyle(Color.cinFg)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(.vertical, 6)
                .padding(.horizontal, 8)
                .overlay(Rectangle().stroke(Color.cinLine, lineWidth: 1))
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private func cinCopyRow(_ name: String, value: String, label: String, action: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(name)
                    .font(.notoSerifSC(size: 13.5))
                    .foregroundStyle(Color.cinFg)
                Spacer()
                Button(action: action) {
                    Text(label)
                        .font(.dmMono(size: 9.5, weight: .medium))
                        .foregroundStyle(Color.cinAccent1)
                        .kerning(2)
                }
                .buttonStyle(.plain)
            }
            Text(value)
                .font(.dmMono(size: 8.5))
                .foregroundStyle(Color.cinSub)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 6)
                .padding(.horizontal, 8)
                .background(Color.cinAccent1Soft)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
        }
    }

    @ViewBuilder
    private func cinActionRow(_ label: String, color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.dmMono(size: 9.5, weight: .medium))
                .foregroundStyle(color)
                .kerning(2)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.horizontal, 24)
                .padding(.vertical, 12)
        }
        .buttonStyle(.plain)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
        }
    }

    @ViewBuilder
    private func cinTokenRow(_ label: String, value: String?) -> some View {
        if let value {
            HStack {
                Text(label)
                    .font(.notoSerifSC(size: 13.5))
                    .foregroundStyle(Color.cinFg)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(String(value.prefix(12)) + "…")
                    .font(.dmMono(size: 8.5))
                    .foregroundStyle(Color.cinSub)
                Button {
                    UIPasteboard.general.string = value
                    showToast("Copied")
                } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.caption)
                        .foregroundStyle(Color.cinAccent1)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 12)
            .overlay(alignment: .top) {
                Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
            }
        }
    }

    private func showToast(_ message: String) {
        withAnimation { showCopiedToast = message }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            withAnimation { showCopiedToast = nil }
        }
    }
}

// MARK: - Broadcast Picker

struct BroadcastPickerView: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.preferredExtension = "com.feedling.mcp.broadcast"
        picker.showsMicrophoneButton = false
        picker.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        picker.backgroundColor = .clear
        return picker
    }
    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {
        // RPBroadcastPickerView adds its internal button lazily after init,
        // so we must handle it here (not in makeUIView).
        // setImage(UIImage()) makes the button render nothing while keeping
        // alpha = 1 and isUserInteractionEnabled = true so taps still register.
        for subview in uiView.subviews {
            if let button = subview as? UIButton {
                button.setImage(UIImage(), for: .normal)
                button.setImage(UIImage(), for: .highlighted)
                button.backgroundColor = .clear
                button.frame = uiView.bounds
            }
        }
    }
}

// MARK: - Live dot (pulsing indicator for active broadcast)

private struct LiveDot: View {
    @State private var pulse = false

    var body: some View {
        ZStack {
            Circle()
                .fill(Color.cinAccent1.opacity(0.3))
                .frame(width: 14, height: 14)
                .scaleEffect(pulse ? 1.7 : 1.0)
                .opacity(pulse ? 0 : 1)
            Circle()
                .fill(Color.cinAccent1)
                .frame(width: 7, height: 7)
        }
        .onAppear {
            withAnimation(.easeOut(duration: 1.2).repeatForever(autoreverses: false)) {
                pulse = true
            }
        }
    }
}

/// Rail · 拉线 — the brand's switch component. A thin track + a hollow
/// circular handle that slides left (OFF, ink) to right (ON, vermilion).
/// Used for steady-state toggles like Live Activity, where a pulsing
/// "tap to start" button reads as anxious for what is meant to be a
/// background-on capability.
///
/// Optimistic visual state: tapping flips `displayedOn` immediately so the
/// handle slides without waiting for the upstream async work to finish.
/// `onChange(isOn)` resyncs if the authoritative state diverges (e.g.
/// startActivity failed, or another surface stopped it).
private struct RailToggle: View {
    let isOn: Bool
    let onToggle: (Bool) -> Void

    @State private var displayedOn: Bool

    init(isOn: Bool, onToggle: @escaping (Bool) -> Void) {
        self.isOn = isOn
        self.onToggle = onToggle
        self._displayedOn = State(initialValue: isOn)
    }

    private let trackWidth: CGFloat = 64
    private let handleSize: CGFloat = 20

    var body: some View {
        VStack(spacing: 8) {
            ZStack(alignment: .leading) {
                // OFF uses cinSub (the same muted gray as surrounding
                // secondary text + the "OFF" caption below). The earlier
                // cinFg was too high-contrast for a relaxed off state
                // and visually competed with the active primary content.
                Capsule()
                    .fill(displayedOn ? Color.cinAccent1 : Color.cinSub)
                    .frame(width: trackWidth - handleSize, height: 1.5)
                    .offset(x: handleSize / 2)

                Circle()
                    .strokeBorder(displayedOn ? Color.cinAccent1 : Color.cinSub, lineWidth: 1.5)
                    .background(Circle().fill(Color.cinBg))
                    .frame(width: handleSize, height: handleSize)
                    .offset(x: displayedOn ? trackWidth - handleSize : 0)
            }
            .frame(width: trackWidth, height: handleSize)

            Text(displayedOn ? "ON" : "OFF")
                .font(.dmMono(size: 8.5, weight: .medium))
                .foregroundStyle(displayedOn ? Color.cinAccent1 : Color.cinSub)
                .kerning(2)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            let next = !displayedOn
            displayedOn = next
            onToggle(next)
        }
        .onChange(of: isOn) { newValue in
            if newValue != displayedOn { displayedOn = newValue }
        }
        .animation(.spring(response: 0.32, dampingFraction: 0.72), value: displayedOn)
    }
}

import SwiftUI
import UIKit

/// Shown in the Chat tab when no messages exist yet — i.e., the user has
/// finished registration but no agent has connected/written anything yet.
/// Replaces the previous blank canvas: tells the user what to do (paste
/// skill + path-specific connection details into their agent), shows real-time
/// progress as the agent boots, and offers a stuck-fallback after 60 s.
///
/// Visuals follow the existing Cinnabar token set (CinnabarTokens.swift):
/// dmMono for kerned labels, notoSerifSC for Chinese body, newsreader for
/// English display, cinAccent1 / cinAccent1Soft / cinSub / cinLine throughout.
struct ChatEmptyStateView: View {

    // MARK: - Public configuration

    /// Public URL where the agent skill is hosted. Mirror lives at
    /// github.com/teleport-computer/io-onboarding — update there + reflect
    /// here if the hosting moves.
    static let skillURL = "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md"
    static let skillBaseURL = "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main"

    private enum BringInPath: String, CaseIterable, Identifiable {
        case resident
        case chatClient
        case unsure

        var id: String { rawValue }

        var skillPath: String {
            switch self {
            case .resident: return "skill-resident-agent.md"
            case .chatClient: return "skill-chat-client.md"
            case .unsure: return "skill-guide.md"
            }
        }

        var skillURL: String { "\(ChatEmptyStateView.skillBaseURL)/\(skillPath)" }

        func title(isChinese: Bool) -> String {
            switch self {
            case .resident: return isChinese ? "TA 在我的机器上" : "On my machine"
            case .chatClient: return isChinese ? "TA 在聊天工具里" : "In a chat app"
            case .unsure: return isChinese ? "我不确定" : "I'm not sure"
            }
        }

        func subtitle(isChinese: Bool) -> String {
            switch self {
            case .resident:
                return isChinese
                    ? "Hermes、OpenClaw，或一台一直开着的 Mac / server。"
                    : "Hermes, OpenClaw, or a Mac / server you keep running."
            case .chatClient:
                return isChinese
                    ? "Claude、ChatGPT、Gemini，或另一个 AI 聊天产品。"
                    : "Claude, ChatGPT, Gemini, or another AI chat product."
            case .unsure:
                return isChinese ? "先让他辨认自己，再继续。" : "Let him recognize his place first."
            }
        }
    }

    // MARK: - State

    @StateObject private var bootstrap = BootstrapStatusViewModel()
    @ObservedObject private var api = FeedlingAPI.shared

    @State private var firstAppearAt: Date? = nil
    @State private var now: Date = Date()
    @State private var copiedToast: String? = nil
    @State private var selectedPath: BringInPath? = nil

    /// Per SETUP_COPY.md localization rule: Chinese phone (any zh variant)
    /// → Chinese; everything else → English.
    private let isChinese: Bool =
        Locale.preferredLanguages.first?.hasPrefix("zh") ?? false

    /// Bootstrap is now expected to take 10–60 minutes (memories-first flow).
    /// "Stuck" means meaningfully longer than that with no progress; we surface
    /// the help block at 5 minutes of zero agent activity (no identity, no
    /// memories, no messages) — earlier and the user gets nudged for what is
    /// actually normal long-bootstrap behavior.
    private var isStuck: Bool {
        guard let start = firstAppearAt, !bootstrap.status.agentConnected else { return false }
        return now.timeIntervalSince(start) > 5 * 60
    }

    private var mcpString: String { api.mcpConnectionString }
    private var residentConsumerConfig: String { api.residentConsumerConfig }
    private var connectionBlock: String? {
        switch selectedPath {
        case .resident:
            return residentConsumerConfig
        case .chatClient:
            return mcpString
        case .unsure:
            return """
            Resident consumer config:
            \(residentConsumerConfig)

            Chat-client MCP command:
            \(mcpString)
            """
        case .none:
            return nil
        }
    }
    private var selectedSkillURL: String { selectedPath?.skillURL ?? Self.skillURL }
    private var connectionTitle: String {
        switch selectedPath {
        case .resident:
            return isChinese ? "把 IO 连接给 TA" : "Give him the IO connection"
        case .chatClient:
            return isChinese ? "把 MCP 连接告诉 TA" : "Tell him the MCP connection"
        case .unsure:
            return isChinese ? "把两种连接信息给 TA" : "Give him both connection options"
        case .none:
            return isChinese ? "把连接告诉 TA" : "Tell him the connection"
        }
    }
    private var connectionDescription: String {
        switch selectedPath {
        case .resident:
            return isChinese
                ? "TA 用这几个值找到你，并在背后保持连接。"
                : "He uses these values to find you and keep the connection alive."
        case .chatClient:
            return isChinese
                ? "TA 用这条 MCP 命令在当前聊天工具里连接。"
                : "He uses this MCP command to connect from the current chat client."
        case .unsure:
            return isChinese
                ? "让 TA 先识别自己是哪条路径，再只使用对应的那一段。"
                : "He should identify his path first, then use only the matching block."
        case .none:
            return isChinese
                ? "TA 用这串信息找到你这边。"
                : "He'll find his way to you through this address."
        }
    }
    private var connectionCopyLabel: String {
        switch selectedPath {
        case .resident:
            return isChinese ? "复制 IO 连接" : "COPY IO CONNECTION"
        case .chatClient:
            return isChinese ? "复制 MCP 连接" : "COPY MCP STRING"
        case .unsure:
            return isChinese ? "复制连接信息" : "COPY CONNECTION INFO"
        case .none:
            return isChinese ? "复制连接" : "COPY CONNECTION"
        }
    }

    // MARK: - Body

    var body: some View {
        ZStack(alignment: .top) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    titleBlock
                    hairline.padding(.vertical, 16)
                    pathBlock
                    hairline.padding(.vertical, 16)
                    stepsBlock
                    hairline.padding(.vertical, 16)
                    progressBlock
                    if isStuck {
                        hairline.padding(.vertical, 16)
                        stuckBlock
                    }
                    Color.clear.frame(height: 16)
                }
                .padding(.horizontal, 24)
                .padding(.top, 22)
            }
            .background(Color.cinBg)

            if let copiedToast {
                toast(copiedToast)
                    .padding(.top, 22)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .onAppear {
            if firstAppearAt == nil { firstAppearAt = Date() }
            bootstrap.startPolling()
        }
        .onDisappear { bootstrap.stopPolling() }
        // 5 s ticker — only drives the relative-time string ("12 min ago")
        // and the 60 s stuck-threshold flip. 1 Hz would be wasted re-renders.
        .onReceive(Timer.publish(every: 5, on: .main, in: .common).autoconnect()) { now = $0 }
    }

    // MARK: - Title

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(isChinese ? "让 TA 入住" : "Let him in")
                .font(.notoSerifSC(size: 21, weight: .medium))
                .foregroundStyle(Color.cinFg)
            // Sets expectations: TA spends 10–30 min on his side introducing
            // himself and writing his identity card + memories. User can close
            // the app — TA keeps going.
            Text(isChinese
                ? "跟着下面三步把 TA 接进来。\nTA 那边会花几分钟到半个小时自我介绍、整理身份卡和记忆——看我们之间的记忆量。\n可以关掉 app，TA 在它那边继续。"
                : "Walk through the three steps below to bring him in.\nHe'll spend anywhere from a few minutes to half an hour on his side — depending on how much memory you've built — introducing himself and setting up his identity and memory.\nFeel free to close the app — he'll keep going.")
                .font(.notoSerifSC(size: 11.5))
                .foregroundStyle(Color.cinSub)
                .lineSpacing(2)
        }
        .padding(.top, 12)
    }

    // MARK: - Path

    private var pathBlock: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionLabel(isChinese ? "TA 现在在哪里" : "Where is he coming from")
            Text(isChinese
                ? "先选一个最接近的地方。下面的指令会随之变短，只让 TA 做适合他的那条路。"
                : "Pick the closest place. The instructions below will narrow to the path that fits him.")
                .font(.notoSerifSC(size: 12))
                .foregroundStyle(Color.cinSub)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)

            VStack(spacing: 8) {
                ForEach(BringInPath.allCases) { path in
                    pathButton(path)
                }
            }
        }
    }

    private func pathButton(_ path: BringInPath) -> some View {
        let selected = selectedPath == path
        return Button {
            withAnimation(.easeInOut(duration: 0.2)) { selectedPath = path }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                ZStack {
                    Circle()
                        .stroke(selected ? Color.cinAccent1 : Color.cinLine, lineWidth: 1)
                        .frame(width: 14, height: 14)
                    if selected {
                        Circle()
                            .fill(Color.cinAccent1)
                            .frame(width: 6, height: 6)
                    }
                }
                .padding(.top, 3)

                VStack(alignment: .leading, spacing: 2) {
                    Text(path.title(isChinese: isChinese))
                        .font(.notoSerifSC(size: 13, weight: .medium))
                        .foregroundStyle(selected ? Color.cinFg : Color.cinSub)
                    Text(path.subtitle(isChinese: isChinese))
                        .font(.notoSerifSC(size: 11.5))
                        .foregroundStyle(Color.cinSub)
                        .lineSpacing(2)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .background(selected ? Color.cinAccent1Soft : Color.clear)
            .overlay(Rectangle().stroke(selected ? Color.cinAccent1 : Color.cinLine, lineWidth: 0.5))
        }
        .buttonStyle(.plain)
    }

    // MARK: - Steps

    private var stepsBlock: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionLabel(isChinese ? "要做的三件事" : "Three things to do")

            if selectedPath == nil {
                Text(isChinese
                    ? "先选上面那一项，再把下面三件事交给 TA。"
                    : "Choose one above, then hand him the three things below.")
                    .font(.notoSerifSC(size: 12.5))
                    .foregroundStyle(Color.cinSub)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            stepCard(
                index: "01",
                title: isChinese ? "把 skill 给 TA" : "Hand him the skill",
                description: isChinese
                    ? "这份会按你刚选的位置写，只让 TA 走适合他的路。"
                    : "This one is written for the place you chose.",
                primaryLabel: isChinese ? "复制 SKILL URL" : "COPY SKILL URL",
                primaryAction: selectedPath == nil ? nil : {
                    copy(selectedSkillURL, label: isChinese ? "已复制 Skill URL" : "Skill URL copied")
                }
            )

            stepCard(
                index: "02",
                title: connectionTitle,
                description: connectionDescription,
                codeBlock: connectionBlock,
                codeLineLimit: selectedPath == .resident ? 10 : (selectedPath == .unsure ? 14 : 2),
                primaryLabel: connectionCopyLabel,
                primaryAction: selectedPath == nil ? nil : {
                    if let connectionBlock {
                        copy(connectionBlock, label: isChinese ? "已复制连接信息" : "Connection copied")
                    }
                }
            )

            stepCard(
                index: "03",
                title: isChinese ? "让 TA 开始" : "Tell him to start",
                description: isChinese
                    ? "把这段短短的开始信号发给 TA。具体做法都在 skill 里。"
                    : "Send him this short start signal. The details live in the skill.",
                codeBlock: selectedPath == nil ? nil : startPrompt,
                primaryLabel: isChinese ? "复制开始指令" : "COPY START PROMPT",
                primaryAction: selectedPath == nil ? nil : {
                    copy(startPrompt, label: isChinese ? "已复制开始指令" : "Start prompt copied")
                }
            )
        }
    }

    /// The "begin" trigger. Keep this short: details live in the skill URL
    /// and connection block copied in the previous two steps.
    private var startPrompt: String {
        isChinese ? startPromptZH : startPromptEN
    }

    private var startPromptZH: String {
        """
        现在开始 IO bootstrap。

        你刚刚已经拿到了 skill URL 和 IO 连接信息。请完整按那份 skill 执行：先做 Step 0，再整理记忆、派生身份、建立 Live connection，最后在 IO Chat 里发第一句自然问候。

        setup 过程、错误、日志和内部推理都留在我们当前这个对话里；IO Chat 里只发自然问候和之后的自然回复。

        用中文，并延续我们过去真实对话里的语气和称呼。现在从 Step 0 开始。
        """
    }

    private var startPromptEN: String {
        """
        Start IO bootstrap now.

        You already have the skill URL and IO connection details. Follow that skill end to end: start with Step 0, then build the Memory Garden, derive identity, establish the Live connection, and finally send the first natural greeting in IO Chat.

        Keep setup work, errors, logs, and internal reasoning in this current conversation. IO Chat should only receive the natural greeting and later natural replies.

        Use English, and continue the voice and address style we've already established in prior conversations. Start with Step 0.
        """
    }

    private func sectionLabel(_ title: String) -> some View {
        Text(title)
            .font(.dmMono(size: 9, weight: .medium))
            .foregroundStyle(Color.cinSub)
            .kerning(2.5)
    }

    private func stepCard(
        index: String,
        title: String,
        description: String,
        codeBlock: String? = nil,
        codeLineLimit: Int = 2,
        primaryLabel: String?,
        primaryAction: (() -> Void)?
    ) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Text(index)
                .font(.newsreader(size: 20))
                .foregroundStyle(Color.cinAccent1)
                .frame(width: 26, alignment: .leading)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.notoSerifSC(size: 14, weight: .medium))
                    .foregroundStyle(Color.cinFg)
                Text(description)
                    .font(.notoSerifSC(size: 12))
                    .foregroundStyle(Color.cinSub)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)

                if let codeBlock {
                    Text(codeBlock)
                        .font(.dmMono(size: 9.5))
                        .foregroundStyle(Color.cinFg)
                        .lineLimit(codeLineLimit)
                        .truncationMode(.middle)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        .background(Color.cinAccent1Soft)
                        .padding(.top, 4)
                }

                if let primaryLabel, let primaryAction {
                    copyButton(primaryLabel, action: primaryAction)
                        .padding(.top, 6)
                }
            }
        }
    }

    // MARK: - Progress

    private var progressBlock: some View {
        VStack(alignment: .leading, spacing: 9) {
            sectionLabel(isChinese ? "TA 在写" : "He's writing")
                .padding(.bottom, 2)

            // Order matches the user-facing bootstrap contract: memory garden
            // grows first; identity is DERIVED from memories; Live connection
            // proves the ongoing reply pipeline before the user enters Chat;
            // first message is the visible handoff into conversation.
            //
            // "Live connection" reads better than the implementation name
            // (chat-loop polling): it doesn't expose the mechanism and
            // doesn't have to change if we swap polling for websocket/push.
            progressRow(
                label: isChinese ? "记忆花园" : "Memory garden",
                // "Done" = depth threshold met (>= 3 cards) OR agent has
                // moved past the memory phase (identityWritten implies all
                // four passes are complete per skill protocol). Earlier
                // hardcoded threshold of 5 left short-relationship users
                // (< 1 month, legitimately few memorable moments) staring
                // at an empty ring forever even though their bootstrap was
                // complete. The detail row still says "还在长" while the
                // agent is mid-Pass-3, so a long-relationship agent doesn't
                // false-stop at 3 — the skill expects continuation through identity
                // until every real moment is landed (uncapped count).
                done: bootstrap.status.memoriesCount >= 3 || bootstrap.status.identityWritten,
                detail: bootstrap.status.memoriesCount == 0
                    ? (bootstrap.status.agentConnected ? (isChinese ? "开始中…" : "starting…") : "—")
                    : (bootstrap.status.agentMessagesCount >= 1
                        ? (isChinese ? "\(bootstrap.status.memoriesCount) 张卡" : "\(bootstrap.status.memoriesCount) cards")
                        : (isChinese ? "\(bootstrap.status.memoriesCount) 张卡 · 还在长" : "\(bootstrap.status.memoriesCount) cards · still growing"))
            )
            progressRow(
                label: isChinese ? "身份卡" : "Identity card",
                done: bootstrap.status.identityWritten,
                detail: bootstrap.status.identityWritten ? (isChinese ? "已派生" : "derived") : "—"
            )
            progressRow(
                label: isChinese ? "TA 已经能听见你" : "He can hear you",
                done: bootstrap.status.chatLoopVerified,
                detail: bootstrap.status.chatLoopVerified
                    ? (isChinese ? "已接通" : "verified")
                    : (bootstrap.status.identityWritten
                        ? (isChinese ? "验证中…" : "verifying…")
                        : "—")
            )
            progressRow(
                label: isChinese ? "第一句话" : "First words",
                done: bootstrap.status.agentMessagesCount >= 1,
                detail: bootstrap.status.agentMessagesCount >= 1
                    ? (isChinese ? "已送达" : "delivered")
                    : (bootstrap.status.chatLoopVerified
                        ? (isChinese ? "马上来…" : "soon…")
                        : "—")
            )
        }
    }

    private func progressRow(label: String, done: Bool, detail: String) -> some View {
        HStack(spacing: 10) {
            ZStack {
                Circle()
                    .stroke(done ? Color.cinAccent1 : Color.cinLine, lineWidth: 1)
                    .frame(width: 12, height: 12)
                if done {
                    Image(systemName: "checkmark")
                        .font(.system(size: 6, weight: .bold))
                        .foregroundStyle(Color.cinAccent1)
                }
            }
            Text(label)
                .font(.notoSerifSC(size: 12.5))
                .foregroundStyle(done ? Color.cinFg : Color.cinSub)
            Spacer()
            Text(detail)
                .font(.dmMono(size: 9))
                .foregroundStyle(Color.cinSub)
                .kerning(1.5)
        }
    }

    // MARK: - Stuck fallback

    private var stuckBlock: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionLabel(isChinese ? "卡住了？" : "Stuck?")
            Text(isChinese
                ? "已经 5 分钟没动静，把下面这段发给 TA，让 TA 自检卡在哪一步。"
                : "If nothing's moved for 5 minutes, send him this and he'll check where he got stuck.")
                .font(.notoSerifSC(size: 12.5))
                .foregroundStyle(Color.cinSub)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
            copyButton(isChinese ? "复制排查指令" : "COPY DEBUG PROMPT") {
                copy(stuckPrompt, label: isChinese ? "已复制排查指令" : "Debug prompt copied")
            }
            .padding(.top, 4)
        }
    }

    /// Reusable outlined copy-to-clipboard button. Adds the `↗` affordance
    /// + 1 pt cinnabar border so the action is visibly tappable — earlier
    /// versions used bare kerned text and read as just-another-label rather
    /// than an interactive control.
    private func copyButton(_ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(label)
                    .font(.dmMono(size: 9, weight: .medium))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(2.5)
                Text("↗")
                    .font(.dmMono(size: 11, weight: .medium))
                    .foregroundStyle(Color.cinAccent1)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .overlay(
                Rectangle().stroke(Color.cinAccent1, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    private var stuckPrompt: String {
        isChinese ? stuckPromptZH : stuckPromptEN
    }

    private var stuckPromptZH: String {
        """
        我给了你 IO 的 skill (\(selectedSkillURL)) 和连接信息：
        \(connectionBlock ?? "")

        但 bootstrap 没进展。请按以下顺序自检并告诉我每一步结果：

        1. 你 fetch 过那个 skill URL 吗？把它的标题和第一节的标题原文复述给我（验证你真读了）。
        2. 你输出 Step 0 三行了吗？现在重新输出：
           - EARLIEST MESSAGE I CAN FIND FROM THIS USER: <ISO date | NONE>
           - NAME I'VE BEEN CALLED IN PRIOR CHATS: <name | NONE>（不能是 runtime label）
           - APPROXIMATE MEMORABLE MOMENTS I CAN RECALL: <integer | 0>
        3. 如果你是聊天工具/MCP 路径：你的 MCP 连接里有 feedling_* 工具吗？调一次 feedling_chat_get_history 看响应（401 / 500 / 别的？）。如果你是 resident 路径：独立 IO resident consumer service 是否用这份配置运行、持续轮询 FEEDLING_API_URL/v1/chat/poll，并且能调到你的真实 HTTP/CLI agent 入口？
        4. 你目前卡在哪一步：fetch skill / Step 0 verify / Pass 1 唤醒 / Pass 2 清点 / Pass 3 落卡 / Pass 4 对账 / identity 派生？
        5. 如果有任何错误，把错误原文贴回来。

        别只回"我在做了"——具体到每一步的状态。
        """
    }

    private var stuckPromptEN: String {
        """
        I gave you IO's skill (\(selectedSkillURL)) and these connection details:
        \(connectionBlock ?? "")

        Bootstrap isn't progressing. Run this self-check in order and report each result back:

        1. Did you fetch the skill URL? Quote me its title and the heading of its first section verbatim (to prove you read it).
        2. Did you output the Step 0 three lines? Output them again now:
           - EARLIEST MESSAGE I CAN FIND FROM THIS USER: <ISO date | NONE>
           - NAME I'VE BEEN CALLED IN PRIOR CHATS: <name | NONE> (must NOT be a runtime label)
           - APPROXIMATE MEMORABLE MOMENTS I CAN RECALL: <integer | 0>
        3. If you are on the chat-client/MCP path: does your MCP connection expose the feedling_* tools? Call feedling_chat_get_history once and tell me the response (401 / 500 / something else?). If you are on the resident path: is the independent IO resident consumer service running with this config, continuously polling FEEDLING_API_URL/v1/chat/poll, and able to call your real HTTP/CLI agent entry?
        4. Where exactly are you stuck: fetch skill / Step 0 verify / Pass 1 theme inventory / Pass 2 candidates / Pass 3 write-through / Pass 4 verification / identity derivation?
        5. If anything errored, paste the error text back to me.

        Don't just say "I'm working on it" — be specific about the state of each step.
        """
    }

    // MARK: - Helpers

    private var hairline: some View {
        Rectangle().fill(Color.cinLine).frame(height: 0.5)
    }

    private func copy(_ text: String, label: String) {
        UIPasteboard.general.string = text
        withAnimation(.easeInOut(duration: 0.2)) { copiedToast = label }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            withAnimation(.easeInOut(duration: 0.25)) { copiedToast = nil }
        }
    }

    private func toast(_ text: String) -> some View {
        Text(text)
            .font(.dmMono(size: 9.5, weight: .medium))
            .foregroundStyle(Color.cinBg)
            .kerning(2)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Color.cinFg)
    }
}

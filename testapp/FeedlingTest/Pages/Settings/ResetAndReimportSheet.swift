import SwiftUI

// MARK: - Reset & re-import sheet (3-step pipeline)

struct ResetAndReimportSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var step: Int = 0
    @State private var error: String? = nil
    @State private var exportData: FeedlingAPI.ExportResult? = nil

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            VStack(spacing: 0) {
                HStack {
                    Button(action: { dismiss() }) {
                        Text("✕")
                            .font(.dmMono(size: 12))
                            .foregroundStyle(Color.cinFg)
                    }
                    .buttonStyle(.plain)
                    Spacer()
                    Text("RESET")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Reset & re-import")
                            .font(.newsreader(size: 26))
                            .foregroundStyle(Color.cinFg)
                            .padding(.horizontal, 24)
                            .padding(.top, 24)
                            .padding(.bottom, 14)
                        Text("Download your data, delete your old account, register a new one. Fresh keys — use the connection details to walk your agent through re-importing.")
                            .font(.notoSerifSC(size: 13))
                            .foregroundStyle(Color.cinSub)
                            .lineSpacing(4)
                            .padding(.horizontal, 24)
                            .padding(.bottom, 24)
                        // Step indicators
                        HStack(spacing: 0) {
                            stepItem(1, label: "Export", active: step >= 1)
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).frame(maxWidth: .infinity)
                            stepItem(2, label: "Delete", active: step >= 2)
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).frame(maxWidth: .infinity)
                            stepItem(3, label: "Register", active: step >= 3)
                        }
                        .padding(.horizontal, 24)
                        .padding(.bottom, 24)
                        if let err = error {
                            Text(err)
                                .font(.dmMono(size: 9))
                                .foregroundStyle(Color.cinAccent2)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 12)
                        }
                        Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        Button { Task { await runPipeline() } } label: {
                            Text(step >= 3 ? "DONE ↗" : "START ↗")
                        }
                        .buttonStyle(CinPrimaryButtonStyle())
                        .padding(.horizontal, 24)
                        .padding(.top, 20)
                    }
                }
            }
        }
    }

    private func stepItem(_ n: Int, label: String, active: Bool) -> some View {
        VStack(spacing: 4) {
            ZStack {
                Circle()
                    .fill(active ? Color.cinAccent1 : Color.cinLine)
                    .frame(width: 22, height: 22)
                Text("\(n)")
                    .font(.dmMono(size: 9, weight: .medium))
                    .foregroundStyle(active ? Color.white : Color.cinSub)
            }
            Text(label)
                .font(.dmMono(size: 8))
                .foregroundStyle(active ? Color.cinAccent1 : Color.cinSub)
                .kerning(1)
        }
    }

    private func runPipeline() async {
        do {
            if step == 0 {
                step = 1
                exportData = try await FeedlingAPI.shared.exportMyData()
            }
            if step == 1 {
                step = 2
                try await FeedlingAPI.shared.deleteMyDataAndResetLocalState()
            }
            if step == 2 {
                step = 3
                await FeedlingAPI.shared.ensureRegisteredIfCloud()
                await FeedlingAPI.shared.ensureUserIdIfNeeded()
            }
            if step == 3 {
                dismiss()
            }
        } catch {
            self.error = "Pipeline failed at step \(step): \(error)"
        }
    }
}

// MARK: - Post-WIPE re-import sheet
//
// Shown after Delete Account & Reset. In cloud mode `ensureRegisteredIfCloud`
// has already minted a fresh API key by the time this sheet appears — happy
// path renders fresh reconnection details + COPY. In self-hosted mode (or
// when cloud registration silently failed), apiKey is still empty — the
// sheet renders a guidance state pointing the user at Settings → Storage
// to register / paste a new key manually.

struct PostWipeReimportSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var api = FeedlingAPI.shared
    @State private var copied: Bool = false

    private let isChinese: Bool =
        Locale.preferredLanguages.first?.hasPrefix("zh") ?? false

    private var hasFreshKey: Bool { !api.apiKey.isEmpty }
    private var reimportBlock: String {
        api.connectionDetailsBlock
    }

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            VStack(spacing: 0) {
                HStack {
                    Spacer()
                    Text("RESET COMPLETE")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                    Spacer()
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)

                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text(hasFreshKey
                             ? (isChinese ? "旧的 key 已经失效。" : "Your old key is dead.")
                             : (isChinese ? "重置完成。" : "Reset complete."))
                            .font(.newsreader(size: 26))
                            .foregroundStyle(Color.cinFg)
                            .padding(.horizontal, 24)
                            .padding(.top, 28)
                            .padding(.bottom, 12)

                        if hasFreshKey {
                            Text(isChinese
                                 ? "已经注册了新账号。在你把 agent runtime 切到新 key 之前，Claude.ai / Hermes 等的每个 tool call 都会返回 401 user_not_found。Hermes / OpenClaw / Mac / server 路径用 resident consumer config；Claude / ChatGPT / Gemini 这类聊天工具才用 MCP 命令。"
                                 : "A fresh account is registered. Until you re-point your agent at the new key, every tool call from Claude.ai / Hermes / etc. will return 401 user_not_found. Hermes / OpenClaw / Mac / server paths use resident consumer config; Claude / ChatGPT / Gemini-style chat tools use the MCP command.")
                                .font(.notoSerifSC(size: 13))
                                .foregroundStyle(Color.cinSub)
                                .lineSpacing(4)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 28)

                            VStack(alignment: .leading, spacing: 8) {
                                HStack {
                                    Text(isChinese ? "重新连接信息" : "Reconnection details")
                                        .font(.notoSerifSC(size: 13.5))
                                        .foregroundStyle(Color.cinFg)
                                    Spacer()
                                    Button {
                                        UIPasteboard.general.string = reimportBlock
                                        withAnimation(.easeOut(duration: 0.15)) { copied = true }
                                    } label: {
                                        Text(copied
                                             ? (isChinese ? "已复制 ✓" : "COPIED ✓")
                                             : (isChinese ? "复制 ↗" : "COPY ↗"))
                                            .font(.dmMono(size: 9.5, weight: .medium))
                                            .foregroundStyle(Color.cinAccent1)
                                            .kerning(2)
                                    }
                                    .buttonStyle(.plain)
                                }
                                Text(reimportBlock)
                                    .font(.dmMono(size: 9))
                                    .foregroundStyle(Color.cinSub)
                                    .lineSpacing(2)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(10)
                                    .background(Color.cinAccent1Soft)
                                    .overlay { Rectangle().stroke(Color.cinAccent1.opacity(0.3), lineWidth: 1) }
                                    .textSelection(.enabled)
                            }
                            .padding(.horizontal, 24)
                            .padding(.bottom, 28)

                            Text(isChinese
                                 ? "Agent 在下一次连接时会重新 bootstrap——身份卡和记忆花园会从空开始。"
                                 : "After you update the runtime, your agent re-bootstraps on its next connection — identity card and memory garden start fresh.")
                                .font(.notoSerifSC(size: 11))
                                .foregroundStyle(Color.cinSub)
                                .lineSpacing(3)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 28)
                        } else {
                            // No fresh key yet — either self-hosted mode (need to
                            // register on user's own VPS and paste back) or cloud
                            // registration silently failed.
                            Text(isChinese
                                 ? "新的 API key 还没就绪。请到 Settings → Storage，按你的存储模式重新拿一个 key——自托管模式从你自己的 VPS 注册并 paste 回来，cloud 模式点 \"REGENERATE API KEY\"。拿到之后，把新的连接信息重新导入你的 agent runtime。"
                                 : "The new API key isn't ready yet. Head to Settings → Storage and grab one — self-hosted mode: register on your VPS and paste back; cloud mode: tap \"REGENERATE API KEY\". Once you have it, re-import the new connection details into your agent runtime.")
                                .font(.notoSerifSC(size: 13))
                                .foregroundStyle(Color.cinSub)
                                .lineSpacing(4)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 28)

                            Text(isChinese
                                 ? "在新 key 到位之前，agent 的每个 tool call 都会返回 401 user_not_found。"
                                 : "Until the new key is in place, every agent tool call will return 401 user_not_found.")
                                .font(.notoSerifSC(size: 11))
                                .foregroundStyle(Color.cinAccent2)
                                .lineSpacing(3)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 28)
                        }
                    }
                }

                Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                Button { dismiss() } label: {
                    Text(buttonLabel)
                }
                .buttonStyle(CinPrimaryButtonStyle())
                .padding(.horizontal, 24)
                .padding(.top, 16)
                .padding(.bottom, 24)
            }
        }
    }

    private var buttonLabel: String {
        if !hasFreshKey {
            return isChinese ? "去 Settings ↗" : "GO TO SETTINGS ↗"
        }
        if copied {
            return isChinese ? "完成 ↗" : "DONE ↗"
        }
        return isChinese ? "稍后再更新 ↗" : "I'LL UPDATE LATER ↗"
    }
}

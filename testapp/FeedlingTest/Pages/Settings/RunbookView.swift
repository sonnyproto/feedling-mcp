import SwiftUI

// MARK: - Runbook viewer ("Help me run my own server")

struct RunbookView: View {
    @EnvironmentObject var router: AppRouter
    @Environment(\.dismiss) private var dismiss
    @State private var runbookText: String = "Loading…"

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            VStack(spacing: 0) {
                HStack {
                    Button(action: { dismiss() }) {
                        Text("← back")
                            .font(.dmMono(size: 9.5))
                            .foregroundStyle(Color.cinFg)
                            .kerning(2)
                    }
                    .buttonStyle(.plain)
                    Spacer()
                    Button {
                        UIPasteboard.general.string = runbookText
                    } label: {
                        Text("COPY ↗")
                            .font(.dmMono(size: 9.5, weight: .medium))
                            .foregroundStyle(Color.cinAccent1)
                            .kerning(2)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    Text(runbookText)
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .lineSpacing(4)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 16)
                }
            }
        }
        .navigationBarHidden(true)
        .onAppear { router.enterDetail() }
        .onDisappear { router.exitDetail() }
        .task { await load() }
    }

    private func load() async {
        // Best-effort: fetch the authoritative self-hosting runbook from
        // GitHub raw. Falls back to a baked pointer if network fails.
        // (Previously this pointed at skill/SKILL.md, which mixed agent
        // skill content with ops content. Those got split 2026-05-12:
        // agent skill → io-onboarding/skill.md; ops → SELF_HOSTING.md.)
        let url = URL(string: "https://raw.githubusercontent.com/teleport-computer/feedling-mcp/main/deploy/SELF_HOSTING.md")!
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let s = String(data: data, encoding: .utf8) {
                runbookText = s
                return
            }
        } catch {}
        runbookText = """
Couldn't fetch the latest self-hosting runbook from GitHub.

Open it directly:
  https://github.com/teleport-computer/feedling-mcp/blob/main/deploy/SELF_HOSTING.md

It walks through: clone, deps, env, systemd units, Caddy + Let's Encrypt,
DNS, iOS → your URL + key. Your agent will fetch its skill separately
from https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md
once the server is up.

Your data stays on your VPS. We stop being in the loop.
"""
    }
}

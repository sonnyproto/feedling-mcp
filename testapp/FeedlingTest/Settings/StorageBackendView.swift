import SwiftUI

// MARK: - Storage backend detail view

struct StorageBackendView: View {
    @EnvironmentObject var router: AppRouter
    @ObservedObject private var api = FeedlingAPI.shared
    @Environment(\.dismiss) private var dismiss

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
                    Text("BACKEND")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    VStack(spacing: 0) {
                        backendRow("Mode", value: api.storageMode == .cloud ? "Cloud" : "Self-hosted")
                        backendRow("API URL", value: api.baseURL)
                        backendRow("User ID", value: api.userId.isEmpty ? "—" : String(api.userId.prefix(20)) + "…")
                    }
                }
            }
        }
        .navigationBarHidden(true)
        .onAppear { router.enterDetail() }
        .onDisappear { router.exitDetail() }
    }

    private func backendRow(_ name: String, value: String) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Text(name)
                .font(.notoSerifSC(size: 13.5))
                .foregroundStyle(Color.cinFg)
            Spacer()
            Text(value)
                .font(.dmMono(size: 9))
                .foregroundStyle(Color.cinSub)
                .lineLimit(1)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
        }
    }
}

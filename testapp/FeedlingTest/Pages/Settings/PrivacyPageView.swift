import SwiftUI

// MARK: - Privacy page (NavigationLink destination from Settings)

struct PrivacyPageView: View {
    @EnvironmentObject var router: AppRouter
    @ObservedObject private var api = FeedlingAPI.shared
    @Environment(\.dismiss) private var dismiss
    @State private var showExportSheet = false
    @State private var showDeleteSheet = false
    @State private var showResetSheet = false
    @State private var toast: String? = nil

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            ScrollView {
                VStack(spacing: 0) {
                    privacyHeader
                    Rectangle().fill(Color.cinFg).frame(height: 1)
                    privacySection("AUDIT") {
                        NavigationLink {
                            AuditCardPage()
                        } label: {
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(api.enclaveComposeHash != nil
                                         ? "Everything you've written is encrypted"
                                         : "Privacy audit not yet run")
                                        .font(.notoSerifSC(size: 13.5))
                                        .foregroundStyle(Color.cinFg)
                                    if let h = api.enclaveComposeHash {
                                        Text("Compose \(h.prefix(8))…")
                                            .font(.dmMono(size: 8.5))
                                            .foregroundStyle(Color.cinSub)
                                    }
                                }
                                Spacer()
                                Text("OPEN ↗")
                                    .font(.dmMono(size: 9.5, weight: .medium))
                                    .foregroundStyle(Color.cinAccent1)
                                    .kerning(2)
                            }
                            .padding(.vertical, 14)
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 24)
                        .overlay(alignment: .top) {
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        }
                    }
                    privacySection("YOUR DATA") {
                        privacyActionRow("Export my data", label: "EXPORT ↗", color: .cinFg) { showExportSheet = true }
                        privacyActionRow("Delete my data", label: "DELETE ↗", color: .cinAccent2) { showDeleteSheet = true }
                        privacyActionRow("Reset & re-import", label: "RUN ↗", color: .cinSub) { showResetSheet = true }
                    }
                    privacySection("WHERE YOUR DATA LIVES") {
                        NavigationLink {
                            StorageBackendView()
                        } label: {
                            privacyLinkRow("Backend: \(api.storageMode == .cloud ? "Cloud" : "Self-hosted")")
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 24)
                        .overlay(alignment: .top) {
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        }

                        NavigationLink {
                            RunbookView()
                        } label: {
                            privacyLinkRow("Self-hosting runbook")
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 24)
                        .overlay(alignment: .top) {
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        }
                    }
                    privacySection("ADVANCED") {
                        NavigationLink {
                            AuditCardPage()
                        } label: {
                            privacyLinkRow("Re-run privacy audit")
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 24)
                        .overlay(alignment: .top) {
                            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        }

                        privacyActionRow("Show intro again", label: "SHOW ↗", color: .cinSub) {
                            FeedlingAPI.shared.hasCompletedOnboardingV1 = false
                            showToast("Intro will show on next launch")
                        }
                    }
                    Spacer(minLength: 40)
                }
            }
        }
        .navigationBarHidden(true)
        .onAppear { router.enterDetail() }
        .onDisappear { router.exitDetail() }
        .sheet(isPresented: $showExportSheet) { ExportSheet() }
        .sheet(isPresented: $showDeleteSheet) { DeleteSheet() }
        .sheet(isPresented: $showResetSheet) { ResetAndReimportSheet() }
        .overlay(alignment: .bottom) {
            if let msg = toast {
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
    }

    private var privacyHeader: some View {
        HStack(alignment: .lastTextBaseline) {
            Button(action: { dismiss() }) {
                Text("← settings")
                    .font(.dmMono(size: 9.5))
                    .foregroundStyle(Color.cinFg)
                    .kerning(2)
            }
            .buttonStyle(.plain)
            Spacer()
            Text("PRIVACY & AUDIT")
                .font(.dmMono(size: 9))
                .foregroundStyle(Color.cinSub)
                .kerning(2)
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 12)
    }

    @ViewBuilder
    private func privacySection<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(label)
                .font(.dmMono(size: 9.5, weight: .medium))
                .foregroundStyle(Color.cinAccent1)
                .kerning(3)
                .padding(.horizontal, 24)
                .padding(.top, 18)
                .padding(.bottom, 8)
            content()
        }
    }

    private func privacyLinkRow(_ title: String) -> some View {
        HStack {
            Text(title)
                .font(.notoSerifSC(size: 13.5))
                .foregroundStyle(Color.cinFg)
            Spacer()
            Text("OPEN ↗")
                .font(.dmMono(size: 9.5, weight: .medium))
                .foregroundStyle(Color.cinAccent1)
                .kerning(2)
        }
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private func privacyActionRow(_ name: String, label: String, color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack {
                Text(name)
                    .font(.notoSerifSC(size: 13.5))
                    .foregroundStyle(Color.cinFg)
                Spacer()
                Text(label)
                    .font(.dmMono(size: 9.5, weight: .medium))
                    .foregroundStyle(color)
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

    private func showToast(_ message: String) {
        withAnimation { toast = message }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            withAnimation { toast = nil }
        }
    }
}

// MARK: - Audit card page

struct AuditCardPage: View {
    @EnvironmentObject var router: AppRouter
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
                    Text("PRIVACY AUDIT")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    AuditCardView()
                        .padding(24)
                }
            }
        }
        .navigationBarHidden(true)
        .onAppear { router.enterDetail() }
        .onDisappear { router.exitDetail() }
    }
}

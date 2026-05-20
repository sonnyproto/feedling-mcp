import SwiftUI

// MARK: - Delete sheet (with "download first" default-on checkbox)

struct DeleteSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var downloadFirst = true
    @State private var running = false
    @State private var error: String? = nil
    @State private var exportedToShare: FeedlingAPI.ExportResult? = nil
    @State private var didExport = false
    @State private var showShareSheet = false
    @State private var pendingDelete = false

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
                    Text("DELETE")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Delete my data")
                            .font(.newsreader(size: 26))
                            .foregroundStyle(Color.cinAccent2)
                            .padding(.horizontal, 24)
                            .padding(.top, 24)
                            .padding(.bottom, 14)
                        Text("Revokes your account, deletes every ciphertext blob on our servers, and wipes the keys on this device. Cannot be undone.")
                            .font(.notoSerifSC(size: 13))
                            .foregroundStyle(Color.cinSub)
                            .lineSpacing(4)
                            .padding(.horizontal, 24)
                            .padding(.bottom, 24)
                        Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Download my data first")
                                    .font(.notoSerifSC(size: 13))
                                    .foregroundStyle(Color.cinFg)
                                Text("Keeps a decrypted archive before deleting.")
                                    .font(.interTight(size: 11))
                                    .foregroundStyle(Color.cinSub)
                            }
                            Spacer()
                            Toggle("", isOn: $downloadFirst)
                                .labelsHidden()
                                .tint(Color.cinAccent1)
                        }
                        .padding(.horizontal, 24)
                        .padding(.vertical, 14)
                        if let err = error {
                            Text(err)
                                .font(.dmMono(size: 9))
                                .foregroundStyle(Color.cinAccent2)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 12)
                        }
                        Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
                        if running {
                            Text("DELETING…")
                                .font(.dmMono(size: 9, weight: .medium))
                                .foregroundStyle(Color.cinSub)
                                .kerning(2)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 16)
                        } else {
                            Button { Task { await runDelete() } } label: { Text("DELETE ↗") }
                                .buttonStyle(CinPrimaryButtonStyle())
                                .padding(.horizontal, 24)
                                .padding(.top, 20)
                        }
                    }
                }
            }
        }
        .sheet(isPresented: $showShareSheet, onDismiss: {
            if pendingDelete { Task { await performFinalDelete() } }
        }) {
            if let r = exportedToShare, let tmp = writeTempFile(r) {
                ShareSheet(activityItems: [tmp])
            }
        }
    }

    private func runDelete() async {
        running = true
        defer { running = false }
        if downloadFirst {
            do {
                let r = try await FeedlingAPI.shared.exportMyData()
                exportedToShare = r
                pendingDelete = true
                showShareSheet = true
            } catch {
                self.error = "Export failed: \(error). Aborting delete to protect your data."
            }
        } else {
            await performFinalDelete()
        }
    }

    private func performFinalDelete() async {
        do {
            try await FeedlingAPI.shared.deleteMyDataAndResetLocalState()
            dismiss()
        } catch {
            self.error = "Delete failed: \(error)"
        }
    }

    private func writeTempFile(_ r: FeedlingAPI.ExportResult) -> URL? {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(r.suggestedFilename)
        try? r.data.write(to: url)
        return url
    }
}

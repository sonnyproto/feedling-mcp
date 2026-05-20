import SwiftUI

// MARK: - Export sheet

struct ExportSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var running = false
    @State private var error: String? = nil
    @State private var exportData: FeedlingAPI.ExportResult? = nil
    @State private var showShareSheet = false

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
                    Text("EXPORT")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 14)
                Rectangle().fill(Color.cinFg).frame(height: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Export my data")
                            .font(.newsreader(size: 26))
                            .foregroundStyle(Color.cinFg)
                            .padding(.horizontal, 24)
                            .padding(.top, 24)
                            .padding(.bottom, 14)
                        Text("Assembles every item on your account into a single JSON file and hands it to the iOS share sheet.")
                            .font(.notoSerifSC(size: 13))
                            .foregroundStyle(Color.cinSub)
                            .lineSpacing(4)
                            .padding(.horizontal, 24)
                            .padding(.bottom, 10)
                        Text("If you save to iCloud Drive, the unencrypted copy leaves your phone. Save to Files (On My iPhone) to keep it local.")
                            .font(.interTight(size: 11))
                            .foregroundStyle(Color.cinSub)
                            .lineSpacing(3)
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
                        if running {
                            Text("PACKAGING…")
                                .font(.dmMono(size: 9, weight: .medium))
                                .foregroundStyle(Color.cinSub)
                                .kerning(2)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 16)
                        } else {
                            Button { Task { await runExport() } } label: { Text("EXPORT ↗") }
                                .buttonStyle(CinPrimaryButtonStyle())
                                .padding(.horizontal, 24)
                                .padding(.top, 20)
                        }
                    }
                }
            }
        }
        .sheet(isPresented: $showShareSheet) {
            if let result = exportData, let tmp = writeTempFile(result) {
                ShareSheet(activityItems: [tmp])
            }
        }
    }

    private func runExport() async {
        running = true; defer { running = false }
        do {
            let result = try await FeedlingAPI.shared.exportMyData()
            exportData = result
            showShareSheet = true
        } catch {
            self.error = "\(error)"
        }
    }

    private func writeTempFile(_ result: FeedlingAPI.ExportResult) -> URL? {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(result.suggestedFilename)
        do {
            try result.data.write(to: url)
            return url
        } catch {
            self.error = "write failed: \(error)"
            return nil
        }
    }
}

// MARK: - Share sheet (UIActivityViewController wrapper)

struct ShareSheet: UIViewControllerRepresentable {
    let activityItems: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
    }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}

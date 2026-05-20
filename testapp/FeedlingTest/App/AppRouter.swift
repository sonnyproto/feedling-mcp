import SwiftUI

// MARK: - Tab enum

enum AppTab: Int {
    case chat = 0
    case identity = 1
    case garden = 2
    case settings = 3
}

// MARK: - Router

class AppRouter: ObservableObject {
    @Published var selectedTab: AppTab = .chat
    /// Depth counter for secondary (pushed) views. The root tab bar hides
    /// itself whenever any secondary view is on screen so users don't jump
    /// between top-level tabs while reading a card / running diagnostics /
    /// etc. and then have to find their way back.
    ///
    /// We use a counter rather than a Bool because nested pushes are real
    /// (e.g. Settings → Privacy → Audit). A boolean would flip to false when
    /// the inner view pops, even though we're still in the middle layer.
    @Published private var detailDepth: Int = 0

    var isInDetail: Bool { detailDepth > 0 }

    func enterDetail() { detailDepth += 1 }
    func exitDetail()  { detailDepth = max(0, detailDepth - 1) }
}

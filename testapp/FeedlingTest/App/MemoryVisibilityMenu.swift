import SwiftUI

// MARK: - Memory visibility context menu

extension View {
    func feedlingMemoryVisibilityMenu(
        moment: MemoryMoment,
        onFlip: @escaping (Bool) -> Void   // toLocalOnly
    ) -> some View {
        self.contextMenu {
            let currentlyLocal = moment.visibility == "local_only"
            if currentlyLocal {
                Button {
                    onFlip(false)   // flip to shared
                } label: {
                    Label("Share with agent", systemImage: "eye")
                }
            } else {
                Button {
                    onFlip(true)    // flip to local_only
                } label: {
                    Label("Hide from agent", systemImage: "eye.slash")
                }
            }
        }
    }
}

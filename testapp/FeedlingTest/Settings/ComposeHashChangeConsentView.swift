import SwiftUI

// MARK: - Compose-hash-changed consent (full-screen)

struct ComposeHashChangeConsentView: View {
    @ObservedObject private var api = FeedlingAPI.shared

    var body: some View {
        ZStack {
            Color.feedlingPaper.ignoresSafeArea()
            VStack(spacing: Spacing.lg) {
                Spacer()
                Image(systemName: "sparkles")
                    .font(.system(size: 56))
                    .foregroundStyle(Color.feedlingSage)
                    .accessibilityHidden(true)
                Text("A new version is available.")
                    .multilineTextAlignment(.center)
                    .feedlingDisplay(.medium)
                Text("The app on your phone just saw a newer version of the server.")
                    .multilineTextAlignment(.center)
                    .feedlingBody()
                    .frame(maxWidth: 320)
                if let change = api.pendingComposeHashChange {
                    VStack(spacing: Spacing.sm) {
                        Text(change.oldHash.prefix(16) + "…")
                            .font(.feedlingMono())
                            .foregroundStyle(Color.feedlingInkMuted)
                        Image(systemName: "arrow.down")
                            .foregroundStyle(Color.feedlingSage)
                            .accessibilityLabel("changed to")
                        Text(change.newHash.prefix(16) + "…")
                            .font(.feedlingMono())
                            .foregroundStyle(Color.feedlingInk)
                    }
                    .padding(.top, Spacing.sm)
                }
                Text("Your existing encrypted memories and chat are still readable — they were encrypted to a key that's bound to your account, not to any specific server version.")
                    .multilineTextAlignment(.center)
                    .feedlingCaption()
                    .frame(maxWidth: 340)
                    .padding(.top, Spacing.sm)
                Spacer()
                VStack(spacing: Spacing.md) {
                    Button {
                        api.acceptComposeHashChange()
                    } label: { Text("Got it, continue") }
                        .buttonStyle(FeedlingPrimaryButtonStyle())
                    Button {
                        api.signOutForComposeChange()
                    } label: { Text("Sign out for now") }
                        .buttonStyle(FeedlingSecondaryButtonStyle())
                }
                .padding(.bottom, Spacing.xl2)
            }
            .padding(.horizontal, Spacing.xl)
        }
    }
}

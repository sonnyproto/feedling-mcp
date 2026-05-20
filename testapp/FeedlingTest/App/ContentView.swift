import SwiftUI

// MARK: - Root view (TabView)

struct ContentView: View {
    @EnvironmentObject var router: AppRouter
    @EnvironmentObject var chatViewModel: ChatViewModel
    @EnvironmentObject var identityViewModel: IdentityViewModel
    @EnvironmentObject var memoryViewModel: MemoryViewModel
    @ObservedObject private var api = FeedlingAPI.shared

    // Phase B: before the chat tab loads on first ever launch, show
    // the three-slide onboarding. Re-shown only from Settings.
    @State private var onboardingShown: Bool = FeedlingAPI.shared.hasCompletedOnboardingV1

    @State private var isKeyboardVisible = false
    @State private var deviceBottomInset: CGFloat = 0

    var body: some View {
        ZStack {
            if !onboardingShown {
                OnboardingView(onDone: {
                    FeedlingAPI.shared.hasCompletedOnboardingV1 = true
                    withAnimation(.easeOut(duration: 0.35)) { onboardingShown = true }
                })
                .transition(.opacity)
            } else {
                rootTabs
            }
        }
        // Phase B: compose-hash-changed consent modal blocks the app
        // until the user reviews or signs out.
        .fullScreenCover(isPresented: $api.composeHashChangedRequiresConsent) {
            ComposeHashChangeConsentView()
        }
    }

    private var rootTabs: some View {
        GeometryReader { geo in
            ZStack(alignment: .bottom) {
                Color.cinBg.ignoresSafeArea()

                // Tab content — all views stay alive, opacity switches active tab
                ZStack {
                    ChatView()
                        .environmentObject(chatViewModel)
                        .environmentObject(identityViewModel)
                        .opacity(router.selectedTab == .chat ? 1 : 0)

                    IdentityView()
                        .environmentObject(identityViewModel)
                        .opacity(router.selectedTab == .identity ? 1 : 0)

                    MemoryGardenView()
                        .environmentObject(memoryViewModel)
                        .environmentObject(chatViewModel)
                        .environmentObject(router)
                        .opacity(router.selectedTab == .garden ? 1 : 0)

                    SettingsView()
                        .opacity(router.selectedTab == .settings ? 1 : 0)
                }
                // When keyboard is visible OR we're in a secondary detail
                // view: remove bottom padding so the content uses the full
                // height. When no keyboard and on a top-level tab: use the
                // stored device inset so the value never inflates when
                // geo.safeAreaInsets.bottom grows.
                .padding(.bottom, (isKeyboardVisible || router.isInDetail) ? 0 : (52 + deviceBottomInset))

                if !isKeyboardVisible && !router.isInDetail {
                    CinnabarTabBar(selectedTab: $router.selectedTab,
                                   bottomInset: deviceBottomInset)
                }
            }
            .ignoresSafeArea(edges: .bottom)
            .onAppear {
                // Capture once, before keyboard ever appears.
                if deviceBottomInset == 0 {
                    deviceBottomInset = geo.safeAreaInsets.bottom
                }
            }
        }
        .preferredColorScheme(.light)
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            withAnimation(.easeInOut(duration: 0.2)) { isKeyboardVisible = true }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in
            withAnimation(.easeInOut(duration: 0.2)) { isKeyboardVisible = false }
        }
        // Removed the automatic tab-switch to Identity on bootstrap-detect.
        // It was keyed on `wasNil` which resets to true every app launch, so
        // every cold open of an already-bootstrapped account would yank the
        // user from Chat → Identity within a few seconds. Users navigate
        // themselves.
    }
}

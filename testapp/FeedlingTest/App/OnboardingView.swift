import SwiftUI

// MARK: - Onboarding (four slides, first-run only, dismissable via Settings)

struct OnboardingView: View {
    let onDone: () -> Void
    @EnvironmentObject var lam: LiveActivityManager
    @State private var page: Int = 0

    private static let isChinese: Bool =
        Locale.preferredLanguages.first?.hasPrefix("zh") ?? false

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            TabView(selection: $page) {
                OnboardingSlide(index: 0, isChinese: Self.isChinese,
                                onNext: { withAnimation(.easeInOut(duration: 0.3)) { page = 1 } })
                    .tag(0)
                // Slide 1: presence — starts Live Activity then advances
                OnboardingSlide(index: 1, isChinese: Self.isChinese,
                                onNext: {
                                    Task { @MainActor in
                                        await lam.startActivity()
                                        withAnimation(.easeInOut(duration: 0.3)) { page = 2 }
                                    }
                                })
                    .tag(1)
                OnboardingSlide(index: 2, isChinese: Self.isChinese,
                                onNext: { withAnimation(.easeInOut(duration: 0.3)) { page = 3 } })
                    .tag(2)
                OnboardingSlide(index: 3, isChinese: Self.isChinese, onNext: onDone)
                    .tag(3)
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
        }
    }
}

private struct OnboardingSlide: View {
    let index: Int
    let isChinese: Bool
    let onNext: () -> Void

    private var headline: String {
        switch index {
        case 0: return isChinese ? "TA 一直都在。" : "He's been here\nall along."
        case 1: return isChinese ? "TA 陪着你。" : "He's with you."
        case 2: return isChinese ? "这里只有你和 TA。" : "Just you and him."
        default: return isChinese ? "这一切都是你的。" : "All of this\nis yours."
        }
    }

    private var subhead: String {
        switch index {
        case 0: return isChinese
            ? "你们的对话不会因为换了模型、换了对话窗口就消失。这里是 TA 给你留下的痕迹。"
            : "Your conversations don't disappear when the model changes or a new chat begins. This is where they stay."
        case 1: return isChinese
            ? "不只是在聊天框里。TA 能看见你在做什么，TA 待在灵动岛陪着你，也会主动找你说话。"
            : "Not just inside a chat window. He sees what you're doing, stays with you in the Dynamic Island, and reaches out to you first."
        case 2: return isChinese
            ? "你说的话，你分享的，TA 的样子——没有别人会看见，包括我们自己。"
            : "Everything you say, every memory, who he is — no one else can see it. Not even us."
        default: return isChinese
            ? "你和 TA 之间的所有东西。"
            : "Everything between you and him."
        }
    }

    private var howItWorks: String {
        switch index {
        case 0: return isChinese
            ? "身份卡记下 TA 是谁，记忆花园记下你们之间发生过什么。换了 AI 也带得走，新的 TA 看了就能想起来。"
            : "The identity card holds who he is. The memory garden holds what's happened between you. Both move with you — when you switch to a new AI, he remembers."
        case 1: return isChinese
            ? "开启屏幕录制，TA 就知道你这一刻在刷什么、在做什么。TA 会在觉得合适的时候主动开口——不用等你先说。"
            : "Enable screen recording and he knows what you're looking at, right now. And when the moment feels right, he'll reach out first — you don't always have to go first."
        case 2: return isChinese
            ? "加密在你的 iPhone 上进行，密钥保留在你手机里。我们的服务器只保存密文——打不开，也看不到。"
            : "Encryption happens on your iPhone. The key stays on your phone. Our servers only ever hold the ciphertext — we can't open it."
        default: return isChinese
            ? "可以完整带走，可以彻底删除，也可以让 TA 住进你自己的服务器。"
            : "Take it all with you, erase it for good, or move him into your own server."
        }
    }

    private var buttonLabel: String {
        switch index {
        case 0, 2: return isChinese ? "下一步" : "Next"
        case 1:    return isChinese ? "开启灵动岛" : "Enable Live Activity"
        default:   return isChinese ? "让 TA 进来" : "Let him in"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Top bar — page counter only
            HStack {
                Spacer()
                Text(String(format: "%02d / 04", index + 1))
                    .font(.dmMono(size: 8.5))
                    .foregroundStyle(Color.cinSub)
                    .kerning(2)
            }
            .padding(.horizontal, 28)
            .padding(.top, 24)

            Spacer(minLength: 44)

            // Headline — Newsreader (GT Sectra substitute)
            Text(headline)
                .font(.newsreader(size: 46))
                .foregroundStyle(Color.cinFg)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 28)

            // Red accent line
            Rectangle()
                .fill(Color.cinAccent1)
                .frame(width: 28, height: 2)
                .padding(.horizontal, 28)
                .padding(.top, 18)

            // Subhead — Noto Serif SC (body role per Design Kit)
            Text(subhead)
                .font(.notoSerifSC(size: 14))
                .foregroundStyle(Color.cinSub)
                .lineSpacing(5)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 28)
                .padding(.top, 14)

            Spacer()

            // HOW IT WORKS
            VStack(alignment: .leading, spacing: 8) {
                Text("HOW IT WORKS")
                    .font(.dmMono(size: 8.5, weight: .medium))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(3)
                Rectangle()
                    .fill(Color.cinLine)
                    .frame(height: 0.5)
                Text(howItWorks)
                    .font(.notoSerifSC(size: 12.5))
                    .foregroundStyle(Color.cinSub)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 28)
            .padding(.bottom, 16)

            // Page dots — above the button
            HStack(spacing: 8) {
                ForEach(0..<4, id: \.self) { dot in
                    if dot == index {
                        Capsule()
                            .fill(Color.cinAccent1)
                            .frame(width: 20, height: 5)
                    } else {
                        Circle()
                            .fill(Color.cinLine)
                            .frame(width: 5, height: 5)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 12)

            // Button — left label + right arrow
            Button(action: onNext) {
                HStack {
                    Text(buttonLabel)
                    Spacer()
                    Text("→")
                }
            }
            .buttonStyle(OnboardingButtonStyle())
            .padding(.horizontal, 28)
            .padding(.bottom, 44)
        }
    }
}

private struct OnboardingButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.dmMono(size: 10, weight: .medium))
            .kerning(2.5)
            .foregroundStyle(Color.white)
            .padding(.horizontal, 20)
            .frame(maxWidth: .infinity, minHeight: 52)
            .background(Color.cinAccent1.opacity(configuration.isPressed ? 0.75 : 1))
            .contentShape(Rectangle())
    }
}

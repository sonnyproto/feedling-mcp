import SwiftUI

// MARK: - Identity Tab

struct IdentityView: View {
    @EnvironmentObject var vm: IdentityViewModel
    // For tap-to-chat: tapping a "最近的变化" card preloads a draft in
    // ChatView and switches to the Chat tab. Both env objects already
    // exist in the app — same pattern as HealthCheckView's diagnostic
    // shortcuts.
    @EnvironmentObject var chatVM: ChatViewModel
    @EnvironmentObject var router: AppRouter

    private let isChinese: Bool =
        Locale.preferredLanguages.first?.hasPrefix("zh") ?? false

    var body: some View {
        ZStack {
            Color.cinBg.ignoresSafeArea()
            if let identity = vm.identity {
                ScrollView {
                    VStack(spacing: 0) {
                        identityHeader(identity)
                        Rectangle().fill(Color.cinFg).frame(height: 1).padding(.horizontal, 24)
                        radarSection(identity)
                        Rectangle().fill(Color.cinFg).frame(height: 1).padding(.horizontal, 24)
                        dimensionsList(identity)
                        if !vm.recentChanges.isEmpty {
                            Rectangle().fill(Color.cinFg).frame(height: 1).padding(.horizontal, 24)
                            recentChangesSection
                        }
                    }
                }
            } else {
                emptyState
            }
        }
        .onAppear { vm.startPolling() }
        .onDisappear { vm.stopPolling() }
    }

    // MARK: - Header

    private func identityHeader(_ id: IdentityCard) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            // Top meta row
            HStack(alignment: .lastTextBaseline) {
                Text("Identity")
                    .font(.newsreader(size: 13, italic: true))
                    .foregroundStyle(Color.cinFg)
                Spacer()
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 12)

            Rectangle().fill(Color.cinFg).frame(height: 1).padding(.horizontal, 24)

            // Relation stage — days + label
            HStack(alignment: .lastTextBaseline, spacing: 8) {
                Text("\(id.daysWithUser)")
                    .font(.newsreader(size: 48))
                    .foregroundStyle(Color.cinAccent1)
                VStack(alignment: .leading, spacing: 1) {
                    Text("DAYS")
                        .font(.dmMono(size: 9, weight: .medium))
                        .foregroundStyle(Color.cinAccent1)
                        .kerning(2.5)
                    Text("TOGETHER")
                        .font(.dmMono(size: 9))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2.5)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 16)

            // Agent name
            Text(id.agentName.isEmpty ? "—" : id.agentName)
                .font(.newsreader(size: 40))
                .foregroundStyle(Color.cinAccent1)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
                .padding(.horizontal, 24)
                .padding(.bottom, 10)

            // Self-introduction (one-line agent tagline)
            if !id.selfIntroduction.isEmpty {
                Text(id.selfIntroduction)
                    .font(.notoSerifSC(size: 13))
                    .italic()
                    .foregroundStyle(Color.cinSub)
                    .lineSpacing(2)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 16)
            }

            // Signature — agent's attitude toward proactive messaging, in its own voice
            if let sig = id.signature, !sig.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(sig, id: \.self) { line in
                        Text(line)
                            .font(.newsreader(size: 13, italic: true))
                            .foregroundStyle(Color.cinFg.opacity(0.55))
                            .lineSpacing(2)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 24)
                .padding(.bottom, 20)
            }

            // Category tag
            if let cat = id.category, !cat.isEmpty {
                Text(cat)
                    .font(.dmMono(size: 9))
                    .foregroundStyle(Color.cinSub)
                    .kerning(2)
                    .padding(.horizontal, 24)
                    .padding(.bottom, 20)
            }
        }
    }

    // MARK: - Hatched Radar

    private func radarSection(_ id: IdentityCard) -> some View {
        VStack(spacing: 0) {
            HatchedRadarView(dimensions: id.dimensions)
                .frame(height: 280)
                .padding(.vertical, 24)
                .padding(.horizontal, 24)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Dimensions list

    private func dimensionsList(_ id: IdentityCard) -> some View {
        VStack(spacing: 0) {
            // Spacer pins the label to the leading edge so it matches the
            // "RECENT CHANGES" section below; without it the HStack sizes to
            // content and the outer VStack centers it.
            HStack(alignment: .lastTextBaseline, spacing: 10) {
                Text("DIMENSIONS")
                    .font(.dmMono(size: 9.5))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(3)
                    .fontWeight(.semibold)
                Spacer()
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 8)

            ForEach(Array(id.dimensions.enumerated()), id: \.element.id) { idx, dim in
                CinDimensionRow(index: idx + 1, dimension: dim)
            }
        }
        .padding(.bottom, 32)
    }

    // MARK: - Recent changes ("最近的变化")
    //
    // Renders the top 3 most recent identity_change events as cards.
    // Each card is a tap target: tapping preloads ChatView with a draft
    // message that references THIS specific change, and switches to the
    // Chat tab. The user can edit the draft (or send as-is) — the agent
    // sees a normal user message and decides how to respond. We do not
    // tell the agent what to say; the iOS layer just opens the channel.

    private var recentChangesSection: some View {
        VStack(spacing: 0) {
            HStack(alignment: .lastTextBaseline, spacing: 10) {
                Text(isChinese ? "最近的变化" : "RECENT CHANGES")
                    .font(.dmMono(size: 9.5))
                    .foregroundStyle(Color.cinAccent1)
                    .kerning(3)
                    .fontWeight(.semibold)
                Spacer()
                if vm.recentChanges.count > 3 {
                    // TODO: future "ALL ↗" tap action — opens a dedicated
                    // history page. For now it's a passive label so the
                    // user knows the feed is truncated.
                    Text("ALL ↗")
                        .font(.dmMono(size: 9, weight: .medium))
                        .foregroundStyle(Color.cinSub)
                        .kerning(2)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 12)

            ForEach(vm.recentChanges.prefix(3)) { change in
                IdentityChangeCard(
                    change: change,
                    isChinese: isChinese,
                    onTap: { tapChange(change) }
                )
                .padding(.horizontal, 24)
                .padding(.bottom, 12)
            }
        }
        .padding(.bottom, 32)
    }

    /// Build a draft message referencing this specific change, drop it
    /// into ChatViewModel.inputText, and switch to the Chat tab. The
    /// draft is intentionally short and incomplete — it gives the user
    /// a context-anchor opener; they finish the sentence themselves.
    private func tapChange(_ change: IdentityChange) {
        let opener = draftOpener(for: change, isChinese: isChinese)
        chatVM.inputText = opener
        router.selectedTab = .chat
    }

    /// Compose the chat draft. Format is "关于...，我想说" / "About ... I want to
    /// say" — gives context without putting words in the user's mouth.
    private func draftOpener(for change: IdentityChange, isChinese: Bool) -> String {
        switch change.action {
        case "nudge":
            let dim = change.dimension ?? "—"
            let delta = change.delta ?? 0
            let sign = delta > 0 ? "+" : ""
            if isChinese {
                return "关于你刚把「\(dim) \(sign)\(delta)」那次调整，我想说："
            } else {
                return "About that \"\(dim) \(sign)\(delta)\" adjustment you just made — "
            }
        case "replace":
            return isChinese
                ? "关于你刚才把整张身份卡重写，我想说："
                : "About rewriting the whole identity card just now — "
        case "init":
            return isChinese
                ? "关于你刚才第一次写身份卡，我想说："
                : "About writing the identity card for the first time — "
        default:
            return isChinese
                ? "关于你刚才那次身份调整，我想说："
                : "About that identity change just now — "
        }
    }

    // MARK: - Empty state

    private var emptyState: some View {
        VStack(spacing: 20) {
            Image(systemName: "sparkles")
                .font(.system(size: 44, weight: .thin))
                .foregroundStyle(Color.cinLine)
            Text(isChinese ? "还没有身份卡" : "No identity card yet")
                .font(.newsreader(size: 22, italic: true))
                .foregroundStyle(Color.cinSub)
            Text(isChinese
                 ? "Agent 走完 onboarding 后，这里会有 TA 的名字、性格维度、和 TA 想跟你说的第一句话。"
                 : "After your agent completes onboarding, you'll see their name, dimensions, and the first thing they want to say to you.")
                .font(.interTight(size: 13))
                .foregroundStyle(Color.cinSub)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 24)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Dimension Row

private struct CinDimensionRow: View {
    let index: Int
    let dimension: IdentityCard.Dimension

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            // Index
            Text(String(format: "%02d", index))
                .font(.dmMono(size: 9))
                .foregroundStyle(Color.cinSub)
                .kerning(1)
                .frame(width: 24)

            // Name — column widened to fit two-word English dimension names
            // ("Execution Drive", "Precision & Boundaries", …) without
            // SwiftUI breaking single words across lines character-by-character.
            // Two-line wrap is allowed; minimumScaleFactor catches the longest
            // edge cases ("Operational Caution"). Chinese names (2–4 chars)
            // still left-align with whitespace to the right — fine.
            Text(dimension.name)
                .font(.notoSerifSC(size: 14, weight: .medium))
                .foregroundStyle(Color.cinFg)
                .lineLimit(2)
                .minimumScaleFactor(0.85)
                .multilineTextAlignment(.leading)
                .frame(width: 110, alignment: .leading)

            // Bar
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Rectangle()
                        .fill(Color.cinLine)
                        .frame(height: 1)
                    Rectangle()
                        .fill(Color.cinAccent1)
                        .frame(width: geo.size.width * dimension.normalizedValue, height: 2)
                }
            }
            .frame(height: 2)

            // Score + delta
            HStack(spacing: 4) {
                Text("\(dimension.value)")
                    .font(.dmMono(size: 10, weight: .medium))
                    .foregroundStyle(Color.cinFg)
                if let delta = dimension.delta, !delta.isEmpty {
                    Text(delta)
                        .font(.dmMono(size: 9))
                        .foregroundStyle(delta.hasPrefix("+") ? Color.cinAccent1 : Color.cinSub)
                }
            }
            .frame(width: 56, alignment: .trailing)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cinLine).frame(height: 0.5).padding(.horizontal, 24)
        }
    }
}

// MARK: - Identity-change card
//
// One entry in the "最近的变化" feed. Style matches the Identity page's
// existing typography (dmMono for timestamps/numbers, notoSerifSC for
// reason text, cinAccent1 for delta emphasis). The whole card is a tap
// target — the CTA "和我聊聊 ↗" is a visual hint, not a separate button.

private struct IdentityChangeCard: View {
    let change: IdentityChange
    let isChinese: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            VStack(alignment: .leading, spacing: 10) {
                // Timestamp row — month/day · time, with action marker if
                // this is an init/replace (no diff to show in the body).
                HStack(alignment: .center, spacing: 8) {
                    Text(formattedTimestamp)
                        .font(.dmMono(size: 9.5))
                        .foregroundStyle(Color.cinSub)
                        .kerning(1.5)
                    Spacer()
                    if change.action != "nudge", let label = actionLabel {
                        Text(label)
                            .font(.dmMono(size: 9))
                            .foregroundStyle(Color.cinAccent1)
                            .kerning(2)
                    }
                }

                // Diff row — only for nudges. "温柔   7.0 → 7.5    +0.5"
                if change.action == "nudge",
                   let dim = change.dimension,
                   let oldV = change.oldValue,
                   let newV = change.newValue {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(dim)
                            .font(.notoSerifSC(size: 15, weight: .medium))
                            .foregroundStyle(Color.cinFg)
                        Text("\(numberFormatted(oldV))  →  \(numberFormatted(newV))")
                            .font(.dmMono(size: 13))
                            .foregroundStyle(Color.cinFg)
                        Spacer()
                        if let d = change.delta {
                            Text(deltaFormatted(d))
                                .font(.dmMono(size: 13, weight: .medium))
                                .foregroundStyle(Color.cinAccent1)
                        }
                    }
                }

                // Reason text — agent's own voice. Italic newsreader to
                // match the signature line on the Identity header.
                if let reason = change.reason, !reason.isEmpty {
                    Text(reason)
                        .font(.newsreader(size: 13, italic: true))
                        .foregroundStyle(Color.cinFg.opacity(0.75))
                        .lineSpacing(3)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                // CTA hint — entire card is tappable, this is just visual
                // confirmation that the card leads somewhere.
                HStack {
                    Spacer()
                    Text(isChinese ? "和我聊聊  ↗" : "Talk to me  ↗")
                        .font(.dmMono(size: 10, weight: .medium))
                        .foregroundStyle(Color.cinAccent1)
                        .kerning(1.5)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(
                Rectangle()
                    .stroke(Color.cinLine, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    // Timestamps from the server are ISO 8601. Display as "5/19 · 14:32".
    private var formattedTimestamp: String {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = iso.date(from: change.ts)
        if date == nil {
            iso.formatOptions = [.withInternetDateTime]
            date = iso.date(from: change.ts)
        }
        if date == nil {
            // Fallback: try plain "yyyy-MM-dd'T'HH:mm:ss" (server's local-time
            // format without explicit offset). datetime.now().isoformat()
            // doesn't include 'Z'.
            let f = DateFormatter()
            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
            date = f.date(from: change.ts)
            if date == nil {
                f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                date = f.date(from: change.ts)
            }
        }
        guard let d = date else { return change.ts }
        let out = DateFormatter()
        out.dateFormat = "M/d  ·  HH:mm"
        return out.string(from: d)
    }

    private var actionLabel: String? {
        switch change.action {
        case "init":    return "◆  IDENTITY · FIRST WRITE"
        case "replace": return "◆  IDENTITY · REWRITTEN"
        default:        return nil
        }
    }

    /// Server stores dimension values as 0-100 ints; the existing radar
    /// table displays them as one decimal (7.8 etc.) so the change card
    /// follows the same convention. value 78 → "7.8".
    private func numberFormatted(_ v: Int) -> String {
        String(format: "%.1f", Double(v) / 10.0)
    }

    private func deltaFormatted(_ d: Int) -> String {
        let sign = d > 0 ? "+" : ""
        return "\(sign)\(String(format: "%.1f", Double(d) / 10.0))"
    }
}

// MARK: - Hatched Radar

struct HatchedRadarView: View {
    let dimensions: [IdentityCard.Dimension]

    var body: some View {
        Canvas { ctx, size in
            guard !dimensions.isEmpty else { return }
            let n = dimensions.count
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let maxR = min(size.width, size.height) / 2 - 36

            // Grid rings
            for level in stride(from: 0.25, through: 1.0, by: 0.25) {
                var ring = Path()
                for i in 0..<n {
                    let pt = vertex(i: i, n: n, r: maxR * level, c: center)
                    i == 0 ? ring.move(to: pt) : ring.addLine(to: pt)
                }
                ring.closeSubpath()
                ctx.stroke(ring, with: .color(Color.cinLine.opacity(0.6)), lineWidth: 0.5)
            }

            // Spokes
            for i in 0..<n {
                var spoke = Path()
                spoke.move(to: center)
                spoke.addLine(to: vertex(i: i, n: n, r: maxR, c: center))
                ctx.stroke(spoke, with: .color(Color.cinLine.opacity(0.6)), lineWidth: 0.5)
            }

            // Value polygon — hatched fill using clipped diagonal lines
            var fillPath = Path()
            for i in 0..<n {
                let r = maxR * dimensions[i].normalizedValue
                let pt = vertex(i: i, n: n, r: r, c: center)
                i == 0 ? fillPath.move(to: pt) : fillPath.addLine(to: pt)
            }
            fillPath.closeSubpath()

            // Draw 45° hatch lines clipped to the fill polygon.
            // Scope the clip inside drawLayer so it only applies to the hatch
            // pass — otherwise the clip persists for the rest of the closure
            // and chops off the outer half of the polygon outline + the 7
            // vertex dots that sit right on the polygon edge.
            ctx.drawLayer { layerCtx in
                layerCtx.clip(to: fillPath)
                let bounds = CGRect(origin: .zero, size: size)
                let step: CGFloat = 8
                var x = bounds.minX - bounds.height
                while x < bounds.maxX + bounds.height {
                    var hatch = Path()
                    hatch.move(to: CGPoint(x: x, y: bounds.minY))
                    hatch.addLine(to: CGPoint(x: x + bounds.height, y: bounds.maxY))
                    layerCtx.stroke(hatch, with: .color(Color.cinAccent1.opacity(0.25)), lineWidth: 1)
                    x += step
                }
            }

            // Stroke outline (full line width visible — no longer clipped)
            ctx.stroke(fillPath, with: .color(Color.cinAccent1.opacity(0.8)), lineWidth: 1.5)

            // Dots at vertices — drawn last so they sit on top of the polygon
            // outline as full circles. Previously the lingering clip from the
            // hatch pass cut each dot in half.
            for i in 0..<n {
                let r = maxR * dimensions[i].normalizedValue
                let pt = vertex(i: i, n: n, r: r, c: center)
                let dot = CGRect(x: pt.x - 3, y: pt.y - 3, width: 6, height: 6)
                ctx.fill(Path(ellipseIn: dot), with: .color(Color.cinAccent1))
            }
        }
        .overlay(
            GeometryReader { geo in
                let center = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
                let maxR = min(geo.size.width, geo.size.height) / 2 - 36
                ForEach(Array(dimensions.enumerated()), id: \.offset) { i, dim in
                    let pt = vertex(i: i, n: dimensions.count, r: maxR + 22, c: center)
                    // Constrain label width so multi-word English dimension
                    // names ("Tolerance for Fluff", "Precision & Boundaries")
                    // wrap to two lines instead of sprawling horizontally and
                    // colliding with the neighbouring vertex labels. Single-
                    // word and short Chinese names stay on one line within
                    // the same frame.
                    Text(dim.name)
                        .font(.dmMono(size: 10.5))
                        .foregroundStyle(Color.cinSub)
                        .kerning(0.5)
                        .lineSpacing(1)
                        .multilineTextAlignment(.center)
                        .frame(width: 96)
                        .position(pt)
                }
            }
        )
    }

    private func vertex(i: Int, n: Int, r: Double, c: CGPoint) -> CGPoint {
        let angle = (2 * Double.pi / Double(n)) * Double(i) - Double.pi / 2
        return CGPoint(x: c.x + r * cos(angle), y: c.y + r * sin(angle))
    }
}

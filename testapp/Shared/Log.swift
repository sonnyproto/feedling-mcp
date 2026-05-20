import Foundation

func log(_ items: Any..., separator: String = " ", file: String = #file, function: String = #function, line: Int = #line) {
#if DEBUG
    let message = items.map { "\($0)" }.joined(separator: separator)
    let filename = (file as NSString).lastPathComponent
    print("\(filename):\(line) \(message)")
#endif
}

import AppKit
import Foundation
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let menu = NSMenu()
    private var serviceProcess: Process?
    private var statusMenuItem: NSMenuItem!
    private var openMenuItem: NSMenuItem!
    private var startMenuItem: NSMenuItem!
    private var stopMenuItem: NSMenuItem!
    private var loginMenuItem: NSMenuItem!
    private var refreshTimer: Timer?
    private var intentionalStop = false
    private var quitting = false
    private var restartTimes: [Date] = []

    private lazy var supportDirectory: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("StockWatch", isDirectory: true)
    }()
    private var readyFile: URL { supportDirectory.appendingPathComponent("service.url") }
    private var hostLog: URL { supportDirectory.appendingPathComponent("host.log") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        try? FileManager.default.createDirectory(
            at: supportDirectory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        configureStatusItem()
        startService(nil)
        refreshTimer = Timer.scheduledTimer(
            timeInterval: 2,
            target: self,
            selector: #selector(refreshStatus),
            userInfo: nil,
            repeats: true
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        intentionalStop = true
        stopProcess()
    }

    private func configureStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(
                systemSymbolName: "chart.line.uptrend.xyaxis",
                accessibilityDescription: "StockWatch"
            )
        }
        statusMenuItem = NSMenuItem(title: "正在启动本地服务…", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())

        openMenuItem = NSMenuItem(title: "打开 Dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        openMenuItem.target = self
        menu.addItem(openMenuItem)

        startMenuItem = NSMenuItem(title: "启动服务", action: #selector(startService(_:)), keyEquivalent: "")
        startMenuItem.target = self
        menu.addItem(startMenuItem)

        stopMenuItem = NSMenuItem(title: "停止服务", action: #selector(stopService(_:)), keyEquivalent: "")
        stopMenuItem.target = self
        menu.addItem(stopMenuItem)

        menu.addItem(.separator())
        loginMenuItem = NSMenuItem(title: "登录时启动", action: #selector(toggleLaunchAtLogin), keyEquivalent: "")
        loginMenuItem.target = self
        menu.addItem(loginMenuItem)

        let logs = NSMenuItem(title: "在 Finder 中显示日志", action: #selector(showLogs), keyEquivalent: "")
        logs.target = self
        menu.addItem(logs)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出 StockWatch", action: #selector(quitApplication), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
        refreshStatus()
    }

    @objc private func refreshStatus() {
        let running = serviceProcess?.isRunning == true
        let ready = running && FileManager.default.fileExists(atPath: readyFile.path)
        statusMenuItem.title = ready ? "● 服务正在运行" : (running ? "◐ 服务正在启动…" : "○ 服务已停止")
        openMenuItem.isEnabled = ready
        startMenuItem.isEnabled = !running
        stopMenuItem.isEnabled = running
        loginMenuItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        if let button = statusItem.button {
            button.contentTintColor = ready ? NSColor.systemGreen : (running ? NSColor.systemOrange : NSColor.secondaryLabelColor)
        }
    }

    @objc private func openDashboard() {
        var value = "http://127.0.0.1:8765/"
        if let contents = try? String(contentsOf: readyFile, encoding: .utf8), !contents.isEmpty {
            value = contents.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard let url = URL(string: value), url.host == "127.0.0.1" else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func startService(_ sender: Any?) {
        guard serviceProcess?.isRunning != true else { return }
        intentionalStop = false
        try? FileManager.default.removeItem(at: readyFile)
        guard
            let resources = Bundle.main.resourceURL,
            let python = bundledPython(resources: resources)
        else {
            statusMenuItem.title = "○ 缺少 Python 运行时"
            return
        }
        let runtime = resources.appendingPathComponent("runtime", isDirectory: true)
        let launcher = runtime.appendingPathComponent("stockwatch_service.py")
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = runtime
        process.arguments = [
            launcher.path,
            "--source-root", runtime.path,
            "--support-dir", supportDirectory.path,
            "--host", "127.0.0.1",
            "--port", "8765",
            "--ready-file", readyFile.path
        ]
        var environment = ProcessInfo.processInfo.environment
        let userBin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path
        environment["PATH"] = [userBin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"].joined(separator: ":")
        environment["STOCKWATCH_SUPPORT_DIR"] = supportDirectory.path
        environment["STOCKWATCH_DATA_DIR"] = supportDirectory.appendingPathComponent("data").path
        environment["STOCKWATCH_ENV_FILE"] = supportDirectory.appendingPathComponent(".env").path
        let claude = userBin + "/claude"
        if FileManager.default.isExecutableFile(atPath: claude) {
            environment["CLAUDE_CLI_BIN"] = claude
        }
        process.environment = environment

        FileManager.default.createFile(atPath: hostLog.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: hostLog) {
            _ = try? handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async {
                guard let self else { return }
                self.serviceProcess = nil
                try? FileManager.default.removeItem(at: self.readyFile)
                self.refreshStatus()
                if !self.intentionalStop && !self.quitting {
                    self.scheduleLimitedRestart()
                }
            }
        }
        do {
            try process.run()
            serviceProcess = process
            refreshStatus()
        } catch {
            statusMenuItem.title = "○ 启动失败：\(error.localizedDescription)"
        }
    }

    private func bundledPython(resources: URL) -> URL? {
        let candidates = [
            resources.appendingPathComponent("venv/bin/python3.13"),
            resources.appendingPathComponent("venv/bin/python3"),
            resources.appendingPathComponent("venv/bin/python")
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    private func scheduleLimitedRestart() {
        let now = Date()
        restartTimes = restartTimes.filter { now.timeIntervalSince($0) < 300 }
        guard restartTimes.count < 3 else {
            statusMenuItem.title = "○ 服务反复退出，请查看日志"
            return
        }
        restartTimes.append(now)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.startService(nil)
        }
    }

    @objc private func stopService(_ sender: Any?) {
        intentionalStop = true
        stopProcess()
        refreshStatus()
    }

    private func stopProcess() {
        guard let process = serviceProcess, process.isRunning else { return }
        process.terminate()
        let pid = process.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + 8) {
            if process.isRunning {
                kill(pid, SIGKILL)
            }
        }
    }

    @objc private func toggleLaunchAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "无法更新登录启动设置"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
        refreshStatus()
    }

    @objc private func showLogs() {
        NSWorkspace.shared.activateFileViewerSelecting([hostLog])
    }

    @objc private func quitApplication() {
        quitting = true
        intentionalStop = true
        stopProcess()
        NSApp.terminate(nil)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()

import AppKit
import AVFoundation
import AudioToolbox
import CoreAudioKit
import Foundation

func fourCC(_ value: String) -> OSType {
    let padded = value.padding(toLength: 4, withPad: " ", startingAt: 0)
    return padded.utf8.prefix(4).reduce(0) { partial, byte in
        (partial << 8) | OSType(byte)
    }
}

func componentMetadata(from bundleURL: URL) -> (description: AudioComponentDescription, type: String)? {
    guard
        let bundle = Bundle(url: bundleURL),
        let components = bundle.infoDictionary?["AudioComponents"] as? [[String: Any]],
        let first = components.first,
        let type = first["type"] as? String,
        let subtype = first["subtype"] as? String,
        let manufacturer = first["manufacturer"] as? String
    else {
        return nil
    }
    return (
        AudioComponentDescription(
            componentType: fourCC(type),
            componentSubType: fourCC(subtype),
            componentManufacturer: fourCC(manufacturer),
            componentFlags: 0,
            componentFlagsMask: 0
        ),
        type
    )
}

func printLine(_ value: String) {
    if let data = "\(value)\n".data(using: .utf8) {
        FileHandle.standardOutput.write(data)
    }
}

func printErrorLine(_ value: String) {
    if let data = "ERROR \(value)\n".data(using: .utf8) {
        FileHandle.standardOutput.write(data)
    }
}

final class HostRootView: NSView {
    override var acceptsFirstResponder: Bool { true }
    override var canBecomeKeyView: Bool { true }
}

final class PluginContainerViewController: NSViewController {
    private let embeddedController: NSViewController

    init(embeddedController: NSViewController) {
        self.embeddedController = embeddedController
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        nil
    }

    override func loadView() {
        view = HostRootView(frame: NSRect(x: 0, y: 0, width: 960, height: 640))
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        addChild(embeddedController)
        let childView = embeddedController.view
        childView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(childView)
        NSLayoutConstraint.activate([
            childView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            childView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            childView.topAnchor.constraint(equalTo: view.topAnchor),
            childView.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])
    }
}

enum HostMode {
    case editorOnce
    case server(showOnLaunch: Bool)
    case embedded(windowId: String)
}

final class HostDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let pluginURL: URL
    private let mode: HostMode
    private var window: NSWindow?
    private var hostedAudioUnit: AVAudioUnit?
    private var hostedEditorController: NSViewController?
    private var audioEngine: AVAudioEngine?
    private var componentName: String = "Plugin"
    private var componentType: String = ""
    private var readySent = false
    private var openWindowWhenReady = false
    private var stdinBuffer = ""

    init(pluginURL: URL, mode: HostMode) {
        self.pluginURL = pluginURL
        self.mode = mode
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if case .editorOnce = mode {
            NSApp.activate(ignoringOtherApps: true)
        }
        if case .embedded = mode {
            NSApp.setActivationPolicy(.prohibited)
        }
        startPluginHost()
        if case .server = mode {
            startCommandReader()
        }
        if case .embedded = mode {
            startCommandReader()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Clean up audio resources
        if let engine = audioEngine {
            engine.stop()
        }
        if let au = hostedAudioUnit?.auAudioUnit {
            au.deallocateRenderResources()
        }
        audioEngine = nil
        hostedAudioUnit = nil
    }

    private func startPluginHost() {
        guard let metadata = componentMetadata(from: pluginURL) else {
            fail("The plugin bundle does not expose AudioComponents metadata.")
            return
        }

        componentType = metadata.type
        let manager = AVAudioUnitComponentManager.shared()
        let matches = manager.components(matching: metadata.description)
        guard let component = matches.first else {
            fail("This Audio Unit could not be discovered by the system host APIs.")
            return
        }
        componentName = component.name

        let hostSelf = self
        AVAudioUnit.instantiate(with: component.audioComponentDescription, options: []) { avAudioUnit, error in
            DispatchQueue.main.async {
                if let error {
                    hostSelf.fail("The plugin failed to load: \(error.localizedDescription)")
                    return
                }
                guard let avAudioUnit else {
                    hostSelf.fail("The plugin loaded without an AU instance.")
                    return
                }
                
                // Initialize the audio unit
                do {
                    try avAudioUnit.auAudioUnit.allocateRenderResources()
                } catch {
                    printErrorLine("Failed to allocate render resources: \(error.localizedDescription)")
                }
                
                hostSelf.hostedAudioUnit = avAudioUnit
                hostSelf.prepareAudioEngine(with: avAudioUnit)
                hostSelf.sendReadyIfNeeded()
                if case .editorOnce = hostSelf.mode {
                    hostSelf.openOrFocusWindow()
                } else if case .server(let showOnLaunch) = hostSelf.mode, showOnLaunch || hostSelf.openWindowWhenReady {
                    hostSelf.openOrFocusWindow()
                }
            }
        }
    }

    private func prepareAudioEngine(with avAudioUnit: AVAudioUnit) {
        let engine = AVAudioEngine()
        engine.attach(avAudioUnit)
        
        // Get the hardware output format for proper connection
        let outputFormat = engine.outputNode.outputFormat(forBus: 0)
        let sampleRate = outputFormat.sampleRate
        
        // Create a format that matches the hardware for the mixer
        let mixerFormat = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 2)
        
        // Connect plugin to mixer with proper format
        engine.connect(avAudioUnit, to: engine.mainMixerNode, format: mixerFormat)
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: outputFormat)
        
        
        do {
            try engine.start()
            audioEngine = engine
            printLine("AUDIO_ENGINE_STARTED")
        } catch {
            printErrorLine("Audio engine failed to start: \(error.localizedDescription)")
            audioEngine = nil
        }
    }

    private func sendReadyIfNeeded() {
        guard !readySent else {
            return
        }
        readySent = true
        if case .server = mode {
            printLine("READY")
        }
        if case .embedded = mode {
            printLine("READY")
        }
    }
    
    private func reportWindowSize(_ size: NSSize) {
        if case .embedded = mode {
            printLine("SIZE \(Int(size.width)) \(Int(size.height))")
        }
    }

    private func openOrFocusWindow() {
        guard hostedAudioUnit != nil else {
            openWindowWhenReady = true
            return
        }
        openWindowWhenReady = false
        if let window {
            // For embedded mode, just order front without activating
            if case .embedded = mode {
                window.orderFront(nil)
            } else {
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
                if let hostedEditorController {
                    window.makeFirstResponder(hostedEditorController.view)
                }
            }
            return
        }
        requestEditorWindow()
    }

    private func requestEditorWindow() {
        guard let audioUnit = hostedAudioUnit?.auAudioUnit else {
            fail("The plugin loaded without an AU instance.")
            return
        }
        let hostSelf = self
        audioUnit.requestViewController(completionHandler: { controller in
            DispatchQueue.main.async {
                let resolvedController = controller ?? hostSelf.makeFallbackViewController(for: audioUnit)
                guard let resolvedController else {
                    hostSelf.fail("This plugin does not expose a macOS editor window to the built-in host.")
                    return
                }
                hostSelf.presentEditorWindow(with: resolvedController)
            }
        })
    }

    private func presentEditorWindow(with controller: NSViewController) {
        let container = PluginContainerViewController(embeddedController: controller)
        hostedEditorController = controller
        let initialSize = preferredWindowSize(for: controller)
        reportWindowSize(initialSize)
        
        // Account for Retina display scale factor
        let scaleFactor = NSScreen.main?.backingScaleFactor ?? 1.0
        let scaledSize = NSSize(
            width: initialSize.width / scaleFactor,
            height: initialSize.height / scaleFactor
        )
        
        let window = NSWindow(contentViewController: container)
        window.title = componentName
        window.setContentSize(scaledSize)
        window.minSize = NSSize(width: 400, height: 280)
        window.styleMask.insert(.resizable)
        window.isReleasedWhenClosed = false
        window.delegate = self
        
        if case .embedded = mode {
            window.styleMask.remove(.titled)
            window.styleMask.remove(.closable)
            window.styleMask.remove(.miniaturizable)
            window.styleMask.insert(.borderless)
            window.level = .floating
            window.isOpaque = true
            window.hasShadow = true
            // Prevent window from stealing focus
            window.hidesOnDeactivate = false
            window.makeKeyAndOrderFront(nil)
        } else {
            window.center()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            window.makeFirstResponder(controller.view)
        }
        self.window = window
    }

    func windowWillClose(_ notification: Notification) {
        if let closingWindow = notification.object as? NSWindow, closingWindow === window {
            window = nil
        }
    }

    private func makeFallbackViewController(for audioUnit: AUAudioUnit) -> NSViewController? {
        if #available(macOS 13.0, *) {
            let controller = AUGenericViewController()
            controller.auAudioUnit = audioUnit
            return controller
        }
        return nil
    }

    private func preferredWindowSize(for controller: NSViewController) -> NSSize {
        let preferred = controller.preferredContentSize
        if preferred.width > 64, preferred.height > 64 {
            return preferred
        }
        controller.loadView()
        let fitting = controller.view.fittingSize
        if fitting.width > 64, fitting.height > 64 {
            return NSSize(width: min(max(fitting.width, 640), 1400), height: min(max(fitting.height, 360), 960))
        }
        return NSSize(width: 960, height: 640)
    }

    private func startCommandReader() {
        FileHandle.standardInput.readabilityHandler = { [weak self] handle in
            guard let self else { return }
            let data = handle.availableData
            if data.isEmpty {
                return
            }
            guard let chunk = String(data: data, encoding: .utf8) else {
                return
            }
            self.stdinBuffer += chunk
            let lines = self.stdinBuffer.components(separatedBy: .newlines)
            self.stdinBuffer = lines.last ?? ""
            lines.dropLast().forEach { line in
                self.handleCommand(line.trimmingCharacters(in: .whitespacesAndNewlines))
            }
        }
    }

    private func handleCommand(_ command: String) {
        guard !command.isEmpty else {
            return
        }
        if command == "OPEN" || command == "FOCUS" {
            DispatchQueue.main.async {
                self.openOrFocusWindow()
            }
            return
        }
        if command == "QUIT" {
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
            return
        }
        if command.hasPrefix("NOTE_ON ") {
            let parts = command.split(separator: " ")
            guard parts.count >= 3, let midi = UInt8(parts[1]), let velocity = UInt8(parts[2]) else {
                printErrorLine("Could not parse NOTE_ON command.")
                return
            }
            triggerNoteOn(midi: midi, velocity: velocity)
            return
        }
        if command.hasPrefix("NOTE_OFF ") {
            let parts = command.split(separator: " ")
            guard parts.count >= 2, let midi = UInt8(parts[1]) else {
                printErrorLine("Could not parse NOTE_OFF command.")
                return
            }
            triggerNoteOff(midi: midi)
            return
        }
    }

    private func triggerNoteOn(midi: UInt8, velocity: UInt8) {
        guard let hostedAudioUnit else {
            printErrorLine("The plugin instrument is not ready yet.")
            return
        }
        guard audioEngine != nil else {
            printErrorLine("The audio engine is not running.")
            return
        }
        
        if let instrument = hostedAudioUnit as? AVAudioUnitMIDIInstrument {
            instrument.startNote(midi, withVelocity: velocity, onChannel: 0)
            printLine("NOTE_ON \(midi) \(velocity)")
            return
        }
        
        if let midiBlock = hostedAudioUnit.auAudioUnit.scheduleMIDIEventBlock {
            midiBlock(AUEventSampleTimeImmediate, 0, 3, [0x90, midi, velocity])
            printLine("NOTE_ON \(midi) \(velocity)")
            return
        }
        printErrorLine("This plugin does not accept MIDI note input from the host.")
    }

    private func triggerNoteOff(midi: UInt8) {
        guard let hostedAudioUnit else {
            return
        }
        guard audioEngine != nil else {
            return
        }
        
        if let instrument = hostedAudioUnit as? AVAudioUnitMIDIInstrument {
            instrument.stopNote(midi, onChannel: 0)
            printLine("NOTE_OFF \(midi)")
            return
        }
        
        if let midiBlock = hostedAudioUnit.auAudioUnit.scheduleMIDIEventBlock {
            midiBlock(AUEventSampleTimeImmediate, 0, 3, [0x80, midi, 0])
            printLine("NOTE_OFF \(midi)")
            return
        }
    }

    private func fail(_ message: String) {
        if case .server = mode {
            printErrorLine(message)
            return
        }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Plugin Window"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

struct ParsedArguments {
    let mode: HostMode
    let pluginURL: URL
}

func parseArguments() -> ParsedArguments? {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard !arguments.isEmpty else {
        return nil
    }

    var serveMode = false
    var showOnLaunch = false
    var embeddedWindowId: String?
    var pluginPath: String?

    var index = 0
    while index < arguments.count {
        let argument = arguments[index]
        if argument == "--serve" {
            serveMode = true
            index += 1
            continue
        }
        if argument == "--show" {
            showOnLaunch = true
            index += 1
            continue
        }
        if argument == "--embed" {
            if index + 1 < arguments.count {
                embeddedWindowId = arguments[index + 1]
                index += 2
                continue
            }
        }
        pluginPath = argument
        index += 1
    }

    guard let pluginPath else {
        return nil
    }
    
    let mode: HostMode
    if let windowId = embeddedWindowId {
        mode = .embedded(windowId: windowId)
    } else if serveMode {
        mode = .server(showOnLaunch: showOnLaunch)
    } else {
        mode = .editorOnce
    }
    
    return ParsedArguments(
        mode: mode,
        pluginURL: URL(fileURLWithPath: pluginPath)
    )
}

guard let parsedArguments = parseArguments() else {
    fputs("Usage: au-host [--serve] [--show] <plugin.component>\n", stderr)
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = HostDelegate(pluginURL: parsedArguments.pluginURL, mode: parsedArguments.mode)
app.delegate = delegate
app.run()

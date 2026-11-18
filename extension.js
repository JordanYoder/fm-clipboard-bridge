const vscode = require('vscode');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const REQUEST_TIMEOUT_MS = 15000;
const READY_TIMEOUT_MS = 20000;

// Text that starts like a FileMaker clipboard object (optionally preceded by
// an XML declaration or comments) and ends with a closing tag.
const FM_XML_RE =
  /^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--[\s\S]*?-->\s*)*<(?:fmxmlsnippet|Step|Script|Group|CustomFunction|Field|BaseTable|ValueList|Layout|Object)\b[\s\S]*>\s*$/;

/** No usable helper (exe blocked/missing and no working Python). */
class BridgeUnavailableError extends Error {}

let log; // OutputChannel

function getRunnerCandidates(extensionPath) {
  const candidates = [];
  const exePath = path.join(extensionPath, 'fm_clip_bridge.exe');
  if (fs.existsSync(exePath)) {
    candidates.push({ label: 'bundled fm_clip_bridge.exe', cmd: exePath, baseArgs: [] });
  }

  const pyScript = path.join(extensionPath, 'fm_clip_bridge.py');
  const configured = (vscode.workspace.getConfiguration('fmBridge').get('pythonPath') || '').trim();
  if (configured) {
    candidates.push({ label: configured, cmd: configured, baseArgs: [pyScript] });
  }
  // The `py` launcher ships with python.org installers even when "Add to PATH"
  // was unchecked, so it often works where `python` does not.
  for (const c of [
    { cmd: 'python', pre: [] },
    { cmd: 'py', pre: ['-3'] },
    { cmd: 'python3', pre: [] }
  ]) {
    if (c.cmd !== configured) {
      candidates.push({ label: [c.cmd, ...c.pre].join(' '), cmd: c.cmd, baseArgs: [...c.pre, pyScript] });
    }
  }
  return candidates;
}

/**
 * Keeps one fm_clip_bridge process running in `serve` mode and talks to it
 * over JSON lines. Starting the single-file exe costs ~1 s (it unpacks itself
 * each launch), so paying that once instead of per operation is what makes
 * smart copy and convert-on-focus feel instant.
 */
class BridgeHelper {
  constructor(extensionPath) {
    this.extensionPath = extensionPath;
    this.proc = null;
    this.starting = null;
    this.pending = new Map();
    this.nextId = 1;
  }

  request(op, payload = {}) {
    if (process.platform !== 'win32') {
      return Promise.reject(new Error('FileMaker Clipboard Bridge only works on Windows.'));
    }
    return this.ensureStarted().then(
      (proc) =>
        new Promise((resolve, reject) => {
          const id = this.nextId++;
          const timer = setTimeout(() => {
            this.pending.delete(id);
            reject(new Error(`The FileMaker bridge did not respond (${op}). It will restart on the next try.`));
            this.stop();
          }, REQUEST_TIMEOUT_MS);
          this.pending.set(id, { resolve, reject, timer });
          proc.stdin.write(JSON.stringify({ id, op, ...payload }) + '\n');
        })
    );
  }

  ensureStarted() {
    if (this.proc) return Promise.resolve(this.proc);
    if (!this.starting) {
      this.starting = this.start().finally(() => {
        this.starting = null;
      });
    }
    return this.starting;
  }

  async start() {
    const failures = [];
    for (const runner of getRunnerCandidates(this.extensionPath)) {
      try {
        const proc = await this.spawnRunner(runner);
        log.appendLine(`Bridge started: ${runner.label}`);
        this.proc = proc;
        return proc;
      } catch (err) {
        log.appendLine(`Bridge candidate failed: ${err.message}`);
        failures.push(err.message);
      }
    }
    throw new BridgeUnavailableError(
      'Could not start the FileMaker bridge. Install Python 3.7+ from python.org, ' +
        'or set "fmBridge.pythonPath" to your python.exe. Details: ' +
        failures.join(' | ')
    );
  }

  spawnRunner(runner) {
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = spawn(runner.cmd, [...runner.baseArgs, 'serve'], {
          windowsHide: true,
          stdio: ['pipe', 'pipe', 'pipe']
        });
      } catch (err) {
        reject(new Error(`${runner.label}: ${err.message}`));
        return;
      }

      let ready = false;
      let settled = false;
      let stderr = '';
      let buffer = '';

      const fail = (reason) => {
        if (settled) return;
        settled = true;
        clearTimeout(readyTimer);
        try {
          child.kill();
        } catch (_) {
          /* already gone */
        }
        const detail = stderr.trim().split(/\r?\n/).slice(-2).join(' ');
        reject(new Error(`${runner.label}: ${reason}${detail ? ` (${detail})` : ''}`));
      };
      const readyTimer = setTimeout(() => fail('did not start in time'), READY_TIMEOUT_MS);

      child.on('error', (err) => fail(err.code === 'ENOENT' ? 'not found' : err.message));
      child.stdin.on('error', () => {}); // EPIPE if it dies; reported via 'exit'
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (d) => {
        stderr = (stderr + d).slice(-4000);
      });

      child.on('exit', (code) => {
        if (!ready) {
          // 9009 = Windows "python" alias that only opens the Microsoft Store
          fail(code === 9009 ? 'Microsoft Store placeholder, not a real Python' : `exited with code ${code}`);
          return;
        }
        log.appendLine(`Bridge exited (code ${code}). ${stderr.trim()}`);
        if (this.proc === child) this.proc = null;
        this.rejectAll(new Error('The FileMaker bridge stopped unexpectedly. Try again.'));
      });

      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        buffer += chunk;
        let nl;
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (!line) continue;
          let msg;
          try {
            msg = JSON.parse(line);
          } catch (_) {
            if (!ready) fail(`unexpected output: ${line.slice(0, 200)}`);
            else log.appendLine(`Bridge: unparseable output: ${line.slice(0, 200)}`);
            continue;
          }
          if (!ready) {
            if (!msg.ready) {
              fail('unexpected handshake');
              return;
            }
            ready = true;
            settled = true;
            clearTimeout(readyTimer);
            resolve(child);
            continue;
          }
          this.handleResponse(msg);
        }
      });
    });
  }

  handleResponse(msg) {
    const entry = this.pending.get(msg.id);
    if (!entry) return;
    this.pending.delete(msg.id);
    clearTimeout(entry.timer);
    if (msg.ok) entry.resolve(msg.result);
    else entry.reject(new Error(msg.error));
  }

  rejectAll(err) {
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this.pending.clear();
  }

  stop() {
    const proc = this.proc;
    this.proc = null;
    if (proc) {
      try {
        proc.stdin.end(); // serve loop exits on EOF
        proc.kill();
      } catch (_) {
        /* already gone */
      }
    }
    this.rejectAll(new Error('The FileMaker bridge was restarted.'));
  }

  dispose() {
    this.stop();
  }
}

/**
 * Read-only "FileMaker view" of an XML document, served as a virtual
 * document (scheme fmscript:) that re-renders as the XML is edited.
 * The view URI carries the source document's URI in its query.
 */
const VIEW_SCHEME = 'fmscript';
const VIEW_LANG = 'fmscript';

class ReadableViews {
  constructor(helper) {
    this.helper = helper;
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
    this.state = new Map(); // view uri string -> { map: number[], headerLines: number, lastGood }
    this.timers = new Map();
  }

  static viewUriFor(sourceUri) {
    const base = path.basename(sourceUri.path) || 'Untitled';
    return vscode.Uri.from({
      scheme: VIEW_SCHEME,
      path: `/${base} (FileMaker view)`,
      query: encodeURIComponent(sourceUri.toString())
    });
  }

  static sourceUriFor(viewUri) {
    return vscode.Uri.parse(decodeURIComponent(viewUri.query));
  }

  async provideTextDocumentContent(viewUri) {
    const key = viewUri.toString();
    const prev = this.state.get(key);
    const sourceUri = ReadableViews.sourceUriFor(viewUri);
    const source = vscode.workspace.textDocuments.find((d) => d.uri.toString() === sourceUri.toString());
    if (!source) {
      const note = '⚠ The XML tab for this view was closed or renamed. Close this view and open it again from the XML.';
      return prev && prev.lastGood ? `${note}\n\n${prev.lastGood.lines.join('\n')}` : note;
    }
    try {
      const r = await this.helper.request('render', { text: source.getText() });
      this.state.set(key, { map: r.map, headerLines: 0, lastGood: r });
      return r.lines.join('\n');
    } catch (err) {
      const header = [`⚠ ${err.message}`];
      if (prev && prev.lastGood) {
        header.push('(Showing the last readable version until the XML is fixed.)', '');
        this.state.set(key, { map: prev.lastGood.map, headerLines: header.length, lastGood: prev.lastGood });
        return [...header, ...prev.lastGood.lines].join('\n');
      }
      this.state.set(key, { map: [], headerLines: header.length, lastGood: null });
      return header.join('\n');
    }
  }

  /** Re-render open views of a source document, debounced while typing. */
  sourceChanged(sourceUri) {
    const viewUri = ReadableViews.viewUriFor(sourceUri);
    const key = viewUri.toString();
    if (!vscode.workspace.textDocuments.some((d) => d.uri.toString() === key)) return;
    clearTimeout(this.timers.get(key));
    this.timers.set(
      key,
      setTimeout(() => {
        this.timers.delete(key);
        this.emitter.fire(viewUri);
      }, 250)
    );
  }

  /** XML line (0-based) -> view line (0-based): first view line of the nearest step at/above it. */
  viewLineFor(viewUri, xmlLine) {
    const st = this.state.get(viewUri.toString());
    if (!st || !st.map.length) return 0;
    const target = xmlLine + 1;
    let best = 0;
    let bestSrc = -1;
    st.map.forEach((src, i) => {
      if (src <= target && src > bestSrc) {
        bestSrc = src;
        best = i;
      }
    });
    return best + st.headerLines;
  }

  /** View line (0-based) -> XML line (0-based). */
  xmlLineFor(viewUri, viewLine) {
    const st = this.state.get(viewUri.toString());
    if (!st || !st.map.length) return 0;
    const i = Math.min(Math.max(viewLine - st.headerLines, 0), st.map.length - 1);
    return Math.max(st.map[i] - 1, 0);
  }

  forget(viewUri) {
    this.state.delete(viewUri.toString());
  }

  dispose() {
    this.timers.forEach(clearTimeout);
    this.emitter.dispose();
  }
}

function revealLine(editor, line) {
  const pos = new vscode.Position(Math.min(line, editor.document.lineCount - 1), 0);
  editor.selection = new vscode.Selection(pos, pos);
  editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
}

async function reportError(prefix, err) {
  log.appendLine(`${prefix}: ${err.message}`);
  if (err instanceof BridgeUnavailableError) {
    const choice = await vscode.window.showErrorMessage(`${prefix}: ${err.message}`, 'Open Settings');
    if (choice === 'Open Settings') {
      vscode.commands.executeCommand('workbench.action.openSettings', 'fmBridge.pythonPath');
    }
  } else {
    vscode.window.showErrorMessage(`${prefix}: ${err.message}`);
  }
}

function activate(context) {
  log = vscode.window.createOutputChannel('FileMaker Clipboard Bridge');
  const helper = new BridgeHelper(context.extensionPath);
  const config = () => vscode.workspace.getConfiguration('fmBridge');

  // Pull/Push stay available as fallbacks (Command Palette, Ctrl+Alt+T / Ctrl+Alt+F);
  // day-to-day use is plain Ctrl+C / Ctrl+V.
  const pullCmd = vscode.commands.registerCommand('fmBridge.pull', async () => {
    try {
      const xmlOut = await helper.request('pull');
      const doc = await vscode.workspace.openTextDocument({ content: xmlOut + '\n', language: 'xml' });
      await vscode.window.showTextDocument(doc);
      vscode.window.setStatusBarMessage('$(check) Pulled FileMaker object from clipboard', 4000);
    } catch (err) {
      reportError('FM Pull Failed', err);
    }
  });

  const pushCmd = vscode.commands.registerCommand('fmBridge.push', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showWarningMessage('Open an XML snippet in the editor first.');
      return;
    }
    const xmlText = editor.selection.isEmpty
      ? editor.document.getText()
      : editor.document.getText(editor.selection);
    if (!xmlText.trim()) {
      vscode.window.showWarningMessage('Editor or selection is empty.');
      return;
    }
    try {
      const r = await helper.request('push', { text: xmlText });
      const msg = `Copied to FileMaker clipboard as ${r.label}. Ready to paste in FileMaker!`;
      vscode.window.setStatusBarMessage(`$(check) ${msg}`, 4000);
      vscode.window.showInformationMessage(msg);
    } catch (err) {
      reportError('FM Push Failed', err);
    }
  });

  // Ctrl+C: when exactly one selection holds FileMaker XML, copy it in
  // FileMaker's format (plus plain text); everything else is a normal copy.
  const smartCopyCmd = vscode.commands.registerCommand('fmBridge.smartCopy', async () => {
    const defaultCopy = () => vscode.commands.executeCommand('editor.action.clipboardCopyAction');
    const editor = vscode.window.activeTextEditor;
    if (
      process.platform !== 'win32' ||
      !editor ||
      editor.selections.length !== 1 ||
      editor.selection.isEmpty
    ) {
      return defaultCopy();
    }
    const text = editor.document.getText(editor.selection);
    if (!FM_XML_RE.test(text)) {
      return defaultCopy();
    }
    try {
      const r = await helper.request('push', { text });
      vscode.window.setStatusBarMessage(`$(check) Copied as FileMaker ${r.label}. Paste in FileMaker with Ctrl+V.`, 4000);
    } catch (err) {
      await defaultCopy();
      log.appendLine(`Smart copy fell back to plain text: ${err.message}`);
      vscode.window.setStatusBarMessage(`$(warning) Copied as plain text only. Not FileMaker-ready: ${err.message}`, 8000);
    }
  });

  // When VS Code regains focus (typically right after copying in FileMaker),
  // add the XML as plain text so the normal Ctrl+V pastes it.
  const convertOnFocus = async () => {
    if (process.platform !== 'win32' || !config().get('convertOnFocus', true)) return;
    try {
      const r = await helper.request('convert');
      if (r.converted) {
        vscode.window.setStatusBarMessage(`$(clippy) FileMaker ${r.label} ready. Ctrl+V pastes it as XML.`, 5000);
      }
    } catch (err) {
      log.appendLine(`Convert on focus failed: ${err.message}`);
    }
  };
  const focusListener = vscode.window.onDidChangeWindowState((state) => {
    if (state.focused) convertOnFocus();
  });

  // ---- Readable "FileMaker view" ------------------------------------------
  const views = new ReadableViews(helper);
  const viewProvider = vscode.workspace.registerTextDocumentContentProvider(VIEW_SCHEME, views);

  const openView = async (beside) => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme === VIEW_SCHEME) return;
    const viewUri = ReadableViews.viewUriFor(editor.document.uri);
    try {
      let doc = await vscode.workspace.openTextDocument(viewUri);
      if (doc.languageId !== VIEW_LANG) doc = await vscode.languages.setTextDocumentLanguage(doc, VIEW_LANG);
      views.emitter.fire(viewUri); // make sure it reflects the latest XML
      const viewEditor = await vscode.window.showTextDocument(doc, {
        viewColumn: beside ? vscode.ViewColumn.Beside : editor.viewColumn,
        preserveFocus: beside,
        preview: false
      });
      // Give the refresh a moment, then put the cursor on the same step.
      setTimeout(() => revealLine(viewEditor, views.viewLineFor(viewUri, editor.selection.active.line)), 150);
    } catch (err) {
      reportError('FileMaker view failed', err);
    }
  };

  const showViewCmd = vscode.commands.registerCommand('fmBridge.showReadable', () => openView(false));
  const showViewBesideCmd = vscode.commands.registerCommand('fmBridge.showReadableBeside', () => openView(true));

  const showXmlCmd = vscode.commands.registerCommand('fmBridge.showXml', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== VIEW_SCHEME) return;
    const viewUri = editor.document.uri;
    const sourceUri = ReadableViews.sourceUriFor(viewUri);
    const source = vscode.workspace.textDocuments.find((d) => d.uri.toString() === sourceUri.toString());
    if (!source) {
      vscode.window.showWarningMessage('The XML tab for this view is no longer open.');
      return;
    }
    const xmlEditor = await vscode.window.showTextDocument(source, { viewColumn: editor.viewColumn, preview: false });
    revealLine(xmlEditor, views.xmlLineFor(viewUri, editor.selection.active.line));
  });

  const docChangeListener = vscode.workspace.onDidChangeTextDocument((e) => {
    if (e.document.uri.scheme !== VIEW_SCHEME) views.sourceChanged(e.document.uri);
  });
  const docCloseListener = vscode.workspace.onDidCloseTextDocument((doc) => {
    if (doc.uri.scheme === VIEW_SCHEME) views.forget(doc.uri);
    else views.sourceChanged(doc.uri); // view shows "XML tab was closed"
  });

  const configListener = vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration('fmBridge.pythonPath')) helper.stop(); // restart with the new path on next use
  });

  context.subscriptions.push(
    log,
    helper,
    pullCmd,
    pushCmd,
    smartCopyCmd,
    focusListener,
    configListener,
    views,
    viewProvider,
    showViewCmd,
    showViewBesideCmd,
    showXmlCmd,
    docChangeListener,
    docCloseListener
  );

  // Warm up the helper in the background so the first copy/paste is instant.
  if (process.platform === 'win32') {
    helper
      .ensureStarted()
      .then(() => {
        if (vscode.window.state.focused) convertOnFocus();
      })
      .catch((err) => log.appendLine(err.message));
  }
}

function deactivate() {}

module.exports = { activate, deactivate };

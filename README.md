# FileMaker Clipboard Bridge

**Edit FileMaker scripts in VS Code with plain copy and paste.**

Copy scripts, script steps, custom functions, fields, tables, value lists and layout objects out of FileMaker Pro, then paste them into VS Code as clean, indented XML. Edit them with everything VS Code offers (search and replace, multi-cursor editing, diffing, Git, snippets) and paste them straight back into FileMaker. Ctrl+C and Ctrl+V work in both directions.

Prefer to *read* a script rather than its XML? One click switches to a **FileMaker Script view** that shows the steps the way FileMaker's Script Workspace does.

> **Windows only.** Nothing else to install: the clipboard helper is bundled with the extension.

---

## Why

FileMaker keeps copied scripts and steps on the clipboard in its own private format. Paste them into a text editor and you get nothing useful. Paste text back into FileMaker and it isn't recognized as script steps. This extension translates between the two automatically, so FileMaker objects become ordinary text you can work with, and that text becomes FileMaker objects again.

Typical uses:

- Find and replace across a long script (rename a variable, a field or a layout everywhere)
- Duplicate and adapt blocks of steps with multi-cursor editing
- Keep scripts in Git and diff versions of a script
- Build a personal library of reusable steps and custom functions
- Review or share a script as readable text

---

## Features

### Copy and paste in both directions

**FileMaker → VS Code**
1. Copy anything in FileMaker (Ctrl+C): a script, a few script steps, a custom function, a field…
2. Switch to VS Code. A short status bar message confirms that the FileMaker data is ready.
3. Press **Ctrl+V**. It pastes as formatted XML.

**VS Code → FileMaker**
1. Select the XML: a whole snippet, or just a few `<Step>` elements.
2. Press **Ctrl+C**.
3. Switch to FileMaker and press **Ctrl+V**.

Ctrl+C only changes behavior when the selection is FileMaker XML. Copying anything else in VS Code works exactly as before. If the selected XML isn't valid, it is still copied as plain text and the status bar explains why FileMaker won't accept it.

### FileMaker Script view

Click the **eye button** at the top right of any XML tab to see the script without the XML, as FileMaker shows it:

```
━━ Script: Process Invoices ━━
# Purpose: process all open invoices
# Author: team
Set Error Capture [ On ]
Set Variable [ $param ; Value: Get ( ScriptParameter ) ]
Go to Layout [ "Invoices" ]

Loop [ Flush: Always ]
    If [ Invoices::Status = "Open" and
          $param ≠ "" ]
        Set Field [ Invoices::Status ; "Processed" ]
        // Perform Script [ "Send Email" ; Parameter: $param ]
    Else If [ IsEmpty ( Invoices::Status ) ]
        Show Custom Dialog [ "Warning" ; "Missing status" ]
    End If
    Go to Record/Request/Page [ With dialog: Off ; Exit after last: On ; Next ]
End Loop
Commit Records/Requests [ With dialog: Off ; Option: Off ]
Exit Script [ Text Result: True ]
```

- **One-click toggle.** The eye button shows the script view, and the code button switches back to the XML.
- **The cursor stays on the same step** when you switch, in both directions.
- **Side by side.** Alt+click the eye button to keep the XML and the script view open next to each other.
- **Live.** The view updates as you edit the XML. While the XML has an error, the view shows the error with its line number and keeps showing the last readable version.
- **Syntax coloring** for step names, control steps (`If`, `Loop`, `Exit Script`…), comments, `$variables` and `$$globals`, `Table::Field` references, strings and option labels.
- **Block indentation** for `If` / `Else If` / `Else` / `End If`, `Loop` / `End Loop` and transactions.
- **Disabled steps** are shown as `// …`, and multi-line comments and calculations keep their line breaks.
- **Works with anything you paste:** a single step, a whole script, several scripts, script folders and custom functions. Fields, tables and value lists get a readable summary.
- **Read-only**, so you always edit the XML and nothing gets out of sync.

### Your code stays exactly as written

Pasted XML is neatly indented, but only the XML structure is reformatted. **Calculations, comments and text keep every line break and space**, so what you paste back into FileMaker is exactly what you copied out. Reformatting a document twice gives the same result.

### Smart about what you paste

- **Partial selections work.** Copy just a few `<Step>` elements and they're wrapped in the `<fmxmlsnippet>` FileMaker expects automatically. Plain-text paste still gives you exactly what you selected.
- **Automatic object detection.** Scripts, script steps, custom functions, fields, tables, value lists and layout objects are each put on the clipboard in the right FileMaker format.
- **Clear errors.** Invalid XML gives a message with the line and column, counted from your selection. Unsupported objects are reported instead of being pasted as something they aren't.

### Fast and unobtrusive

- A small helper starts in the background when VS Code opens, so copying and pasting take milliseconds after that.
- It retries automatically when another app (such as a clipboard manager or Windows clipboard history) briefly holds the clipboard.
- It restarts itself if it stops, and closes when VS Code closes.
- **Fully local:** the extension makes no network requests. Your scripts never leave your computer.

---

## Getting started

1. Install the extension.
2. In FileMaker, open a script and copy a few steps (Ctrl+C).
3. In VS Code, open a new file (Ctrl+N) and press **Ctrl+V**.
4. Click the **eye button** to see it as a FileMaker script, then click the **code button** to go back.
5. Edit the XML, select it, press **Ctrl+C**, and paste it into FileMaker with **Ctrl+V**.

Tip: set the new file's language to XML (bottom right of the status bar) to get XML highlighting and the eye button.

---

## Commands and shortcuts

Everyday use only needs Ctrl+C and Ctrl+V. These are available as well:

| Command | Shortcut | What it does |
|---|---|---|
| FileMaker: Show as FileMaker Script | eye button | Switches to the readable script view. |
| FileMaker: Show as FileMaker Script (Side by Side) | Alt+click eye button | Opens the script view next to the XML. |
| FileMaker: Show XML | code button | Switches from the script view back to the XML. |
| FileMaker: Pull from FileMaker Clipboard | Ctrl+Alt+T | Opens the FileMaker clipboard object as XML in a new tab. A fallback if a paste doesn't come through. |
| FileMaker: Push to FileMaker Clipboard | Ctrl+Alt+F | Copies the whole tab, or the selection, for FileMaker. A fallback for Ctrl+C. |

All commands are in the Command Palette (Ctrl+Shift+P → "FileMaker").

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `fmBridge.smartCopy` | `true` | Ctrl+C on a selection of FileMaker XML also copies it in FileMaker's format. Other copies are unaffected. |
| `fmBridge.convertOnFocus` | `true` | When VS Code regains focus and FileMaker data is on the clipboard, add its XML as text so a normal Ctrl+V pastes it. FileMaker's own data stays on the clipboard. |
| `fmBridge.pythonPath` | *(empty)* | Optional. Only used if the bundled helper is missing or blocked: the path to a Python 3.7+ executable. Empty means auto-detect. |

---

## Requirements

- Windows 10 or 11
- VS Code 1.80 or newer
- FileMaker Pro

Works in Remote-SSH, WSL and Dev Container sessions too. The extension always runs on your local Windows machine, where the clipboard is.

---

## Troubleshooting

- **Ctrl+V pastes nothing or old text after copying in FileMaker.** You probably pasted a moment too soon after switching windows. Press Ctrl+V again, or use **Ctrl+Alt+T**.
- **"Could not start the FileMaker bridge".** Antivirus software may have blocked the bundled helper (`fm_clip_bridge.exe`). Allow it, or install Python 3.7+ from python.org and the extension will use that instead.
- **FileMaker doesn't paste after Ctrl+C in VS Code.** Check the status bar message: the XML may have an error. Make sure the selection includes complete elements.
- **Ctrl+C conflicts with another extension (for example Vim).** Set `fmBridge.smartCopy` to `false` and use **Ctrl+Alt+F** instead.
- **Details and logs:** **View → Output**, then choose **FileMaker Clipboard Bridge** in the drop-down.

---

## Known limitations

- **Windows only.** The extension reads FileMaker's Windows clipboard formats.
- **The script view approximates FileMaker's display.** Common steps (Set Variable, Set Field, If, Else If, Exit Loop If, Perform Script, Perform Script on Server, Exit Script, Go to Layout, Show Custom Dialog) are formatted like FileMaker. Other steps list their options generically.
- **Layout objects** can be copied and pasted, but the script view only summarizes them.
- With FileMaker data on the clipboard, Windows clipboard history (Win+V) may show the XML text version too.

---

## Building from source

Requires Windows, Python 3.7+ and Node.js.

1. Build the clipboard helper exe (optional; without it the extension runs `fm_clip_bridge.py` with Python):

   ```
   pip install pyinstaller
   pyinstaller --onefile fm_clip_bridge.py
   ```

   Copy `distm_clip_bridge.exe` into the project folder.

2. Package the extension:

   ```
   npx @vscode/vsce package
   ```

   This creates `fm-clipboard-bridge-<version>.vsix`. Install it with **Extensions → ⋯ → Install from VSIX...**

---

## License

Public domain. No copyright, no conditions: copy, modify, sell, or do anything else with it, with or without credit. See [LICENSE](LICENSE) (The Unlicense).

The software is provided as is, with no warranty of any kind.

---

*FileMaker is a trademark of Claris International Inc., registered in the U.S. and other countries. This extension is not affiliated with or endorsed by Claris.*

"""Bridge between FileMaker's private clipboard formats and plain-text XML.

FileMaker (on Windows too) stores copied objects under registered clipboard
formats named "Mac-XM??". Each payload is a 4-byte little-endian length
followed by UTF-8 XML.

Usage:
    fm_clip_bridge pull                 print FM clipboard object as XML
    fm_clip_bridge push --stdin|<file>  put XML on the clipboard as an FM object
    fm_clip_bridge convert              add a plain-text XML copy next to an FM object
    fm_clip_bridge serve                long-running JSON-lines helper used by VS Code
"""
from __future__ import annotations  # keeps `tuple[str, str]` hints working on Python 3.7/3.8

import json
import struct
import sys
import time
import ctypes
from ctypes import wintypes
import xml.dom.minidom as minidom
from xml.dom import Node
import xml.etree.ElementTree as ET

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

FM_FORMATS = {
    "Mac-XMSS": "Script Steps",
    "Mac-XMSC": "Script",
    "Mac-XMFN": "Custom Function",
    "Mac-XMFD": "Field",
    "Mac-XMTB": "Table",
    "Mac-XMVL": "Value List",
    "Mac-XML2": "Layout Object",
}

# Top-level tags inside <fmxmlsnippet> -> clipboard format, checked in order.
TAG_FORMATS = (
    (("Script", "Group"), "Mac-XMSC"),
    (("Step",), "Mac-XMSS"),
    (("CustomFunction",), "Mac-XMFN"),
    (("Field",), "Mac-XMFD"),
    (("BaseTable",), "Mac-XMTB"),
    (("ValueList",), "Mac-XMVL"),
    (("Layout", "Object"), "Mac-XML2"),
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t

# Clipboard sequence numbers this process produced or already inspected, so
# `convert` never rewrites our own data or re-checks an unchanged clipboard.
_last_own_seq = None
_last_checked_seq = None


# --------------------------------------------------------------------------
# Clipboard primitives
# --------------------------------------------------------------------------

class ClipboardContext:
    """Opens the clipboard, retrying briefly: clipboard managers and Windows
    clipboard history often hold it for a few milliseconds."""

    def __init__(self, attempts: int = 20, delay: float = 0.025):
        self.attempts = attempts
        self.delay = delay

    def __enter__(self):
        for _ in range(self.attempts):
            if user32.OpenClipboard(None):
                return self
            time.sleep(self.delay)
        raise RuntimeError(
            "Unable to open the Windows clipboard: another app is using it. Try again.")

    def __exit__(self, exc_type, exc_val, exc_tb):
        user32.CloseClipboard()


def _set_raw_clipboard_bytes(fmt_id: int, payload: bytes):
    """Clipboard must be open. Ownership of the memory passes to Windows only
    if SetClipboardData succeeds; otherwise we free it ourselves."""
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(payload), 1))
    if not h_mem:
        raise MemoryError("GlobalAlloc failed.")
    try:
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            raise MemoryError("GlobalLock failed.")
        try:
            ctypes.memmove(ptr, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(h_mem)
        if not user32.SetClipboardData(fmt_id, h_mem):
            raise RuntimeError(f"SetClipboardData failed for format ID {fmt_id}.")
    except Exception:
        kernel32.GlobalFree(h_mem)
        raise


def _set_plain_text(text: str):
    """Clipboard must be open."""
    _set_raw_clipboard_bytes(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")


def _fm_format_available() -> bool:
    # IsClipboardFormatAvailable does not need the clipboard to be open,
    # so this check never blocks or locks out other apps.
    return any(user32.IsClipboardFormatAvailable(user32.RegisterClipboardFormatW(name))
               for name in FM_FORMATS)


def _read_fm_xml() -> tuple[str, str] | None:
    """Clipboard must be open. Returns (xml_text, format_name) or None."""
    for fmt_name in FM_FORMATS:
        fmt_id = user32.RegisterClipboardFormatW(fmt_name)
        if not user32.IsClipboardFormatAvailable(fmt_id):
            continue
        h_mem = user32.GetClipboardData(fmt_id)
        if not h_mem:
            continue
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            continue
        try:
            total_size = kernel32.GlobalSize(h_mem)
            if total_size <= 4:
                continue
            raw_bytes = ctypes.string_at(ptr, total_size)
        finally:
            kernel32.GlobalUnlock(h_mem)

        xml_length = struct.unpack("<I", raw_bytes[:4])[0]
        xml_bytes = raw_bytes[4: 4 + xml_length]
        return xml_bytes.decode("utf-8", errors="replace").strip(), fmt_name
    return None


# --------------------------------------------------------------------------
# Lossless pretty printing
# --------------------------------------------------------------------------
# Only elements that contain child elements and no real text are re-indented.
# Elements holding text or CDATA (calculations, comments, ...) are written
# back exactly as parsed, so blank lines and spacing inside them survive.

def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _esc_attr(s: str) -> str:
    return (_esc_text(s).replace('"', "&quot;")
            .replace("\n", "&#10;").replace("\t", "&#9;"))


def _start_tag(el) -> str:
    attrs = "".join(f' {name}="{_esc_attr(value)}"' for name, value in el.attributes.items())
    return f"<{el.tagName}{attrs}>"


def _inline(node) -> str:
    t = node.nodeType
    if t == Node.ELEMENT_NODE:
        start = _start_tag(node)
        if not node.childNodes:
            return start[:-1] + "/>"
        return start + "".join(_inline(c) for c in node.childNodes) + f"</{node.tagName}>"
    if t == Node.TEXT_NODE:
        return _esc_text(node.data)
    if t == Node.CDATA_SECTION_NODE:
        return "<![CDATA[" + node.data.replace("]]>", "]]]]><![CDATA[>") + "]]>"
    if t == Node.COMMENT_NODE:
        return f"<!--{node.data}-->"
    if t == Node.PROCESSING_INSTRUCTION_NODE:
        return f"<?{node.target} {node.data}?>"
    return node.toxml()


def _is_blank_text(node) -> bool:
    return node.nodeType == Node.TEXT_NODE and not node.data.strip()


def _has_element_only_content(el) -> bool:
    has_element = False
    for c in el.childNodes:
        if c.nodeType == Node.ELEMENT_NODE:
            has_element = True
        elif c.nodeType == Node.CDATA_SECTION_NODE:
            return False
        elif c.nodeType == Node.TEXT_NODE and c.data.strip():
            return False
    return has_element


def _pretty(node, depth: int, lines: list):
    pad = "  " * depth
    if node.nodeType == Node.ELEMENT_NODE and _has_element_only_content(node):
        lines.append(pad + _start_tag(node))
        for c in node.childNodes:
            if not _is_blank_text(c):
                _pretty(c, depth + 1, lines)
        lines.append(f"{pad}</{node.tagName}>")
    else:
        lines.append(pad + _inline(node))


def pretty_format(xml_text: str) -> str:
    try:
        doc = minidom.parseString(xml_text.encode("utf-8"))
    except Exception:
        return xml_text  # not parseable: hand it back untouched
    lines = []
    if xml_text.startswith("<?xml"):
        lines.append(xml_text[: xml_text.index("?>") + 2])
    for child in doc.childNodes:
        if not _is_blank_text(child):
            _pretty(child, 0, lines)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def pull_from_filemaker(pretty: bool = True) -> str:
    global _last_own_seq
    with ClipboardContext():
        found = _read_fm_xml()
        if found is None:
            raise RuntimeError(
                "No FileMaker object found on the clipboard. Copy a script or step in FileMaker first.")
        xml_text = pretty_format(found[0]) if pretty else found[0]
        # Convenience only: also offer the XML as plain text. Never let this
        # step fail the pull itself.
        try:
            _set_plain_text(xml_text)
        except Exception:
            pass
    _last_own_seq = user32.GetClipboardSequenceNumber()
    return xml_text


def convert_clipboard() -> dict:
    """If FileMaker put an object on the clipboard, add its XML as plain text
    so a normal Ctrl+V in any editor pastes the XML. FileMaker's own data is
    left in place, so pasting back into FileMaker keeps working."""
    global _last_own_seq, _last_checked_seq
    seq = user32.GetClipboardSequenceNumber()
    if seq in (_last_own_seq, _last_checked_seq):
        return {"converted": False}
    if not _fm_format_available():
        _last_checked_seq = seq
        return {"converted": False}
    with ClipboardContext():
        found = _read_fm_xml()
        if found is None:
            _last_checked_seq = seq
            return {"converted": False}
        xml_text, fmt_name = found
        _set_plain_text(pretty_format(xml_text))
    _last_own_seq = user32.GetClipboardSequenceNumber()
    return {"converted": True, "format": fmt_name, "label": FM_FORMATS[fmt_name]}


def normalize_and_detect_format(xml_text: str) -> tuple[str, str]:
    xml_text = xml_text.strip()
    if not xml_text:
        raise ValueError("Nothing to copy: the XML is empty.")

    # Auto-wrap partial selections (e.g. a few <Step> elements without the root node)
    wrapped = not xml_text.startswith("<?xml") and not xml_text.startswith("<fmxmlsnippet")
    if wrapped:
        xml_text = f'<fmxmlsnippet type="FMObjectList">\n{xml_text}\n</fmxmlsnippet>'

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        line, col = e.position
        if wrapped:
            line -= 1  # report positions relative to what the user wrote
        raise ValueError(f"XML syntax error on line {line}, column {col}: {e.msg.split(':')[0]}")

    if root.tag != "fmxmlsnippet":
        raise ValueError(f"Root tag must be <fmxmlsnippet>, found <{root.tag}>.")

    child_tags = {child.tag for child in root}
    for tags, fmt_name in TAG_FORMATS:
        if child_tags.intersection(tags):
            return xml_text, fmt_name

    if not child_tags:
        raise ValueError("Nothing to copy: <fmxmlsnippet> is empty.")
    supported = ", ".join(f"<{t}>" for tags, _ in TAG_FORMATS for t in tags)
    found = ", ".join(f"<{t}>" for t in sorted(child_tags))
    raise ValueError(f"Unrecognized FileMaker object ({found}). Supported top-level tags: {supported}.")


def push_to_filemaker(raw_input: str) -> str:
    global _last_own_seq
    xml_text, fmt_name = normalize_and_detect_format(raw_input)
    fmt_id = user32.RegisterClipboardFormatW(fmt_name)

    xml_bytes = xml_text.replace("\r\n", "\n").encode("utf-8")
    fm_payload = struct.pack("<I", len(xml_bytes)) + xml_bytes

    with ClipboardContext():
        user32.EmptyClipboard()
        _set_raw_clipboard_bytes(fmt_id, fm_payload)
        # Plain text = exactly what the user copied (not the auto-wrapped
        # version), so pasting into a text editor behaves like a normal copy.
        _set_plain_text(raw_input)
    _last_own_seq = user32.GetClipboardSequenceNumber()
    return fmt_name


# --------------------------------------------------------------------------
# Readable view: FileMaker XML -> Script Workspace-style text
# --------------------------------------------------------------------------
# Approximates how FileMaker displays steps. Common steps have dedicated
# formatters; every other step lists its options generically, so nothing is
# silently hidden. Each output line records the XML line it came from, so the
# editor can keep the cursor on the same step when switching views.

class _El:
    __slots__ = ("tag", "attrs", "children", "parts", "line")

    def __init__(self, tag, attrs, line):
        self.tag, self.attrs, self.line = tag, attrs, line
        self.children, self.parts = [], []

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def child(self, tag):
        return next((c for c in self.children if c.tag == tag), None)


def _parse_with_lines(xml_text: str) -> tuple[_El, int]:
    import xml.parsers.expat as expat

    xml_text = xml_text.strip()
    if not xml_text:
        raise ValueError("The document is empty.")
    wrapped = not xml_text.startswith("<?xml") and not xml_text.startswith("<fmxmlsnippet")
    if wrapped:
        xml_text = f'<fmxmlsnippet type="FMObjectList">\n{xml_text}\n</fmxmlsnippet>'
    line_offset = -1 if wrapped else 0

    parser = expat.ParserCreate()
    roots, stack = [], []

    def start(tag, attrs):
        el = _El(tag, attrs, parser.CurrentLineNumber + line_offset)
        (stack[-1].children if stack else roots).append(el)
        stack.append(el)

    def end(_tag):
        stack.pop()

    def chars(data):
        if stack:
            stack[-1].parts.append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(xml_text.encode("utf-8"), True)
    except expat.ExpatError as e:
        raise ValueError(
            f"XML error on line {e.lineno + line_offset}, column {e.offset}: {expat.ErrorString(e.code)}")
    root = roots[0]
    if root.tag != "fmxmlsnippet":
        raise ValueError(f"Root tag must be <fmxmlsnippet>, found <{root.tag}>.")
    return root, line_offset


BLOCK_OPEN = {"If", "Else If", "Else", "Loop", "Open Transaction"}
BLOCK_CLOSE = {"Else If", "Else", "End If", "End Loop", "Commit Transaction"}
INDENT = "    "


def _calc(el) -> str:
    c = el.child("Calculation") if el is not None else None
    return c.text.strip() if c is not None else ""


def _on_off(el) -> str:
    return "On" if el.attrs.get("state") == "True" else "Off"


# Friendlier names for common option tags (None = show the value alone).
LABELS = {
    "Exit": "Exit after last",
    "FlushType": "Flush",
    "RowPageLocation": None,
    "LayoutDestination": None,
}


def _label(tag: str) -> str:
    if tag in LABELS and LABELS[tag]:
        return LABELS[tag]
    out = ""
    for i, ch in enumerate(tag):
        if ch.isupper() and i and not tag[i - 1].isupper():
            out += " "
        out += ch
    return out


def _field_name(el) -> str:
    name = el.attrs.get("name", "")
    table = el.attrs.get("table", "")
    return f"{table}::{name}" if table and name else name or el.text.strip()


def _describe(el):
    """Generic one-parameter description of a step's child element."""
    tag, a = el.tag, el.attrs
    if tag == "Calculation":
        return el.text.strip() or None
    if tag == "NoInteract":
        return "With dialog: " + ("Off" if a.get("state") == "True" else "On")
    if tag == "Set" and "state" in a:
        return _on_off(el)
    if "state" in a:
        return f"{_label(tag)}: {_on_off(el)}"
    if tag == "Field":
        return _field_name(el) or None
    if tag in ("Script", "Layout", "ValueList", "FileReference", "Table") and a.get("name"):
        return f'"{a["name"]}"'
    if tag == "Repetition":
        rep = _calc(el)
        return None if rep in ("", "1") else f"Repetition: {rep}"
    if el.child("Calculation") is not None:
        calc = _calc(el)
        return f"{_label(tag)}: {calc}" if calc else None
    if "value" in a and not el.children:
        return a["value"] if LABELS.get(tag, "") is None else f"{_label(tag)}: {a['value']}"
    if not el.children:
        txt = el.text.strip()
        if txt:
            return txt if tag in ("Name", "Text") else f"{_label(tag)}: {txt}"
        if a.get("name"):
            return f'{_label(tag)}: "{a["name"]}"'
        return None
    inner = [d for d in (_describe(c) for c in el.children) if d]
    return f"{_label(tag)}: {', '.join(inner)}" if inner else None


def _generic_params(step) -> list:
    return [d for d in (_describe(c) for c in step.children) if d]


def _fmt_set_variable(st):
    name = (st.child("Name").text.strip() if st.child("Name") is not None else "")
    rep = _calc(st.child("Repetition"))
    if rep and rep != "1":
        name += f"[{rep}]"
    return [name, f"Value: {_calc(st.child('Value'))}"]


def _fmt_set_field(st):
    f = st.child("Field")
    return [p for p in (_field_name(f) if f is not None else "", _calc(st)) if p]


def _fmt_condition(st):
    return [_calc(st)] if _calc(st) else []


def _fmt_perform_script(st):
    params = []
    sc = st.child("Script")
    if sc is not None and sc.attrs.get("name"):
        params.append(f'"{sc.attrs["name"]}"')
    fr = st.child("FileReference")
    if fr is not None and fr.attrs.get("name"):
        params.append(f'From file: "{fr.attrs["name"]}"')
    if _calc(st):
        params.append(f"Parameter: {_calc(st)}")
    return params


def _fmt_exit_script(st):
    return [f"Text Result: {_calc(st)}"] if _calc(st) else []


def _fmt_go_to_layout(st):
    dest = st.child("LayoutDestination")
    kind = dest.attrs.get("value", "") if dest is not None else ""
    layout = st.child("Layout")
    if kind == "OriginalLayout":
        params = ["original layout"]
    elif layout is not None and layout.attrs.get("name"):
        params = [f'"{layout.attrs["name"]}"']
    elif _calc(st):
        params = [f"Layout: {_calc(st)}"]
    else:
        params = []
    anim = st.child("Animation")
    if anim is not None and anim.attrs.get("value") not in (None, "", "None"):
        params.append(f"Animation: {anim.attrs['value']}")
    return params


def _fmt_custom_dialog(st):
    return [p for p in (_calc(st.child("Title")), _calc(st.child("Message"))) if p]


STEP_FORMATTERS = {
    "Set Variable": _fmt_set_variable,
    "Set Field": _fmt_set_field,
    "If": _fmt_condition,
    "Else If": _fmt_condition,
    "Exit Loop If": _fmt_condition,
    "Perform Script": _fmt_perform_script,
    "Perform Script on Server": _fmt_perform_script,
    "Exit Script": _fmt_exit_script,
    "Go to Layout": _fmt_go_to_layout,
    "Show Custom Dialog": _fmt_custom_dialog,
}


def _step_lines(st) -> list:
    name = st.attrs.get("name", "(unnamed step)")
    if name == "# (comment)":
        txt = st.child("Text").text if st.child("Text") is not None else ""
        lines = [f"# {ln}".rstrip() for ln in txt.split("\n")] if txt.strip() else [""]
    else:
        formatter = STEP_FORMATTERS.get(name)
        params = formatter(st) if formatter else _generic_params(st)
        text = f"{name} [ {' ; '.join(params)} ]" if params else name
        lines = text.split("\n")
    if st.attrs.get("enable") == "False":
        lines = [f"// {ln}" for ln in lines]
    return lines


def _render_steps(steps, out):
    depth = 0
    for st in steps:
        name = st.attrs.get("name", "")
        if name in BLOCK_CLOSE:
            depth = max(0, depth - 1)
        pad = INDENT * depth
        # Calculation continuation lines are indented under their step;
        # extra comment lines already carry their own "# " marker.
        cont = "" if name == "# (comment)" else INDENT
        for i, ln in enumerate(_step_lines(st)):
            if not ln:
                out.append(("", st.line))
            else:
                out.append((pad + (cont if i else "") + ln, st.line))
        if name in BLOCK_OPEN:
            depth += 1


def _header(text, line, out):
    if out:
        out.append(("", line))
    out.append((f"━━ {text} ━━", line))


def _render_object(el, out, folder_path=""):
    tag, name = el.tag, el.attrs.get("name", "")
    if tag == "Group":
        path = f"{folder_path}{name} / "
        for c in el.children:
            _render_object(c, out, path)
    elif tag == "Script":
        if el.attrs.get("isSeparatorItem") == "True" or (name == "-" and not el.children):
            _header("—— separator ——", el.line, out)
            return
        _header(f"Script: {folder_path}{name}", el.line, out)
        _render_steps([c for c in el.children if c.tag == "Step"], out)
    elif tag == "CustomFunction":
        params = " ; ".join(p.strip() for p in el.attrs.get("parameters", "").split(";") if p.strip())
        _header(f"Custom Function: {name} ( {params} )" if params else f"Custom Function: {name}", el.line, out)
        for ln in _calc(el).split("\n"):
            out.append((ln, el.line))
    else:
        title = f"{_label(tag)}: {name}" if name else _label(tag)
        _header(title, el.line, out)
        for c in el.children:
            if c.tag == "Field":
                desc = [d for d in (_describe(x) for x in c.children) if d]
                fn = c.attrs.get("name", "")
                kind = ", ".join(v for v in (c.attrs.get("dataType"), c.attrs.get("fieldType")) if v)
                out.append((f"{INDENT}{fn}" + (f"  ({kind})" if kind else ""), c.line))
                for d in desc:
                    for ln in d.split("\n"):
                        out.append((INDENT * 2 + ln, c.line))
            else:
                d = _describe(c)
                if d:
                    for ln in d.split("\n"):
                        out.append((INDENT + ln, c.line))


def render_readable(xml_text: str) -> dict:
    root, _ = _parse_with_lines(xml_text)
    out = []
    loose_steps = [c for c in root.children if c.tag == "Step"]
    if loose_steps:
        _render_steps(loose_steps, out)
    for c in root.children:
        if c.tag != "Step":
            _render_object(c, out)
    if not out:
        out = [("(nothing to show)", root.line)]
    return {"lines": [t for t, _ in out], "map": [ln for _, ln in out]}


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def serve():
    """JSON-lines request loop: one request per stdin line, one response per
    stdout line. Exits when stdin closes (i.e. when VS Code goes away)."""
    sys.stdin.reconfigure(encoding="utf-8")

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")  # ASCII-only by default
        sys.stdout.flush()

    emit({"ready": True, "protocol": 1})
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        if not line.strip():
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            op = req.get("op")
            if op == "pull":
                result = pull_from_filemaker(pretty=True)
            elif op == "push":
                fmt = push_to_filemaker(req["text"])
                result = {"format": fmt, "label": FM_FORMATS[fmt]}
            elif op == "convert":
                result = convert_clipboard()
            elif op == "render":
                result = render_readable(req["text"])
            elif op == "ping":
                result = "pong"
            else:
                raise ValueError(f"Unknown operation: {op!r}")
            emit({"id": req_id, "ok": True, "result": result})
        except Exception as exc:
            emit({"id": req_id, "ok": False, "error": str(exc)})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    cmd = sys.argv[1] if len(sys.argv) >= 2 else None
    if cmd not in ("pull", "push", "convert", "serve"):
        print("Usage: fm_clip_bridge [pull | push (--stdin | file_path) | convert | serve]",
              file=sys.stderr)
        sys.exit(1)

    try:
        if cmd == "serve":
            serve()
        elif cmd == "pull":
            print(pull_from_filemaker(pretty=True))
        elif cmd == "convert":
            r = convert_clipboard()
            print(f"Converted {r['label']} to text." if r["converted"] else "Nothing to convert.")
        elif cmd == "push":
            if "--stdin" in sys.argv:
                sys.stdin.reconfigure(encoding="utf-8")
                content = sys.stdin.read()
            elif len(sys.argv) >= 3:
                with open(sys.argv[2], "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                raise ValueError("Provide XML via --stdin or file path.")
            fmt = push_to_filemaker(content)
            print(f"Copied to FileMaker clipboard as {FM_FORMATS[fmt]} ({fmt}). Ready to paste in FileMaker!")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

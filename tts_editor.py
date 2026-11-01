"""
Tabletop Simulator Editor - the TTS External Editor API for Sublime Text 3 and 4.

Copyright (C) 2026 wunditta (https://github.com/wunditta/sublime-tabletop-simulator-editor)

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details
(the LICENSE file, or https://www.gnu.org/licenses/).

Talks to Tabletop Simulator directly:
  sends on   127.0.0.1:39999   (Get Lua Scripts, Save & Play, Execute Lua)
  listens on 127.0.0.1:39998   (TTS answers on its own connection: scripts, print output, errors)

Files (in the script folder; subfolders are ignored unless "objects_subfolder" names one):
  Global.-1.lua, Global.-1.xml      the Global script and its UI
  <Name>.<GUID>.lua                 one file per scripted object

Settings come from the project file ("settings": {"tts_editor": {...}}) first, then from the
package settings (Tabletop Simulator Editor.sublime-settings).
"""

import sublime
import sublime_plugin

import json
import os
import re
import shutil
import socket
import threading
import time
import uuid

TTS_PORT = 39999
LISTEN_PORT = 39998
SETTINGS_FILE = "Tabletop Simulator Editor.sublime-settings"
PANEL = "tts"
GLOBAL_LUA = "Global.-1.lua"
GLOBAL_XML = "Global.-1.xml"
OBJECT_RE = re.compile(r"^(.+)\.([0-9a-fA-F]{6})\.lua$")
ID_RE = re.compile(r"tts-editor-id:[ \t]*([\w-]+)")
ID_LINE = "-- Sublime TTS Editor - Unique ID to prevent pushing to wrong TTS mod - tts-editor-id: %s"
HISTORY_DIR = "_history"
STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d-\d\d-\d\d")

DEFAULTS = {
    "script_folder": "",
    "objects_subfolder": "",
    "listener": "on_demand",
    "save_before_push": False,
    "report_push_result": True,
    "listener_grace_seconds": 30,
    "push": "open",
    "save_id": True,
    "history": False,
    "history_keep": None,
    "confirm_get": True,
    "show_panel_on_output": True,
    "file_interface": False,
    "file_interface_folder": "_bridge",
}

_listener = None           # Listener thread
_deadline = 0.0            # on_demand: when the listener may stop
_waiters = []              # [(messageID, callback)] one-shot consumers of incoming messages
_known_guids = {}          # guid -> name as of the last Get
_incoming = {"folder": None, "suffix": ""}   # where incoming scripts go (None = script folder)
_logfiles = set()          # file interface: log files that mirror the panel
_poller_running = False
_expecting = 0             # answers to a Get still outstanding (keeps an on-demand listener alive)


# ------------------------------------------------------------------ settings --

def setting(window, key):
    """Project setting ("settings": {"tts_editor": {...}}), else package setting, else default."""
    if window is not None:
        pd = window.project_data() or {}
        proj = (pd.get("settings") or {}).get("tts_editor") or {}
        if key in proj:
            return proj[key]
    s = sublime.load_settings(SETTINGS_FILE)
    if s.has(key):
        return s.get(key)
    return DEFAULTS.get(key)


def script_folder(window):
    """The configured folder; without one, the project's first folder."""
    f = setting(window, "script_folder")
    if f:
        f = os.path.expandvars(os.path.expanduser(f))
        if not os.path.isabs(f) and window is not None and window.project_file_name():
            f = os.path.join(os.path.dirname(window.project_file_name()), f)
        return os.path.normpath(f)
    if window is not None and window.folders():
        return os.path.normpath(window.folders()[0])
    return None


def objects_folder(window, base):
    sub = setting(window, "objects_subfolder") or ""
    return os.path.normpath(os.path.join(base, sub)) if sub else base


# ------------------------------------------------------------------- helpers --

def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")
    return name[:50] or "unnamed"


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def write_text(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


def stamp():
    return time.strftime("%Y-%m-%d %H-%M-%S")


def log(tag, text):
    """A line in the TTS output panel (on the main thread)."""
    def do():
        window = sublime.active_window()
        if window is None:
            return
        panel = window.find_output_panel(PANEL)
        if panel is None:
            panel = window.create_output_panel(PANEL)
            panel.settings().set("word_wrap", True)
            panel.settings().set("gutter", False)
            panel.settings().set("scroll_past_end", False)
        line = "[%s] %-6s %s\n" % (time.strftime("%H:%M:%S"), tag, text)
        for lf in list(_logfiles):            # file interface: the same line into its log file
            try:
                with open(lf, "ab") as f:
                    f.write(line.encode("utf-8"))
            except (IOError, OSError):
                pass
        panel.set_read_only(False)
        panel.run_command("append", {"characters": line, "scroll_to_end": True})
        panel.set_read_only(True)
        if setting(window, "show_panel_on_output"):
            window.run_command("show_panel", {"panel": "output." + PANEL})
    sublime.set_timeout(do, 0)


_errors = []               # Lua errors collected for one dialog when the panel is not shown automatically


def report_error(err):
    """With show_panel_on_output off the panel stays hidden, so Lua errors are brought up in a dialog instead
    (collected for a second, one dialog for all of them - a reload can report several at once)."""
    if setting(sublime.active_window(), "show_panel_on_output"):
        return
    _errors.append(err)
    if len(_errors) > 1:
        return
    def show():
        errs = list(_errors)
        del _errors[:]
        text = "Tabletop Simulator reports %d Lua error(s):\n\n%s" % (len(errs), "\n\n".join(e[:400] for e in errs[:5]))
        if len(errs) > 5:
            text += "\n\n... and %d more (see the TTS output panel)" % (len(errs) - 5)
        sublime.error_message(text)
    sublime.set_timeout(show, 1000)


def send(payload):
    """One JSON message to TTS. True when delivered."""
    data = json.dumps(payload).encode("utf-8")
    try:
        sock = socket.create_connection(("127.0.0.1", TTS_PORT), timeout=3)
        try:
            sock.sendall(data)
        finally:
            sock.close()
        return True
    except (socket.error, OSError) as e:
        msg = "cannot reach Tabletop Simulator on port %d - is the game running with a save loaded? (%s)" % (TTS_PORT, e)
        log("ERR", msg)
        sublime.error_message("Tabletop Simulator Editor: " + msg)
        return False


def wait_for(message_id, callback, timeout, on_timeout, match=None):
    """Call callback(msg) on the main thread for the next message with this id (and match(msg) true, when given),
    or on_timeout() after timeout seconds."""
    entry = [message_id, callback, match]
    _waiters.append(entry)

    def expire():
        if entry in _waiters:
            _waiters.remove(entry)
            on_timeout()
    sublime.set_timeout(expire, int(timeout * 1000))


# ------------------------------------------------------------------ incoming --

def save_incoming_scripts(states, window):
    """Write the script states TTS sent: into the script folder, or into a history folder with a suffix."""
    global _known_guids
    hist, suffix = _incoming["folder"], _incoming["suffix"]
    _incoming["folder"], _incoming["suffix"] = None, ""
    expect_answer(-1)
    base = hist or script_folder(window)
    if not base:
        log("ERR", "no script folder - open a project with a folder or set \"script_folder\"")
        return
    objdir = base if hist else objects_folder(window, base)
    where = id_mode(window)
    if not hist and where is not None:
        # the reverse check: does the loaded TTS file belong to this project? Otherwise a Get would overwrite the wrong scripts
        local = os.path.join(base, GLOBAL_LUA)
        received = next((s.get("script") or "" for s in states if str(s.get("guid", "")) == "-1"), "")
        loaded_id, project_id = text_id(received), file_id(local)
        if os.path.isfile(local) and loaded_id != project_id:
            text = (QUESTION_GET + "\n\nLoaded file: %s\nThis project: %s" % (loaded_id or "no id yet", project_id or "no id yet"))
            dest = None
            if setting(window, "history"):
                dest = history_folder(base)
                text += ("\n\nBackups of the project's scripts, which will be overwritten, are saved to folder \"%s\\%s\" with \"-Overwritten\" at the end of their file names."
                         % (HISTORY_DIR, os.path.basename(dest)))
            text += "\n\nGo ahead and overwrite?"
            if not sublime.ok_cancel_dialog(text, "Overwrite"):
                log("INFO", "Get cancelled - id %s loaded, project has %s; nothing written" % (loaded_id or "none", project_id or "none"))
                return
            if dest:
                write_history(window, folder_script_files(window), dest, "-Overwritten")
    count, known = 0, {}
    for s in states:
        guid, name = str(s.get("guid", "")), str(s.get("name", ""))
        known[guid] = name
        if guid == "-1":
            write_text(os.path.join(base, "Global.-1%s.lua" % suffix), s.get("script") or "")
            if s.get("ui") is not None:
                write_text(os.path.join(base, "Global.-1%s.xml" % suffix), s.get("ui") or "")
            count += 1
        else:
            script = s.get("script") or ""
            if not script.strip():
                continue                     # unscripted object
            write_text(os.path.join(objdir, "%s.%s%s.lua" % (safe_name(name), guid, suffix)), script)
            count += 1
    if not suffix:
        _known_guids = known
    log("RECV", "%d scripts written to %s (%d objects in the scene)" % (count, base, len(known)))
    if not hist:
        if where is not None and os.path.isfile(os.path.join(base, GLOBAL_LUA)):
            ensure_file_id(os.path.join(base, GLOBAL_LUA), window)
        def open_global():
            w = sublime.active_window()
            if w is not None and os.path.isfile(os.path.join(base, GLOBAL_LUA)):
                w.open_file(os.path.join(base, GLOBAL_LUA))
        sublime.set_timeout(open_global, 0)


def handle_message(text):
    try:
        msg = json.loads(text)
    except ValueError:
        log("ERR", "unparseable message from TTS: " + text[:200])
        return
    mid = msg.get("messageID")
    for entry in list(_waiters):          # one-shot consumers first (save-id check, overwrite backup)
        if entry[0] == mid and (entry[2] is None or entry[2](msg)):
            _waiters.remove(entry)
            sublime.set_timeout(lambda e=entry, m=msg: e[1](m), 0)
            return
    if mid in (0, 1):
        # 1 comes as the answer to a Get, but also unasked whenever TTS loads a game (after every Save & Play, after a
        # manual load); 0 when a player pushes an object's script from TTS. Only an answer we asked for is written.
        states = msg.get("scriptStates") or []
        if _expecting > 0 or _incoming["folder"] is not None:
            sublime.set_timeout(lambda: save_incoming_scripts(states, sublime.active_window()), 0)
        else:
            log("RECV", "TTS %s %d scripts - not written (no Get requested)" % ("loaded a game with" if mid == 1 else "pushed", len(states)))
    elif mid == 2:
        log("PRINT", str(msg.get("message", "")))
    elif mid == 3:
        err = "%s%s  (guid %s)" % (msg.get("errorMessagePrefix", ""), msg.get("error", ""), msg.get("guid", ""))
        log("LUAERR", err)
        report_error(err)
    elif mid == 4:
        log("CUSTOM", json.dumps(msg.get("customMessage")))
    elif mid == 5:
        log("RETURN", json.dumps(msg.get("returnValue")))
    elif mid == 6:
        log("SAVED", "TTS saved the game")
    elif mid == 7:
        log("RECV", "object created: %s" % msg.get("guid", ""))
    else:
        log("RECV", "unhandled messageID %s" % mid)


class Listener(threading.Thread):
    """Accepts connections from TTS on 39998; each connection carries one JSON message."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.stop_flag = False
        self.ready = threading.Event()
        self.error = None
        self.sock = None

    def run(self):
        sock = self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":          # lets the port be reused while old connections linger; on Windows it would allow a second binder
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", LISTEN_PORT))
            sock.listen(5)
            sock.settimeout(0.5)
        except (socket.error, OSError) as e:
            self.error = str(e)
            self.ready.set()
            log("ERR", "cannot listen on port %d - another TTS editor tool is running? (%s)" % (LISTEN_PORT, e))
            return
        self.ready.set()
        log("INFO", "listening for Tabletop Simulator on port %d" % LISTEN_PORT)
        while not self.stop_flag:
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            except (socket.error, OSError):
                break                       # closed by stop()
            t = threading.Thread(target=self.receive, args=(client,))
            t.daemon = True
            t.start()
        try:
            sock.close()
        except (socket.error, OSError):
            pass
        log("INFO", "listener stopped")

    def stop(self):
        """Stops the accept loop at once (closing the socket frees the port for an immediate restart)."""
        self.stop_flag = True
        try:
            if self.sock is not None:
                self.sock.close()
        except (socket.error, OSError):
            pass
        self.join(2)

    @staticmethod
    def receive(client):
        """Reads one JSON message. TTS does not close its connection after sending, so the message is complete when it
        parses - waiting for the close (or a read timeout) would stall every message for seconds."""
        chunks, text, msg = [], "", None
        try:
            client.settimeout(10)
            while True:
                try:
                    data = client.recv(65536)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
                text = b"".join(chunks).decode("utf-8", "replace")
                try:
                    msg = json.loads(text)
                    break
                except ValueError:
                    continue
        finally:
            client.close()
        if msg is None and not text.strip():
            return
        try:
            handle_message(text)
        except Exception as e:         # one bad message must not kill the listener
            log("ERR", "while handling a message: %s" % e)


def listener_running():
    return _listener is not None and _listener.is_alive() and not _listener.error


def start_listener():
    global _listener
    if listener_running():
        return True
    stop_listener()
    _listener = Listener()
    _listener.start()
    _listener.ready.wait(2)
    return listener_running()


def stop_listener():
    global _listener
    if _listener is not None:
        l, _listener = _listener, None
        l.stop()


def ensure_listener(window, seconds=None):
    """Listener for the next operation, according to the "listener" setting. False when nothing can be received."""
    global _deadline
    mode = setting(window, "listener")
    if not start_listener():
        return False
    if mode not in ("always", "first_use"):     # both keep the listener until Sublime closes
        grace = seconds if seconds is not None else float(setting(window, "listener_grace_seconds") or 0)
        _deadline = max(_deadline, time.time() + grace)
        sublime.set_timeout(listener_check, int(grace * 1000) + 100)
    return True


def listener_check():
    """on_demand: stop the listener once the grace period is over and nothing is outstanding."""
    if _listener is None or setting(sublime.active_window(), "listener") in ("always", "first_use"):
        return
    if _waiters or _expecting > 0:
        sublime.set_timeout(listener_check, 500)      # an answer is still due - look again
    elif time.time() >= _deadline - 0.05:
        stop_listener()


def expect_answer(delta):
    """A Get is outstanding (+1) or answered (-1); an outstanding Get times out after 60 s."""
    global _expecting
    _expecting = max(0, _expecting + delta)
    if delta > 0:
        def give_up():
            global _expecting
            if _expecting > 0:
                _expecting -= 1
        sublime.set_timeout(give_up, 60000)


def plugin_loaded():
    def go():
        if setting(sublime.active_window(), "listener") == "always":
            start_listener()
        start_poller()
    sublime.set_timeout(go, 500)


def plugin_unloaded():
    stop_listener()


# -------------------------------------------------------------------- files --

def is_script_file(window, path):
    """Global.-1.lua / Global.-1.xml in the script folder, or <Name>.<GUID>.lua in the objects folder."""
    if not path:
        return False
    base = script_folder(window)
    if not base:
        return False
    d, fn = os.path.split(os.path.normpath(path))
    if fn in (GLOBAL_LUA, GLOBAL_XML):
        return os.path.normcase(d) == os.path.normcase(base)
    if OBJECT_RE.match(fn):
        return os.path.normcase(d) == os.path.normcase(objects_folder(window, base))
    return False


def folder_script_files(window):
    base = script_folder(window)
    files = []
    for fn in (GLOBAL_LUA, GLOBAL_XML):
        if os.path.isfile(os.path.join(base, fn)):
            files.append(os.path.join(base, fn))
    objdir = objects_folder(window, base)
    if os.path.isdir(objdir):
        for fn in sorted(os.listdir(objdir)):
            if OBJECT_RE.match(fn):
                files.append(os.path.join(objdir, fn))
    return files


def build_states(window, files):
    """Script states for the files. Global's script and UI always travel together (TTS clears the UI when
    the Global state has no "ui"), so when either file is among the files, the other is read from disk."""
    base = script_folder(window)
    states, glob = [], None
    for path in files:
        fn = os.path.basename(path)
        if fn in (GLOBAL_LUA, GLOBAL_XML):
            if glob is None:
                glob = {"name": "Global", "guid": "-1", "script": read_text(os.path.join(base, GLOBAL_LUA))}
                xml = os.path.join(base, GLOBAL_XML)
                glob["ui"] = read_text(xml) if os.path.isfile(xml) else ""
                states.insert(0, glob)
        else:
            m = OBJECT_RE.match(fn)
            # every object file is sent: TTS drops a script whose object is not loose in the scene (inside a bag or deck,
            # or gone) without harm, and filtering by the last Get would miss objects taken out of a bag since
            states.append({"name": m.group(1), "guid": m.group(2), "script": read_text(path)})
    return states


def id_mode(window):
    """None (save_id off), "first" or "last": where a missing id line is added."""
    v = setting(window, "save_id")
    if v is False or v is None:
        return None
    return "first" if v == "first" else "last"


def text_id(text):
    """The tts-editor-id anywhere in a script text, or ""."""
    m = ID_RE.search(text or "")
    return m.group(1) if m else ""


def file_id(path):
    try:
        return text_id(read_text(path))
    except (IOError, OSError):
        return ""


def with_id(text, where, new_id):
    """The text with the id line added at the configured end (the text is returned unchanged when it has an id)."""
    if text_id(text):
        return text
    line = ID_LINE % new_id
    if where == "first":
        return line + "\n" + text
    return text + ("" if text.endswith("\n") or text == "" else "\n") + line + "\n"


def ensure_file_id(path, window):
    """Adds the id line to Global.-1.lua when it has none; returns the id ("" when save_id is off)."""
    where = id_mode(window)
    if where is None:
        return ""
    cur = file_id(path)
    if cur:
        return cur
    new = uuid.uuid4().hex[:12]
    write_text(path, with_id(read_text(path), where, new))
    log("INFO", "save id %s written to the %s line of %s" % (new, where, GLOBAL_LUA))
    return new


def history_folder(base):
    """A new <script folder>/_history/<stamp>/ (not created yet)."""
    hist = os.path.join(base, HISTORY_DIR)
    dest = os.path.join(hist, stamp())
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(hist, "%s-%d" % (stamp(), n))
    return dest


def write_history(window, files, dest=None, suffix=""):
    """Copies the files into dest (default: a new history folder), suffix before the extension, and applies the retention."""
    base = script_folder(window)
    hist = os.path.join(base, HISTORY_DIR)
    dest = dest or history_folder(base)
    if not os.path.isdir(dest):
        os.makedirs(dest)
    for path in files:
        root, ext = os.path.splitext(os.path.basename(path))
        shutil.copy2(path, os.path.join(dest, root + suffix + ext))
    log("HIST", "%d files copied to %s" % (len(files), os.path.relpath(dest, base)))
    keep = setting(window, "history_keep")
    if isinstance(keep, (int, float)) and keep > 0:
        folders = sorted(fn for fn in os.listdir(hist) if STAMP_RE.match(fn) and os.path.isdir(os.path.join(hist, fn)))
        for fn in folders[:-int(keep)] if len(folders) > keep else []:
            shutil.rmtree(os.path.join(hist, fn), ignore_errors=True)
            log("HIST", "removed %s (keeping the newest %d)" % (fn, int(keep)))


QUESTION = ("Are you sure the script should be pushed to the currently loaded file in TTS?\n\n"
            "(TTS does not provide the name of the loaded file. Therefore a unique id is added to the global script "
            "for identification. If the ids do not match (or the id is missing) this dialog comes up.)")
QUESTION_GET = ("Are you sure the scripts of the currently loaded file in TTS should overwrite this project's scripts?\n\n"
                "(TTS does not provide the name of the loaded file. Therefore a unique id is added to the global script "
                "for identification. If the ids do not match (or the id is missing) this dialog comes up.)")


# ------------------------------------------------------------ file interface --
# An external program (a build script, an AI assistant, ...) drives TTS by dropping small .json files into
# <script folder>/<file_interface_folder>/outbox/; the package carries them out and mirrors the TTS output panel
# into <file_interface_folder>/log.txt. Commands:
#   {"cmd": "getScripts"}                       scripts of the loaded save -> <file_interface_folder>/inbox/ (the sources stay untouched)
#   {"cmd": "saveAndPlay"}                      push every script file of the folder
#   {"cmd": "exec", "guid": "-1", "script": "..."}   run Lua in Global (or in the object with that GUID)
#   {"cmd": "custom", "payload": {...}}         a custom message to the mod (onExternalMessage)
#   {"cmd": "snapshot"}                         copy the script files into a history folder

def interface_windows():
    """(window, interface folder) for every window whose project enables the file interface."""
    out = []
    for w in sublime.windows():
        if setting(w, "file_interface"):
            base = script_folder(w)
            if base:
                sub = setting(w, "file_interface_folder") or "_bridge"
                out.append((w, os.path.normpath(os.path.join(base, sub))))
    return out


def start_poller():
    global _poller_running
    if _poller_running:
        return
    _poller_running = True
    sublime.set_timeout(poll_outbox, 500)


def poll_outbox():
    global _logfiles
    try:
        wins = interface_windows()
        _logfiles = set(os.path.join(folder, "log.txt") for _, folder in wins)
        for w, folder in wins:
            outbox = os.path.join(folder, "outbox")
            if not os.path.isdir(outbox):
                continue
            for fn in sorted(os.listdir(outbox)):
                if not fn.lower().endswith(".json"):
                    continue
                path = os.path.join(outbox, fn)
                try:
                    cmd = json.loads(read_text(path))
                except (IOError, OSError):
                    continue                      # still being written
                except ValueError:
                    if time.time() - os.path.getmtime(path) > 30:
                        os.rename(path, path + ".bad")
                        log("WARN", "unparseable command file, renamed to %s.bad" % fn)
                    continue
                os.remove(path)
                try:
                    run_file_command(w, folder, cmd, fn)
                except Exception as e:
                    log("ERR", "command %s failed: %s" % (fn, e))
    finally:
        sublime.set_timeout(poll_outbox, 500)


def run_file_command(window, folder, cmd, fn):
    name = cmd.get("cmd") if isinstance(cmd, dict) else None
    if name == "getScripts":
        if not ensure_listener(window):
            log("ERR", "getScripts: nothing can be received (port taken)")
            return
        _incoming["folder"], _incoming["suffix"] = os.path.join(folder, "inbox"), ""
        expect_answer(+1)
        if send({"messageID": 0}):
            log("PULL", "requested all scripts from TTS (-> %s)" % os.path.join(os.path.basename(folder), "inbox"))
        else:
            expect_answer(-1)
            _incoming["folder"] = None
    elif name == "saveAndPlay":
        files = folder_script_files(window)
        states = build_states(window, files)
        if not states:
            log("WARN", "saveAndPlay: no script files in %s" % script_folder(window))
            return
        if setting(window, "history"):
            write_history(window, files)
        if send({"messageID": 1, "scriptStates": states}):
            log("PUSH", "Save & Play (file interface): %d scripts, %d chars - save id not checked" % (len(states), sum(len(s["script"]) for s in states)))
            if ensure_listener(window):
                expect_push_result(window, states, False)
    elif name == "exec":
        script = cmd.get("script") or ""
        ensure_listener(window)
        if send({"messageID": 3, "guid": str(cmd.get("guid") or "-1"), "script": script}):
            preview = re.sub(r"\s+", " ", script)
            log("EXEC", "[%s] %s" % (cmd.get("guid") or "-1", preview[:90] + ("..." if len(preview) > 90 else "")))
    elif name == "custom":
        if send({"messageID": 2, "customMessage": cmd.get("payload")}):
            log("SEND", "custom message")
    elif name == "snapshot":
        write_history(window, folder_script_files(window))
    else:
        log("WARN", "unknown command %r in %s" % (name, fn))


def norm_script(text):
    return (text or "").replace("\r\n", "\n").rstrip()


def verify_push(window, sent, states, dialog):
    """After a push TTS reloads and sends the scripts it now runs: compare them with what was sent. Reports objects
    whose script TTS did not take (not loose in the scene) and scripts that differ (should never happen)."""
    loaded = dict((str(st.get("guid", "")), st) for st in states)
    missing, differ = [], []
    for guid, st in sent.items():
        if guid not in loaded:
            missing.append("%s (%s)" % (st["name"], guid))
        elif norm_script(loaded[guid].get("script")) != norm_script(st["script"]):
            differ.append("%s (%s)" % (st["name"], guid))
    if not missing and not differ:
        log("INFO", "push verified: TTS runs the %d scripts as sent" % len(sent))
        return
    lines = []
    if missing:
        lines.append("Scripts TTS did not take - their objects are not loose in the scene (inside a bag or deck, or gone):\n  " + "\n  ".join(missing))
    if differ:
        lines.append("Scripts TTS runs differently from what was sent (check these!):\n  " + "\n  ".join(differ))
    log("WARN", "push result: " + " | ".join(l.replace("\n  ", " ") for l in lines))
    if not dialog or not setting(window, "report_push_result"):
        return
    text = "Save & Play: TTS reloaded, but not everything matches what was sent.\n\n" + "\n\n".join(lines)
    text += "\n\n\"Don't show again\" sets \"report_push_result\": false in this project's settings; the result is still written to the TTS output panel."
    answer = sublime.yes_no_cancel_dialog(text, "OK", "Don't show again")
    if answer == sublime.DIALOG_NO:
        if window is not None and window.project_file_name():
            pd = window.project_data() or {}
            pd.setdefault("settings", {}).setdefault("tts_editor", {})["report_push_result"] = False
            window.set_project_data(pd)
        else:
            st = sublime.load_settings(SETTINGS_FILE)
            st.set("report_push_result", False)
            sublime.save_settings(SETTINGS_FILE)
        log("INFO", "push results are no longer reported in a dialog (report_push_result = false)")


def expect_push_result(window, states, dialog):
    """Waits (up to 90 s) for the script list TTS sends after the reload and verifies it."""
    sent = dict((st["guid"], st) for st in states)
    wait_for(1, lambda m: verify_push(window, sent, m.get("scriptStates") or [], dialog), 90,
             lambda: log("WARN", "no script list from TTS after the push - result not verified (did the save reload?)"))


# ----------------------------------------------------------------- commands --

class TtsGetScriptsCommand(sublime_plugin.WindowCommand):
    """Ask TTS for all scripts; they arrive on the listener and are written to the script folder."""

    def run(self):
        folder = script_folder(self.window)
        if not folder:
            sublime.error_message("Tabletop Simulator Editor: open a project with a folder, or set \"script_folder\"")
            return
        if not ensure_listener(self.window):
            log("ERR", "nothing can be received: the port is taken (see above)")
            return
        if setting(self.window, "confirm_get"):
            if not sublime.ok_cancel_dialog("Get Lua Scripts from the game?\n\nScript files in\n%s\nwill be overwritten." % folder, "Get"):
                return
        expect_answer(+1)
        if send({"messageID": 0}):
            log("PULL", "requested all scripts from TTS")
        else:
            expect_answer(-1)


class TtsSaveAndPlayCommand(sublime_plugin.WindowCommand):
    """Push the script files (open ones, or the whole folder) to TTS, which reloads the save with them."""

    def run(self, mode=None, **kwargs):   # kwargs: whatever a build system passes along
        w = self.window
        base = script_folder(w)
        if not base or not os.path.isfile(os.path.join(base, GLOBAL_LUA)):
            sublime.error_message("Tabletop Simulator Editor: no %s in \"%s\" - open a project with the script folder or set \"script_folder\"" % (GLOBAL_LUA, base))
            return
        mode = mode or setting(w, "push")
        if mode == "open":
            views = [v for v in w.views() if is_script_file(w, v.file_name())]
            dirty = [v for v in views if v.is_dirty()]
            if dirty and not setting(w, "save_before_push"):
                if not sublime.ok_cancel_dialog("%d script file(s) are not saved:\n\n%s\n\nSave them and push?" % (len(dirty), "\n".join(os.path.basename(v.file_name()) for v in dirty)), "Save and push"):
                    return
                for v in dirty:
                    v.run_command("save")
            files = sorted(set(v.file_name() for v in views))
            if not files:
                log("WARN", "no script files are open - nothing to push (open %s or an object script, or set \"push\": \"folder\")" % GLOBAL_LUA)
                return
        else:
            w.run_command("save_all")
            files = folder_script_files(w)
        self.files = files
        self.base = base
        self.hist_dest = None
        self.global_path = os.path.join(base, GLOBAL_LUA)

        listening = ensure_listener(w)
        if id_mode(w) is not None:
            project_id = ensure_file_id(self.global_path, w)
            if listening:
                # the loaded save reports its id through a custom message (messageID 4) - more reliable than the
                # return value of Execute Lua, which not every TTS version sends
                script = ("local s = Global.getLuaScript() or '' "
                          "sendExternalMessage({tts_editor_id = string.match(s, 'tts%-editor%-id:[ \\t]*([%w%-]+)') or ''})")
                wait_for(4, lambda m: self.check_id(project_id, m["customMessage"]["tts_editor_id"]), 5,
                         lambda: self.no_answer(project_id),
                         match=lambda m: isinstance(m.get("customMessage"), dict) and "tts_editor_id" in m["customMessage"])
                if not send({"messageID": 3, "guid": "-1", "script": script}):
                    _waiters[:] = [e for e in _waiters if e[0] != 4]
                return
            log("WARN", "save id not checked - nothing can be received (port taken)")
        self.push()

    def no_answer(self, project_id):
        text = ("TTS did not answer the id check (is the game running with a save loaded? see the TTS output panel).\n\n"
                + QUESTION + "\n\nGo ahead and push?")
        if sublime.ok_cancel_dialog(text, "Push"):
            self.push()
        else:
            log("INFO", "push cancelled - no answer to the id check")

    def check_id(self, project_id, loaded_id):
        loaded_id = str(loaded_id or "")
        if loaded_id == project_id:
            self.push()
            return
        history = setting(self.window, "history")
        text = QUESTION + "\n\nLoaded file: %s\nThis project: %s" % (loaded_id or "no id yet", project_id)
        if history:
            self.hist_dest = history_folder(self.base)
            text += ("\n\nBackups of the scripts currently in TTS, which will be overwritten, are saved to folder \"%s\\%s\" with \"-Overwritten\" at the end of their file names."
                     % (HISTORY_DIR, os.path.basename(self.hist_dest)))
        text += "\n\nGo ahead and push?"
        if not sublime.ok_cancel_dialog(text, "Push"):
            log("INFO", "push cancelled - id %s loaded, project has %s" % (loaded_id or "none", project_id))
            return
        if history:
            _incoming["folder"], _incoming["suffix"] = self.hist_dest, "-Overwritten"
            wait_for(1, lambda m: self.backup_done(m), 60, lambda: self.backup_timeout())
            log("PULL", "requesting the scripts of the loaded file for the history (-Overwritten)")
            expect_answer(+1)
            if not send({"messageID": 0}):
                expect_answer(-1)
                _waiters[:] = [e for e in _waiters if e[0] != 1]
                _incoming["folder"], _incoming["suffix"] = None, ""
            return
        self.push()

    def backup_done(self, msg):
        save_incoming_scripts(msg.get("scriptStates") or [], self.window)
        self.push()

    def backup_timeout(self):
        _incoming["folder"], _incoming["suffix"] = None, ""
        if sublime.ok_cancel_dialog("The scripts, currently in TTS, could not be retrieved for backup.\n\nPush anyways?", "Push"):
            self.push()

    def push(self):
        w = self.window
        states = build_states(w, self.files)
        if not states:
            log("WARN", "nothing to push")
            return
        if setting(w, "history"):
            hist_files = list(self.files)       # Global's lua and xml are pushed together, so both go into the history
            for fn in (GLOBAL_LUA, GLOBAL_XML):
                p = os.path.join(self.base, fn)
                if states[0]["guid"] == "-1" and p not in hist_files and os.path.isfile(p):
                    hist_files.append(p)
            write_history(w, hist_files, self.hist_dest)
        total = sum(len(s["script"]) for s in states)
        if send({"messageID": 1, "scriptStates": states}):
            log("PUSH", "Save & Play: %d scripts, %d chars (%s)" % (len(states), total, ", ".join(os.path.basename(f) for f in self.files)))
            sublime.status_message("TTS: %d scripts sent" % len(states))
            if ensure_listener(w):      # errors from the reload arrive during the grace period; the script list TTS sends is verified
                expect_push_result(w, states, True)


class TtsExecuteCommand(sublime_plugin.TextCommand):
    """Run the selection (or the whole file) as Lua in Global; print() and the return value show in the panel."""

    def run(self, edit):
        parts = [self.view.substr(r) for r in self.view.sel() if not r.empty()]
        code = "\n".join(parts) if parts else self.view.substr(sublime.Region(0, self.view.size()))
        if not code.strip():
            return
        ensure_listener(self.view.window())
        if send({"messageID": 3, "guid": "-1", "script": code}):
            preview = re.sub(r"\s+", " ", code)
            log("EXEC", preview[:90] + ("..." if len(preview) > 90 else ""))


class TtsShowPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        if self.window.find_output_panel(PANEL) is None:
            self.window.create_output_panel(PANEL)
        self.window.run_command("show_panel", {"panel": "output." + PANEL})


class TtsListenerCommand(sublime_plugin.WindowCommand):
    """Start or stop the listener by hand (the "listener" setting decides what happens automatically)."""

    def run(self, action="toggle"):
        global _deadline
        if action == "stop" or (action == "toggle" and listener_running()):
            stop_listener()
        else:
            _deadline = time.time() + 3600 * 24     # by hand: stays until stopped by hand
            start_listener()

    def description(self, action="toggle"):
        return "Stop Listener" if listener_running() else "Start Listener"

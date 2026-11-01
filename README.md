# Tabletop Simulator Editor

A [Sublime Text](https://www.sublimetext.com) 3 / 4 package. Edit the Lua scripts and the XML UI of a Tabletop Simulator save in Sublime Text 3 or 4. The package talks to
Tabletop Simulator itself through its External Editor API; nothing else has to run beside Sublime Text and the game.

## Files

In the script folder (by default the first folder of your project):

    Global.-1.lua          the Global script
    Global.-1.xml          the Global UI
    <Name>.<GUID>.lua      one file per scripted object (or in "objects_subfolder")

Other subfolders are ignored, so notes and other material can live beside the scripts.

## Commands (Tools > Tabletop Simulator, command palette "Tabletop Simulator: ...", Ctrl+B)

* **Get Lua Scripts** (Ctrl+Alt+Space) - TTS sends the scripts of every object in the loaded save (objects inside bags
  or decks are not part of it, objects without a script are skipped); the files are written to the script folder and
  Global.-1.lua is opened. Files of objects that no longer exist are not deleted.
* **Save & Play** (Ctrl+Alt+S, Ctrl+B) - sends script files to TTS, which reloads the save with them. Only the scripts
  change, the table (objects, positions) stays as loaded in TTS. With `"push": "open"` the open script files of the
  project window are sent, with `"folder"` every script file of the folder; **Save & Play (whole folder)** does the
  latter regardless of the setting. Global's Lua and XML always travel together (TTS clears the UI when a Global script
  arrives without it), so when one of them is pushed the other is read from disk. A script whose object is not loose in
  the scene is ignored by TTS without harm. Unsaved files are saved after asking (`"save_before_push": true` saves
  without asking).
* **Execute Selection in Global** (Ctrl+Alt+E) - runs the selection (or the whole file) as Lua in Global; `print`
  output and errors show in the TTS output panel.
* **Show TTS Output** - the panel with print output, Lua errors and status lines. With `"show_panel_on_output": false`
  the panel stays in the background and Lua errors reported by TTS come up in a dialog instead.
* **Start / Stop Listener** - by hand, see below.

## Save id

The API talks to whatever save is loaded in TTS; it cannot tell which one. So the package keeps an id line in
Global.-1.lua (`-- Sublime TTS Editor - Unique ID to prevent pushing to wrong TTS mod - tts-editor-id: ...`, as last
line, or first with `"save_id": "first"`), added after the first Get or before the first push; a line moved elsewhere
in the file is found and left there. Before a push the package asks TTS for the id of the loaded save; after a Get it
compares the received Global with the project's one before writing anything. A different id means another save is
loaded (or another project is open): a dialog asks before pushing or overwriting. With history enabled, the scripts
about to be overwritten are first copied to the history folder with `-Overwritten` in their names. `"save_id": false`
turns all of it off.

## History

`"history": true` copies every pushed file to `<script folder>/_history/<yyyy-mm-dd hh-mm-ss>/`;
`"history_keep": 50` keeps the newest 50 history folders and deletes older ones at the next push.

## The listener

TTS answers on its own connection to port 39998, so the package has to listen there to receive anything: the scripts
of a Get, print output, Lua errors. `"listener"`:

* `"on_demand"` - started for a Get, push or Execute; stops `listener_grace_seconds` after a push or Execute (errors of
  the reload are still caught), or at once with 0; the answer to a Get is always waited for.
* `"first_use"` - started by the first Get, push, Execute or manual start, then runs until Sublime Text closes.
* `"always"` - runs from Sublime Text's start on (print output while you play).

Only one program can listen on that port: with another TTS editor tool running, the panel reports that the port is
taken and nothing can be received until that tool is closed and the listener restarted.

## Projects

One project per save: put the settings into the `.sublime-project` file (Project > Edit Project),

    "settings": { "tts_editor": { "listener": "first_use", "history": true, "history_keep": 50 } }

everything in `Tabletop Simulator Editor.sublime-settings` can be set there. Sublime Text reads the project file when
the project is opened; after editing it on disk, close and reopen the project.

## File interface (for other programs)

With `"file_interface": true` another program - a build script, an AI assistant - can drive TTS through files while
Sublime Text has the project open. Everything happens in `<script folder>/<file_interface_folder>/` (default
`_bridge`):

    outbox/     command files you drop here; each is carried out and deleted (a file that does not parse as JSON
                is renamed to *.json.bad after 30 s)
    inbox/      where "getScripts" writes the scripts of the loaded save (the project's files stay untouched)
    log.txt     every line of the TTS output panel, appended: "[hh:mm:ss] TAG    text"

A command file is one JSON object with a `cmd` field; files are processed in name order, so number them
(`001_get.json`, `002_push.json`). Commands:

| command | fields | effect |
|---|---|---|
| `{"cmd": "getScripts"}` | - | asks TTS for all scripts; they arrive in `inbox/` as `Global.-1.lua`, `Global.-1.xml`, `<Name>.<GUID>.lua` (log: `PULL`, then `RECV` with the count) |
| `{"cmd": "saveAndPlay"}` | - | pushes every script file of the script folder (Global's Lua and XML together) and TTS reloads the save; the save-id dialog is skipped, the log says so (`PUSH`); history applies when enabled |
| `{"cmd": "exec", "guid": "-1", "script": "print(1)"}` | `script`; `guid` optional, default `"-1"` = Global | runs the Lua in Global or in the object with that GUID; `print` output arrives as `PRINT` lines, errors as `LUAERR` |
| `{"cmd": "custom", "payload": {...}}` | `payload`: any JSON | delivered to the mod's `onExternalMessage(payload)` |
| `{"cmd": "snapshot"}` | - | copies the script files into a new history folder |

After a push the package compares the script list TTS reports after the reload with what was sent and logs the
result (`INFO push verified`, or `WARN push result: ...` naming objects whose script TTS did not take because they
are not loose in the scene, or scripts that differ). In Sublime this also comes up as a dialog unless
`"report_push_result"` is false.

The listener is started for every command that expects an answer, according to the `"listener"` setting. Log tags:
`PULL`, `RECV`, `PUSH`, `EXEC`, `SEND`, `HIST`, `PRINT` (TTS print output), `LUAERR` (a Lua error, with the GUID),
`CUSTOM` (a message the mod sent with `sendExternalMessage`), `RETURN`, `SAVED`, `INFO`, `WARN`, `ERR`.

### Protocol for automation tools and AI assistants

What a program (or an AI assistant with file access) needs to know to drive TTS through this interface:

1. Sublime Text must be running with the project open and `"file_interface": true` in its settings; TTS must be running
   with the save loaded. Nothing else is needed - no helper process.
2. Paths, relative to the project's script folder: commands go to `_bridge/outbox/`, results to `_bridge/log.txt`,
   downloaded scripts to `_bridge/inbox/` (`file_interface_folder` may name another folder than `_bridge`).
3. Write a command file in one step - or write it under another name or elsewhere and rename it into `outbox/` - so
   the poller (every 0.5 s) never reads a half-written file. One JSON object per file, UTF-8, `.json` extension.
   Number the files when order matters (`001_get.json`, `002_push.json`); they are processed in name order and
   deleted once carried out. A file that is still there after a few seconds was not processed: Sublime is not
   running, the project is not open, or the interface is off.
4. Read results from `log.txt`: it is append-only, so remember the last line you saw and read what follows. Every
   line is `[hh:mm:ss] TAG    text`. The command is echoed first (`PULL`, `PUSH`, `EXEC`, `SEND`), then what TTS
   answers: `PRINT` for each print() line, `RETURN`/`CUSTOM` for values the mod sends back, `LUAERR` for a Lua error
   (with the object's GUID), `RECV` when scripts arrived, `SAVED` when TTS saved the game (a Save & Play has saved
   and starts to reload; the mod's own onLoad prints follow), `INFO`/`WARN`/`ERR` for the package's own notes.
   Expect `PRINT` lines of an `exec` within a second; a `getScripts` answer within a few seconds; a Save & Play
   reload may take from seconds to a minute depending on the save.
5. To change scripts: edit the files in the script folder (never the copies in `inbox/`), then `saveAndPlay`. To
   inspect the live state of the game, `exec` a Lua snippet that prints what you need. To read the scripts TTS runs,
   `getScripts` and read `inbox/`. Keep in mind that `exec` runs in the game: a snippet that moves or destroys
   objects does exactly that.
6. Failure signs: `ERR` lines (TTS not reachable, port taken), `LUAERR` lines after a push (the pushed script has an
   error), `WARN push result` (an object file whose object is not in the scene, or a script TTS runs differently),
   a command file that is not deleted (see 3).

## Installation

Via [Package Control](https://packagecontrol.io): `Package Control: Install Package` > `Tabletop Simulator Editor`.
By hand: copy this repository into a folder `Tabletop Simulator Editor` inside Sublime's `Packages` folder
(Preferences > Browse Packages...).

## License

GNU General Public License v3.0 or later - see LICENSE. Issues and suggestions: https://github.com/wunditta/sublime-tabletop-simulator-editor/issues

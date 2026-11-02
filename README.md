# Tabletop Simulator Editor

A [Sublime Text](https://www.sublimetext.com) 3 / 4 package. It allows to download, edit, and push (Save & Play) the Lua
scripts and the XML UI of a Tabletop Simulator (TTS) game in Sublime Text 3 or 4 as a project. The package talks to
Tabletop Simulator itself through its External Editor API (with a listener python script running within Sublime as
separate thread); no separate program has to be installed, started or kept running.
The package also allows an AI agent to communicate with TTS (see section "File Interface" below).

## Usage

1. Start TTS and load the game you want to edit
2. Create a folder on your local machine where you want to save the scripts from TTS
3. Add the folder to a new project (Project > Add Folders to Project...)
4. Save your project (Project > Save Project as...). Recommended is to safe it to the same folder you just added.
5. Edit your settings (for all projects: Preferences > Package Settings > Tabletop Simulator Editor > Settings; project specific: Project > Edit Project (see below "Template for Project Settings"))
6. Get Lua Scripts and UI XML (see Commands below), edit the scripts and push to TTS afterwards.

## Files

In the script folder (by default the first folder of your project):

    Global.-1.lua          the Global script
    Global.-1.xml          the Global UI
    <Name>.<GUID>.lua      one file per scripted object (or in "objects_subfolder")
    <Name>.<GUID>.xml      the object's UI, only for objects that have one

Other subfolders are ignored, so notes and other material can live beside the scripts. Script and UI of an object are a
pair: TTS replaces both on a push, so a missing .xml file means "no UI" (an object's UI is removed when its .xml file is
deleted and the object pushed).

## Commands

### Find Commands
* Tools > Tabletop Simulator
* Tools > Build (Ctrl+B)
* Command Palette (Ctrl+Shift+P) > Tabletop Simulator: ...

### Commands List
* **Get Lua Scripts** (Ctrl+Alt+G): TTS sends the scripts of every object in the loaded save (note: scripts of
    objects, which do not exist in the TTS world (e.g. inside container, bags, decks), are not provided by TTS); the
    files are written to the script folder and Global.-1.lua is opened. Files of objects that no longer exist are not
    deleted.
* **Save & Play** (Ctrl+Alt+S, Ctrl+B): Sends script files to TTS via the Safe & Play function, which only saves the
    scripts and UI, and restores the game to its saved state with the new scripts. Only scripts currently open in
    Sublime are pushed to TTS (also see setting "push"). However, TTS requires to push the Lua and UI XML for an
    object, otherwise the not sent part becomes empty. Therefore, always both files are pushed, regardless which
    one (or both) you have open.
* **Save & Play (all scripts)**: Like above but pushes all scripts to TTS (note: pushing a script for an object, which
    does not exist in the 3D world, is silently ignored by TTS).
* **Execute Selection in Global** (Ctrl+Alt+E): Runs the selection (or the whole file) as Lua in Global. Output with
    `print` and errors show in the TTS output panel.
* **Show TTS Output**: the panel with print output, Lua errors and status lines. With `"show_panel_on_output": false`
    the panel stays in the background and Lua errors reported by TTS come up in a dialog instead.
* **Start / Stop Listener**: Start or stop the listener by hand, see below.

## Save ID

The API talks to whatever game is loaded in TTS; it cannot tell which one. If the wrong game is loaded in TTS a push will overwrite at least the Global script. You can enable Save ID (see settings "save_id") to prevent this with a unique ID, which is written as comment line into the Global.-1.lua script as first or last line:

`-- Sublime TTS Editor - Unique ID to prevent pushing to wrong TTS mod - tts-editor-id: #############`

The line is added after the first Get to Global.-1.lua. Before a push the ID of the currently loaded game is compared
with the local one. If the IDs do not match, or an ID is missing, a confirmation dialog asks how to proceed.

This also works in the other direction. If downloading scripts from TTS the IDs are compared and if they do not match a
confirmation dialog asks for permission. In both directions the history (if enabled) keeps the replaced scripts.

## History

If history is enabled (see setting "history") the history keeps the replaced state: before each push the scripts
currently in TTS are fetched and saved to `<script folder>/_history/<yyyy-mm-dd hh-mm-ss>/`, before each Get the
project's current script files are saved there. So the project and TTS hold the current state, the history every
earlier one, including scripts of objects you never had as files. A push takes a few seconds longer for the fetch;
if TTS does not answer within a minute a dialog asks whether to push without the backup.
The number of history folders kept can be configured ("history_keep"); the oldest beyond it are deleted at the next push.

## The Listener

TTS answers on its own connection to port 39998, so the package has to listen there to receive the scripts of a Get,
print output, Lua errors; it sends to port 39999. Both can be changed with the settings "listen_port" and "send_port"
in case TTS changes them in the future. A separate python process within Sublime is used for this and has the following modes:
* `"on_demand"`: Started for a Get, Push or Execute. It stops `listener_grace_seconds` afterwards, or at once when 0 (the answer to a Get is always waited for, so it can be checked if the upload was successful).
* `"first_use"`: Started by the first Get, Push, Execute or manual start, then runs until Sublime Text closes.
* `"always"`: Runs from Sublime Text's start on.

Note: Only one program can listen on that port: with another TTS editor tool running, the panel reports that the port is
taken and nothing can be received until that tool is closed and the listener restarted.

## TTS Output

### Panel

Everything the package reports goes to the TTS output panel, a section at the bottom of the Sublime Text window. With
`"show_panel_on_output": true` it opens by itself whenever a line arrives; a number instead of `true` (e.g. `5`) opens it
the same way and hides it again that many seconds after the last line; `false` leaves it in the background (Lua errors
then come up in a dialog). Escape closes it, Tools > Tabletop Simulator > Show TTS Output opens it again. Output from TTS
arrives only while the listener is running (see "The Listener").

Each line is `[hh:mm:ss] TAG    text`. The tags:
* `PULL` (scripts requested)
* `RECV` (scripts arrived)
* `PUSH` (Save & Play sent)
* `EXEC` (Lua sent for execution)
* `SEND` (custom message sent)
* `HIST` (history written)
* `PRINT` (print output from TTS)
* `LUAERR` (a Lua error reported by TTS, with the object's GUID)
* `CUSTOM` (a message the mod sent with sendExternalMessage)
* `RETURN` (a value returned by executed Lua)
* `SAVED` (TTS saved the game - a Save & Play saves first, then reloads)
* `INFO`, `WARN`, `ERR` (the package's own notes; `ERR` means TTS could not be reached or the listener's port is taken).

If the panel is disabled Lua errors reported by TTS come up in a dialog instead, so they are not missed.

### Log File

If enabled (`log: true`) the same lines as above are appended to `<script folder>\<log_folder>\log.txt`. Therefore the
listener must run for this too. The maximum file size can be defined (`log_max_kb`). If this limit is reached the file
is renamed to log.1.txt and a new log file is created. The file interface (below) needs the log for its answers and
turns it on regardless of the setting.

## Projects

Use a separate project for each game you work on. Set the settings for all projects in Preferences > Package Settings > Tabletop Simulator Editor > Settings.

Set the settings for a project in the project specific settings (Project > Edit Project; also see https://www.sublimetext.com/docs/projects.html). If the file is empty you can copy the example below for a start. Every setting from the general settings can be set in the project specific settings. Note: after changing and saving you have to close and reopen the project in order to take them into effect.

### Example for Project Settings

```JSON
{
    "folders":
    [
        { "path": ".", "folder_exclude_patterns": ["_history", "_backup"] },
    ],
    "settings":
    {
        "tts_editor":
        {
            "listener": "always",
        }
    }
}
```

## API

With `"file_interface": true` another program - a build script, an AI assistant - can drive TTS through files while
Sublime Text has the project open. Commands and answers live in `<script folder>/<file_interface_folder>/` (default
`_bridge`), the log in `<script folder>/<log_folder>/log.txt` (see "Log File" above):

    outbox/     command files you drop here; each is carried out and deleted (a file that does not parse as JSON
                is renamed to *.json.bad after 30 s)
    inbox/      where "getScripts" writes the scripts of the loaded save (the project's files stay untouched)
    log.txt     in the log folder: every line of the TTS output panel, appended: "[hh:mm:ss] TAG    text"

A command file is one JSON object with a `cmd` field; files are processed in name order, so number them
(`001_get.json`, `002_push.json`). Commands:

| command | fields | effect |
|---|---|---|
| `{"cmd": "getScripts"}` | - | asks TTS for all scripts; they arrive in `inbox/` as `Global.-1.lua`, `Global.-1.xml`, `<Name>.<GUID>.lua` and `.xml` (log: `PULL`, then `RECV` with the count) |
| `{"cmd": "saveAndPlay"}` | - | pushes every script file of the script folder (script and UI of each object together) and TTS reloads the save; the save-id dialog is skipped, the log says so (`PUSH`); with history enabled the scripts TTS runs are fetched into a history folder first |
| `{"cmd": "exec", "guid": "-1", "script": "print(1)"}` | `script`; `guid` optional, default `"-1"` = Global | runs the Lua in Global or in the object with that GUID; `print` output arrives as `PRINT` lines, errors as `LUAERR` |
| `{"cmd": "custom", "payload": {...}}` | `payload`: any JSON | delivered to the mod's `onExternalMessage(payload)` |

After a push the package compares the script list TTS reports after the reload with what was sent and logs the
result (`INFO push verified`, or `WARN push result: ...` naming objects whose script TTS did not take because they
are not loose in the scene, or scripts that differ). In Sublime this also comes up as a dialog unless
`"report_push_result"` is false.

The listener is started for every command that expects an answer, according to the `"listener"` setting. Log tags:
`PULL`, `RECV`, `PUSH`, `EXEC`, `SEND`, `HIST`, `PRINT` (TTS print output), `LUAERR` (a Lua error, with the GUID),
`CUSTOM` (a message the mod sent with `sendExternalMessage`), `RETURN`, `SAVED`, `INFO`, `WARN`, `ERR`.

### Protocol AI Assistants

What a program (or an AI assistant with file access) needs to know to drive TTS through this interface:

1. Sublime Text must be running with the project open and `"file_interface": true` in its settings; TTS must be running
   with the save loaded. Nothing else is needed - no helper process.
2. Paths, relative to the project's script folder: commands go to `_bridge/outbox/`, downloaded scripts to
   `_bridge/inbox/` (`file_interface_folder` may name another folder than `_bridge`), results to `log.txt` in the log
   folder (`log_folder`; by default the script folder itself).
3. Write a command file in one step - or write it under another name or elsewhere and rename it into `outbox/` - so
   the poller (once a second) never reads a half-written file. One JSON object per file, UTF-8, `.json` extension.
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

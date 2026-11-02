# Changelog

## 1.0.1

* Listener: a plugin reload no longer leaves an orphaned listener holding port 39998; the new instance stops it and takes
  over. A failed bind ("address in use") is retried once.

## 1.0.0

First release.

* Get Lua Scripts, Save & Play (open script files or whole folder, Ctrl+B build system), Execute Selection in Global
* TTS output panel with print output and Lua errors (dialog for errors when the panel stays hidden)
* Save id in Global.-1.lua, checked before a push and before a Get overwrites the project
* History of pushed files with a kept-count
* Listener modes on_demand / first_use / always
* After-push verification against the script list TTS reports
* File interface (command files and log) for automation tools and AI assistants

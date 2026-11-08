' Run VoiceMeeter Volume OSD without a console window, minimised to the tray.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "pythonw main.py --minimized", 0, False

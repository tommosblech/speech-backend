' Startet den Rechnungsassistenten UNSICHTBAR (ohne schwarzes Fenster).
' Der Browser oeffnet sich automatisch; beendet wird der Assistent
' ueber den "Beenden"-Knopf oben rechts in der Web-Oberflaeche.
'
' Tipp: Von dieser Datei eine Verknuepfung auf dem Desktop anlegen
' (Rechtsklick -> Senden an -> Desktop).
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
projektOrdner = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = projektOrdner
shell.Run """" & projektOrdner & "\Rechnungsassistent.bat"" /leise", 0, False

# HushMeet Google Meet extension

This unpacked Chrome extension detects entry to and exit from the in-call **Leave call** state; it does not trigger from the lobby page. It sends only the current Meet URL and join/leave state to the local HushMeet bridge. The bridge matches a join to a currently active, read-only Google Calendar event, then shows the category confirmation dialog. After a confirmed exit, HushMeet waits 90 continuous seconds before asking whether to stop recording. Choosing **Keep recording** suppresses further end prompts until the call reconnects and ends again.

1. In Chrome, open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select this `meet-extension` directory.
3. Copy the displayed extension ID and run:

   ```bash
   hushmeet-install-meet-extension EXTENSION_ID
   ```

4. Complete Calendar authorization with `hushmeet-calendar-auth`. Discord detection also uses the active timed Calendar event title when one is present; otherwise it falls back to `Discord voice chat`.

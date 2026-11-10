# HushMeet Google Meet extension

This unpacked Chrome extension detects entry to and exit from the in-call **Leave call** state; it does not trigger from the lobby page. It sends only the current Meet URL and join/leave state to the local HushMeet bridge. The bridge matches a join to a currently active, read-only Google Calendar event, then shows the category confirmation dialog. After a confirmed exit, HushMeet waits 90 continuous seconds before asking whether to stop recording. Choosing **Keep recording** suppresses further end prompts until the call reconnects and ends again.

1. In Chrome, open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select this `meet-extension` directory.
3. Copy the displayed extension ID and run:

   ```bash
   hushmeet-install-meet-extension EXTENSION_ID
   ```

4. Complete Calendar authorization with `hushmeet-calendar-auth`. Discord detection also uses the active timed Calendar event title when one is present; otherwise it falls back to `Discord voice chat`. It prompts only after Discord has both communication playback and microphone-capture streams for 30 continuous seconds; notification/media output alone is ignored. Set `discord_confirm_seconds` in `watcher.json` to adjust that confirmation window.

`~/.config/hushmeet/watcher.json` must set `allowed_google_account` to the one
Google Workspace address allowed to use HushMeet. Authorization requests only
`calendar.events.readonly` Calendar access; `openid` and `userinfo.email` are
used solely to verify that exact account. The refresh token is bound to the
verified account in a mode-600 local state file, so a different account or an
unbound token fails closed and asks for reauthorization.

For a private Google Auth Platform app, use **External** with **Testing** and
list only that account as a test user. Do not select **Internal** unless the
account is a managed member of the Cloud organization. Google expires a test
user's Calendar refresh token after seven days. The example's
`calendar_reauthorize_after_days: 6` warns one day early and repeats daily
until `hushmeet-calendar-auth` refreshes the local authorization.

## Calendar failure visibility

The watcher performs a read-only Calendar health check at startup and every six
hours. If Calendar access is revoked or invalid, it shows a desktop notification
with the reauthorization command. Repeated failures are limited to one reminder
per hour.

A Google Meet join that reaches the native bridge also shows the same notification
immediately if Calendar authorization is invalid. Chrome's extension service worker
logs native-message failures for troubleshooting in `chrome://extensions`.

#!/usr/bin/env python3
"""GNOME top-bar controls for local HushMeet recordings."""

from __future__ import annotations

import sys
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3
from gi.repository import Gio, GLib, Gtk

from hushmeet_calendar import load_config, nearby_events
from hushmeet_recording import (
    CATEGORIES,
    AudioInput,
    active_recording,
    available_audio_inputs,
    category_for_title,
    category_label,
    start_recording,
    stop_recording,
)


APPLICATION_ID = "io.hushnote.RecorderWidget"


class RecorderWidget(Gtk.Application):
    """One GNOME application instance that owns the AppIndicator control."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.background = False
        self.indicator: AppIndicator3.Indicator | None = None
        self.window: Gtk.ApplicationWindow | None = None
        self.status_label: Gtk.Label | None = None
        self.nearby_combo: Gtk.ComboBoxText | None = None
        self.title_entry: Gtk.Entry | None = None
        self.category_combo: Gtk.ComboBoxText | None = None
        self.audio_combo: Gtk.ComboBoxText | None = None
        self.start_button: Gtk.Button | None = None
        self.nearby: list[dict[str, Any]] = []
        self.audio_inputs: dict[str, AudioInput] = {}

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        # The indicator has no visible Gtk.Window while idle, so keep the
        # application alive explicitly instead of exiting after activation.
        self.hold()
        self.indicator = AppIndicator3.Indicator.new(
            APPLICATION_ID,
            "media-record-symbolic",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.refresh_state()
        GLib.timeout_add_seconds(1, self.refresh_state)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        self.background = "--background" in command_line.get_arguments()
        self.activate()
        return 0

    def do_activate(self) -> None:
        if not self.background:
            self.present_recorder()

    def _build_menu(self, state: dict[str, Any] | None) -> None:
        if not self.indicator:
            return
        menu = Gtk.Menu()
        if state:
            detail = Gtk.MenuItem.new_with_label(
                f"Recording: {state.get('title', 'Untitled')} · "
                f"{state.get('category_label') or category_label(state.get('category', 'sky'))}"
            )
            detail.set_sensitive(False)
            menu.append(detail)
            audio = Gtk.MenuItem.new_with_label(
                f"Input: {state.get('audio_label', 'Default microphone')}"
            )
            audio.set_sensitive(False)
            menu.append(audio)
            menu.append(Gtk.SeparatorMenuItem())
            stop = Gtk.MenuItem.new_with_label("Stop recording…")
            stop.connect("activate", self.on_stop_requested)
            menu.append(stop)
        else:
            start = Gtk.MenuItem.new_with_label("Start recording…")
            start.connect("activate", lambda *_: self.present_recorder())
            menu.append(start)
        menu.append(Gtk.SeparatorMenuItem())
        show = Gtk.MenuItem.new_with_label("Open recorder")
        show.connect("activate", lambda *_: self.present_recorder())
        menu.append(show)
        quit_item = Gtk.MenuItem.new_with_label("Quit widget")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def refresh_state(self) -> bool:
        state = active_recording()
        if self.indicator:
            if state:
                title = state.get("title", "Untitled recording")
                category = state.get("category_label") or category_label(state.get("category", "sky"))
                self.indicator.set_icon_full("media-record-symbolic", "Recording")
                self.indicator.set_label(f"REC · {title} · {category}", "REC")
                self.indicator.set_title(f"Recording: {title} ({category})")
            else:
                self.indicator.set_icon_full("media-record-symbolic", "Record meeting")
                self.indicator.set_label("Record", "Record")
                self.indicator.set_title("Hushnote Recorder Widget")
            self._build_menu(state)
        if self.status_label and self.window and self.window.get_visible():
            self._render_window_state(state)
        return True

    def _make_window(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self, title="Hushnote Recorder Widget")
        self.window.set_default_size(560, 320)
        self.window.set_border_width(18)
        self.window.connect("delete-event", lambda *_: self.window.hide_on_delete())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.window.add(box)
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_line_wrap(True)
        box.pack_start(self.status_label, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        box.pack_start(grid, True, True, 0)

        self.nearby_combo = Gtk.ComboBoxText()
        self.nearby_combo.connect("changed", self.on_nearby_changed)
        self.title_entry = Gtk.Entry()
        self.category_combo = Gtk.ComboBoxText()
        for value, label in CATEGORIES:
            self.category_combo.append(value, label)
        self.audio_combo = Gtk.ComboBoxText()

        rows = [
            ("Nearby calendar meeting", self.nearby_combo),
            ("Meeting name", self.title_entry),
            ("Category", self.category_combo),
            ("Input signal", self.audio_combo),
        ]
        for row, (label, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button.new_with_label("Close")
        cancel.connect("clicked", lambda *_: self.window.hide())
        self.start_button = Gtk.Button.new_with_label("Start recording")
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.connect("clicked", self.on_start_requested)
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(self.start_button, False, False, 0)
        box.pack_end(buttons, False, False, 0)

    def present_recorder(self) -> None:
        if not self.window:
            self._make_window()
        state = active_recording()
        self._render_window_state(state)
        if not state:
            self.populate_form()
        # Gtk.Widget.present() only presents the toplevel.  Its child widgets
        # remain hidden until explicitly shown on their first construction.
        self.window.show_all()
        self.window.present()

    def _render_window_state(self, state: dict[str, Any] | None) -> None:
        if not self.status_label or not self.start_button:
            return
        if state:
            self.status_label.set_text(
                f"Recording {state.get('title', 'Untitled')} in "
                f"{state.get('category_label') or category_label(state.get('category', 'sky'))}. "
                "Use the top-bar menu to stop it."
            )
            self.start_button.set_sensitive(False)
        else:
            self.status_label.set_text("Choose details, then explicitly start local recording.")
            self.start_button.set_sensitive(True)

    def populate_form(self) -> None:
        assert self.nearby_combo and self.title_entry and self.category_combo and self.audio_combo
        self.nearby_combo.remove_all()
        self.nearby_combo.append("manual", "Manual entry")
        self.nearby = []
        try:
            self.nearby = nearby_events(load_config())
            for index, event in enumerate(self.nearby):
                title = (event.get("summary") or "Untitled calendar event").strip()
                self.nearby_combo.append(str(index), title)
            self.nearby_combo.set_active(1 if self.nearby else 0)
            if not self.nearby:
                self.title_entry.set_text("")
                self.category_combo.set_active_id("sky")
        except Exception as exc:
            self.nearby_combo.set_active(0)
            self.title_entry.set_text("")
            self.category_combo.set_active_id("sky")
            self.status_label.set_text(
                f"Calendar unavailable ({exc}). Enter details manually to record."
            )

        self.audio_inputs = {item.key: item for item in available_audio_inputs()}
        self.audio_combo.remove_all()
        for item in self.audio_inputs.values():
            self.audio_combo.append(item.key, item.label)
        self.audio_combo.set_active_id("microphone")

    def on_nearby_changed(self, combo: Gtk.ComboBoxText) -> None:
        selected = combo.get_active_id()
        if not selected or selected == "manual" or not self.title_entry or not self.category_combo:
            return
        event = self.nearby[int(selected)]
        title = (event.get("summary") or "").strip()
        self.title_entry.set_text(title)
        self.category_combo.set_active_id(category_for_title(title))

    def on_start_requested(self, *_: object) -> None:
        if active_recording():
            self.refresh_state()
            return
        assert self.title_entry and self.category_combo and self.audio_combo and self.status_label
        title = self.title_entry.get_text()
        category = self.category_combo.get_active_id() or "sky"
        audio = self.audio_inputs.get(self.audio_combo.get_active_id() or "microphone")
        if not title.strip():
            self.status_label.set_text("Enter a meeting name before starting recording.")
            return
        if not audio:
            self.status_label.set_text("Choose an input signal before starting recording.")
            return
        try:
            start_recording(
                category,
                title,
                "Recorder widget",
                audio_source_type=audio.source_type,
                audio_source=audio.source,
                audio_label=audio.label,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self.status_label.set_text(f"Could not start recording: {exc}")
            return
        self.window.hide()
        self.refresh_state()

    def on_stop_requested(self, *_: object) -> None:
        state = active_recording()
        if not state:
            self.refresh_state()
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Stop recording {state.get('title', 'this meeting')}?",
        )
        dialog.format_secondary_text("The recording will be queued for HushMeet processing.")
        dialog.add_button("Keep recording", Gtk.ResponseType.CANCEL)
        dialog.add_button("Stop recording", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            stop_recording()
            self.refresh_state()


def main() -> int:
    return RecorderWidget().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/python3
import base64
import functools
import inspect
import json
import os.path
import re
import sys
import types
from enum import IntEnum
from typing import *

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.0')
from gi.repository import Gtk, WebKit2, GLib, Gio, Gdk  # noqa E402


def rsc_path(which_resource):
    return os.path.join(os.path.dirname(__file__), "app", which_resource)


# noinspection PyPep8Naming
class g_async:
    def __init__(self, wrap_me):
        self.wrappee = wrap_me

    def __getattr__(self, item: str):
        func = getattr(self.wrappee, item)
        finish = getattr(self.wrappee, item.removesuffix('_async') + '_finish', None)
        if finish is None:
            fst, *components = item.split('_')
            total = fst
            for comp in components:
                new_finish = getattr(self.wrappee, total + '_finish', None)
                if new_finish is not None:
                    finish = new_finish
                # We don't care about the complete item, since that does not
                # exist, so doing this only in the last loop iteration is fine
                total += "_" + comp

        @types.coroutine
        def async_fn(*args, **kwargs):
            result = yield functools.partial(func, *args, **kwargs)
            return finish(result) if finish is not None else result

        return async_fn

    @staticmethod
    def run(task, callback=None, error_callback=None, _initial=None):
        try:
            invoke_me = task.send(_initial)
        except StopIteration as task_result:
            if callback is not None:
                callback(task_result.value)
        except Exception as e:
            if error_callback:
                error_callback(e)
            else:
                raise
        else:
            invoke_me(callback=lambda _, result: g_async.run(task, callback, error_callback, _initial=result))

    @staticmethod
    def run_sync(task):
        result = None
        finished: int | None = None

        def cb(result_v):
            nonlocal result, finished
            result = result_v
            finished = 1

        def error_cb(exception_v):
            nonlocal result, finished
            result = exception_v
            finished = -1

        g_async.run(task, callback=cb, error_callback=error_cb)
        while not finished:
            Gtk.main_iteration()

        if finished > 0:
            return result
        else:
            raise result

    @staticmethod
    @types.coroutine
    def promise(cb):
        """Allows invoking callback-based functions in the g_async context. Use like callback_arg = await promise(cb).

        :param cb Called with one keyword argument, resolve. callback should be called with the value await should
        resume with.
        """

        def cb_wrapper(callback):
            cb(resolve=lambda result: callback(None, result))

        resolved = yield cb_wrapper
        return resolved


def _g_async_run_cb(async_fn):
    # A separate function allows debugging _args and _kwargs using a breakpoint
    def cb(*_args, **_kwargs):
        g_async.run(async_fn())

    return cb


def g_make_action(where: Gtk.ApplicationWindow, name: str, accel: str, cb):
    def wrapped_cb(*_args, **_kwargs):
        result = cb()
        _debug_print("Action:", cb)
        if inspect.isawaitable(result):
            g_async.run(result)

    action = Gio.SimpleAction.new(name, None)
    action.connect('activate', wrapped_cb)
    where.add_action(action)

    Gtk.Application.get_default().set_accels_for_action("win." + name, [accel])


class BooleanLock:
    def __init__(self, value):
        self.locked = value

    def __enter__(self):
        self.locked = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.locked = False


debug_log_enabled = False


def _debug_print(*args):
    if debug_log_enabled or "EXCALIDRAW_DEBUG" in os.environ or "PYCHARM_HOSTED" in os.environ:
        print(*args)


def _file_filter(chooser: Gtk.FileChooser, name: str, pattern: str):
    chooser_filter = Gtk.FileFilter()
    chooser_filter.set_name(name)
    chooser_filter.add_pattern(pattern)
    chooser.add_filter(chooser_filter)


def _remove_suffix_regex(base: str, suffix: str):
    if match := re.search(suffix, base):
        base = base[:match.start(0)]
    return base


class ExcalidrawSaveFormat(IntEnum):
    JSON = 0
    SVG = 1
    PNG = 2

    def to_js_name(self):
        return {
            ExcalidrawSaveFormat.JSON: "json",
            ExcalidrawSaveFormat.SVG: "svg",
            ExcalidrawSaveFormat.PNG: "png"
        }[self]

    @staticmethod
    def from_filename(filename: str):
        filename = filename.lower()
        if filename.endswith(".png"):
            return ExcalidrawSaveFormat.PNG
        elif filename.endswith(".svg"):
            return ExcalidrawSaveFormat.SVG
        else:
            return ExcalidrawSaveFormat.JSON


class ExcalidrawWindow:
    def __init__(self, parent_application: Gtk.Application | None = None, open_initially: str | None = None,
                 close_on_save: bool = False, fullscreen: bool = False):
        # noinspection PyArgumentList
        window = Gtk.ApplicationWindow(application=parent_application)
        window.set_default_size(800, 600)
        if fullscreen:
            window.fullscreen()

        webview_settings = WebKit2.Settings()
        webview_settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
        # noinspection PyArgumentList
        webview = WebKit2.WebView.new_with_settings(webview_settings)
        manager: WebKit2.UserContentManager = webview.get_user_content_manager()
        self._get_save_data_nonce = 0
        self._get_save_data_cbs = {}
        manager.register_script_message_handler('getSaveData')
        manager.connect('script-message-received::getSaveData', self._on_receive_save_data)

        self._run_javascript_queue = []
        self._excalidraw_initialized = False
        manager.register_script_message_handler('initializedExcalidraw')
        manager.connect('script-message-received::initializedExcalidraw',
                        lambda *_: g_async.run(self._on_initialize_excalidraw()))

        # Load only in the end, so that initializedExcalidraw works reliably
        webview.load_uri("file://" + rsc_path("build/index.html"))

        window.add(webview)
        self.webview = webview
        webview.show()

        g_make_action(window, "save", "<Control>s", self._action_save)
        g_make_action(window, "saveAs", "<Control><Shift>s", self._action_save_as)
        g_make_action(window, "open", "<Control>o", self._action_open)
        g_make_action(window, "print", "<Control>p", self._action_print)
        g_make_action(window, "export", "<Control>e", self._action_export)
        g_make_action(window, "fullscreen", "Escape", self._toggle_fullscreen)
        g_make_action(window, "quit", "<Control>q", Gtk.Application.get_default().quit)
        # self.window needs to set here, because _open_file requires it
        self.window = window

        self._save_location = None
        self._save_running = BooleanLock(False)
        self._export_last: Optional[Gio.File] = None
        self.close_on_save = close_on_save

        if open_initially is not None:
            g_async.run_sync(self._open_file(Gio.File.new_for_commandline_arg(open_initially)))

        print("Initialised")

    def _on_receive_save_data(self, _, response: WebKit2.JavascriptResult):
        result_json = json.loads(response.get_js_value().to_json(0))
        data = result_json['data']
        nonce = result_json['nonce']
        self._get_save_data_cbs.pop(nonce)(data)

    async def _on_initialize_excalidraw(self):
        _debug_print("Initialized Excalidraw")
        self._excalidraw_initialized = True
        if self._run_javascript_queue:
            for cb in self._run_javascript_queue:
                cb()

    async def get_save_data(self, save_format: ExcalidrawSaveFormat, for_export: bool = False):
        def cb(resolve):
            # This hack is necessary because unlike in Apple's WebKit, there is no way to run an async javascript
            # function. Our only option is to abuse signal handlers (though this only works if the function
            # finishes quickly enough).
            used_nonce = self._get_save_data_nonce
            self._get_save_data_cbs[used_nonce] = resolve
            args = {'format': save_format.to_js_name(), 'export': for_export}
            self._run_javascript(f"getSaveData({json.dumps(args)}, {used_nonce})")
            self._get_save_data_nonce += 1

        result = await g_async.promise(cb)
        _debug_print("get_save_data:", result)
        return result

    def _get_save_format(self) -> ExcalidrawSaveFormat:
        if self._save_location is not None:
            return ExcalidrawSaveFormat.from_filename(self._save_location.get_uri())
        else:
            return ExcalidrawSaveFormat.JSON

    def _run_javascript(self, arg):
        if self._excalidraw_initialized:
            self.webview.run_javascript(arg)
        else:
            _debug_print("Excalidraw is not yet initialized:", arg)
            self._run_javascript_queue.append(lambda: self.webview.run_javascript(arg))

    def _load_from(self, data: bytes, save_format: ExcalidrawSaveFormat):
        args = {'format': save_format.to_js_name()}
        if save_format == ExcalidrawSaveFormat.JSON:
            args['data'] = json.loads(data)
        elif save_format == ExcalidrawSaveFormat.SVG:
            # We can't directly encode bytes to JSON
            args['blob'] = data.decode('utf-8')
        elif save_format == ExcalidrawSaveFormat.PNG:
            args['base64'] = base64.b64encode(data)
        _debug_print("_load_from:", args)
        self._run_javascript(f"loadSaveData({json.dumps(args)});")

    async def _export_to(self, save_format: ExcalidrawSaveFormat, for_export: bool = False):
        save_data = await self.get_save_data(save_format, for_export=for_export)
        if save_format == ExcalidrawSaveFormat.JSON:
            return json.dumps(save_data).encode('utf-8')
        elif save_format == ExcalidrawSaveFormat.SVG:
            return save_data['blob'].encode('utf-8')
        elif save_format == ExcalidrawSaveFormat.PNG:
            return base64.b64decode(save_data['base64'])

    async def _perform_save(self):
        if self._save_running.locked:
            return
        with self._save_running:
            assert self._save_location is not None  # Must not call if no save location known
            stream: Gio.FileOutputStream = await g_async(self._save_location).replace_async(
                None, False, Gio.FileCreateFlags.NONE, GLib.PRIORITY_DEFAULT)
            buffer = await self._export_to(self._get_save_format())
            await g_async(stream).write_async(buffer, GLib.PRIORITY_DEFAULT)
            await g_async(stream).close_async(GLib.PRIORITY_DEFAULT)

    async def _action_save(self):
        if self._save_location is None:
            await self._action_save_as()
        else:
            await self._perform_save()
            if self.close_on_save:
                self.window.close()

    def _make_file_chooser(self, action, ok):
        chooser = Gtk.FileChooserDialog(action=action)
        chooser.set_do_overwrite_confirmation(True)
        chooser.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)

        # Using SVG is most convenient, so put it at the top
        _file_filter(chooser, "Excalidraw SVG", "*.excalidraw.svg")
        _file_filter(chooser, "Excalidraw PNG", "*.excalidraw.png")
        _file_filter(chooser, "Excalidraw JSON", "*.excalidraw")

        if self._save_location is not None:
            chooser.set_uri(self._save_location.get_uri())
        elif action != Gtk.FileChooserAction.OPEN:
            chooser.set_current_name("Untitled.excalidraw.svg")
        return chooser

    async def _set_save_location(self, new_file: Gio.File):
        if self._save_location is not None:
            old_loc = await g_async(self._save_location).query_info_async(Gio.FILE_ATTRIBUTE_ID_FILE,
                                                                          Gio.FileQueryInfoFlags.NONE,
                                                                          GLib.PRIORITY_DEFAULT)
            new_loc = await g_async(new_file).query_info_async(Gio.FILE_ATTRIBUTE_ID_FILE, Gio.FileQueryInfoFlags.NONE,
                                                               GLib.PRIORITY_DEFAULT)
            if old_loc != new_loc:
                self._save_running = BooleanLock(False)
        self._save_location = new_file

    async def _action_save_as(self):
        chooser = self._make_file_chooser(Gtk.FileChooserAction.SAVE, Gtk.STOCK_SAVE_AS)

        if chooser.run() == Gtk.ResponseType.OK:
            await self._set_save_location(chooser.get_file())
            await self._perform_save()
            self.close_on_save = False
        chooser.destroy()

    async def _action_open(self):
        chooser = self._make_file_chooser(Gtk.FileChooserAction.OPEN, Gtk.STOCK_OPEN)
        if chooser.run() == Gtk.ResponseType.OK:
            await self._open_file(chooser.get_file())
        chooser.destroy()

    def _action_print(self, *_):
        print_op = WebKit2.PrintOperation(web_view=self.webview)
        print_op.run_dialog(self.window)

    async def _open_file(self, file: Gio.File):
        await self._set_save_location(file)
        try:
            success, content, *_ = await g_async(self._save_location).load_contents_async()
            if success:
                self._load_from(content, self._get_save_format())
        except GLib.Error as e:
            print(f"Failed to open '{self._save_location.get_uri()}': {e}", file=sys.stderr)

    async def _action_export(self):
        chooser = Gtk.FileChooserDialog(action=Gtk.FileChooserAction.SAVE)
        chooser.set_do_overwrite_confirmation(True)
        chooser.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE_AS, Gtk.ResponseType.OK)
        _file_filter(chooser, "svg", "*.svg")
        _file_filter(chooser, "png", "*.png")
        if self._export_last is not None:
            chooser.set_uri(self._export_last.get_uri())
        elif self._save_location is not None:
            name = _remove_suffix_regex(self._save_location.get_uri(), r"(\.excalidraw)?(\.(svg|png))?$")
            name = name or "output"
            chooser.set_uri(name + ".svg")
        else:
            chooser.set_current_name("Untitled.excalidraw.svg")

        if chooser.run() == Gtk.ResponseType.OK:
            file: Gio.File = chooser.get_file()
            self._export_last = file
            save_format = ExcalidrawSaveFormat.PNG if file.get_uri().endswith(".png") else ExcalidrawSaveFormat.SVG
            content = await self._export_to(save_format, for_export=True)
            _debug_print('export', content)
            # Use .new to supress PyCharm warning
            await g_async(file).replace_contents_bytes_async(GLib.Bytes.new(content), None, False,
                                                             Gio.FileCreateFlags.NONE)
        chooser.destroy()

    def _toggle_fullscreen(self, *_):
        fullscreen = self.window.get_window().get_state() & (Gdk.WindowState.FULLSCREEN | Gdk.WindowState.MAXIMIZED)
        self.window.unfullscreen() if fullscreen else self.window.fullscreen()

    def show(self):
        self.window.show()


class ExcalidrawApp(Gtk.Application):
    def __init__(self, **win_kwargs):
        super().__init__(application_id='org.nbfalcon.ExcalidrawApp')

        self.connect('activate', lambda *_: self._activate(**win_kwargs))

    def _activate(self, **win_kwargs):
        window = ExcalidrawWindow(self, **win_kwargs)
        window.window.connect('destroy', lambda *_: self.quit())
        window.show()


def main():
    import argparse
    argparser = argparse.ArgumentParser(description="Excalidraw webview wrapper")
    argparser.add_argument('file', nargs='?', help="Which file to open/save to")
    argparser.add_argument('-c', '--close-on-save', action='store_true', dest='cl_save',
                           help="Close when saving normally (Save As disables this)")
    argparser.add_argument('-f', '--fullscreen', action='store_true',
                           help="Launch in fullscreen mode")
    argparser.add_argument('-d', '--debug', action='store_true', help="Enable debug logging")
    args, rest_args = argparser.parse_known_args()

    if args.debug:
        global debug_log_enabled
        debug_log_enabled = True

    # noinspection PyArgumentList
    Gtk.init()

    app = ExcalidrawApp(open_initially=args.file, close_on_save=args.cl_save, fullscreen=args.fullscreen)
    app.set_default()
    app.run(rest_args)


if __name__ == '__main__':
    main()
